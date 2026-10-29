"""Tests for `halhub.pack.main`, a standalone pack's command line (REQ-HUB-49).

The pack is the synthetic `demo` module, found on a path next to `probe` and `routines`, so
each test also checks that nothing outside the pack leaks in.
"""

from __future__ import annotations

import json

import pytest

from halhub import mcp, pack
from halhub._audit_standin import tail


@pytest.fixture
def home(hal_home, demo_token, fixtures):
    """A config that finds `demo`, `routines` and `probe`; the pack under test is `demo` alone."""
    dirs = [str(fixtures / name) for name in ("demo", "routines", "probe")]
    with hal_home.config.open("a") as fh:
        fh.write(
            f"[hub]\nmodule_paths = {json.dumps(dirs)}\nuse_entry_points = false\n"
            '[hub.runner]\nbwrap = "off"\n'
        )
    return hal_home


def run(capsys, *argv, extra=None):
    code = pack.main("demo", ("demo",), list(argv), extra=extra)
    out, err = capsys.readouterr()
    return code, out, err


def test_extra_subcommand_runs_and_is_not_a_tool(home, capsys, monkeypatch):
    seen = []

    def connect(args: list[str]) -> int:
        seen.append(args)
        return 7

    assert run(capsys, "connect", "--user", "me", extra={"connect": connect})[0] == 7
    assert seen == [["--user", "me"]]
    code, out, _ = run(capsys, "tools", "--json", extra={"connect": connect})
    assert code == 0 and "connect" not in out
    code, _, err = run(capsys, "call", "connect", extra={"connect": connect})
    assert code == 2 and "unknown_tool" in err
    served = []
    monkeypatch.setattr(mcp, "serve_stdio", lambda hub, options: served.append(options))
    run(capsys, "mcp", extra={"connect": connect})
    assert "connect" not in served[0].tools


@pytest.mark.parametrize("name", ["mcp", "tools", "call", "doctor", "Connect", "two words", "x" * 33])
def test_extra_name_clash_raises(name):
    with pytest.raises(ValueError, match="extra subcommand"):
        pack.main("demo", ("demo",), ["tools"], extra={name: lambda args: 0})


def test_bad_pack_name_or_no_modules_raises():
    with pytest.raises(ValueError, match="pack name"):
        pack.main("Demo Pack", ("demo",), [])
    with pytest.raises(ValueError, match="no modules"):
        pack.main("demo", (), [])


def test_tools_lists_only_the_packs_modules(home, capsys):
    code, out, _ = run(capsys, "tools", "--json")
    modules = {row["module"] for row in json.loads(out)}
    assert code == 0 and modules == {"demo"}


def test_call_runs_with_the_standalone_profile(home, capsys):
    code, out, _ = run(capsys, "call", "demo_echo", "text=hi")
    assert code == 0 and json.loads(out) == "hi"
    decisions = [r for r in tail(n=0) if r["event"] == "decision"]
    assert decisions[-1]["profile"] == "standalone"


def test_tools_of_other_packs_are_unknown(home, capsys):
    code, _, err = run(capsys, "call", "probe_shout", "text=a")
    assert code == 2 and "unknown_tool" in err and err.startswith("hal-demo:")


def test_mcp_serves_flat_mode_limited_to_the_pack(home, capsys, monkeypatch):
    served = []
    monkeypatch.setattr(mcp, "serve_stdio", lambda hub, options: served.append(options))
    assert run(capsys, "mcp")[0] == 0
    options = served[0]
    assert (options.mode, options.profile) == ("flat", "standalone")
    assert options.tools == {"demo_echo", "demo_wipe", "demo_lights", "demo_fetch"}


def test_mcp_module_option_names_only_the_packs_modules(home, capsys, monkeypatch):
    monkeypatch.setattr(mcp, "serve_stdio", lambda hub, options: None)
    code, _, err = run(capsys, "mcp", "--module", "probe")
    assert code == 2 and "not a module of pack demo" in err


def test_hal_hub_commands_outside_the_pack_set_are_refused(home, capsys):
    for command in ("approve", "modules", "token", "demo_echo"):
        code, _, err = run(capsys, command)
        assert code == 2 and "hal-demo has mcp, tools, call, doctor" in err


def test_doctor_reports_only_the_packs_modules(home, capsys):
    code, out, _ = run(capsys, "doctor", "--json")
    report = json.loads(out)
    assert [m["module"] for m in report["modules"]] == ["demo"]
