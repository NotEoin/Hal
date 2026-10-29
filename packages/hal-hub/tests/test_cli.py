"""Tests for `halhub.cli` (REQ-HUB-52 to REQ-HUB-58): exit codes, `--yes` rules, listings and `doctor`.

Each test runs `main` in-process against the synthetic `demo` pack and one throwaway module
with real workers; the Hal home is the temporary one from `conftest.py`.
"""

from __future__ import annotations

import io
import json
import textwrap
import threading
import time

import pytest
from hubkit import DEMO_TOKEN

from halhub import __version__, cli
from halhub import _control_standin as control
from halhub.surfaces import HARNESS_ENV


class FakeTerminal:
    """Stands in for a terminal on stdin; the tests never read from it."""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError("not a real terminal")


@pytest.fixture
def scratch(tmp_path):
    """A path-scanned module `scratch` whose tools fail or disagree with their manifest."""
    root = tmp_path / "scratch"
    (root / "halscratch").mkdir(parents=True)
    (root / "halscratch" / "__init__.py").write_text("")
    (root / "halscratch" / "tools.py").write_text(
        textwrap.dedent(
            """
            def boom() -> str:
                raise RuntimeError("disk on fire")

            def strict(name: str, colour: str) -> str:
                return name + colour
            """
        )
    )
    (root / "hal-module.toml").write_text(
        textwrap.dedent(
            """
            schema = 2
            [module]
            name = "scratch"
            summary = "Throwaway tools for CLI tests"
            python = "halscratch.tools"

            [[tools]]
            name = "scratch_boom"
            function = "boom"
            description = "Fail on purpose."
            tier = "read"

            [[tools]]
            name = "scratch_strict"
            function = "strict"
            description = "Disagree with the manifest."
            tier = "read"
            [tools.params.name]
            required = true
            [tools.params.size]
            type = "integer"
            """
        )
    )
    return root


@pytest.fixture
def home(hal_home, fixtures, scratch, tmp_path):
    """A config that finds `demo` and `scratch` only, with the demo token and no bubblewrap."""
    token = tmp_path / "demo-token"
    token.write_text(DEMO_TOKEN + "\n")
    token.chmod(0o600)
    hal_home.config.write_text(
        textwrap.dedent(
            f"""
            [hub]
            module_paths = ["{fixtures / "demo"}", "{scratch}"]
            use_entry_points = false
            [hub.runner]
            bwrap = "off"
            [demo]
            token_file = "{token}"
            """
        )
    )
    return hal_home


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# ---- call and exit codes (REQ-HUB-52, 53, 54, 56) ----------------------------------------------


def test_call_without_a_tool_prints_usage_and_exits_2(home, capsys):
    code, out, err = run(capsys, "call")
    assert code == 2 and "usage" in err and out == ""


def test_word_without_underscore_is_a_usage_error_not_a_tool(home, capsys):
    code, _, err = run(capsys, "calendar")
    assert code == 2 and "tool names contain '_'" in err


def test_subcommands_can_never_shadow_a_tool():
    assert all("_" not in name for name in cli.SUBCOMMANDS)


def test_shorthand_call_prints_the_result_as_json(home, capsys):
    code, out, _ = run(capsys, "demo_echo", "text=hi", "times=2")
    assert code == 0 and json.loads(out) == "hi hi"


def test_raw_prints_a_string_result_without_quotes(home, capsys):
    code, out, _ = run(capsys, "call", "demo_echo", "--json", '{"text": "hi"}', "--raw")
    assert code == 0 and out == "hi\n"


def test_pairs_and_json_together_are_a_usage_error(home, capsys):
    code, _, err = run(capsys, "call", "demo_echo", "text=a", "--json", '{"text": "b"}')
    assert code == 2 and "not both" in err


def test_unknown_tool_and_bad_arguments_exit_2(home, capsys):
    assert run(capsys, "nope_tool")[0] == 2
    code, _, err = run(capsys, "demo_echo", "times=2")
    assert code == 2 and "invalid_arguments" in err


def test_tool_error_exits_1(home, capsys):
    code, out, err = run(capsys, "scratch_boom")
    assert code == 1 and "disk on fire" in err and out == ""


def test_ask_without_a_terminal_waits_for_approve_then_exits_3(home, capsys):
    home.policy.write_text("schema_version = 1\n[approval]\ntimeout_s = 1\n")
    code, out, err = run(capsys, "demo_wipe", "path=/tmp/x")
    assert code == 3 and "approval-timeout" in err and out == ""
    assert "hal-hub approve apr_" in err


def test_ask_without_a_terminal_runs_after_approval_on_the_control_socket(home, capsys, monkeypatch):
    monkeypatch.setattr(control, "peer_in_harness", lambda pid: False)

    def approve_when_listed() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pending = control.list_pending()
            if pending:
                control.answer(pending[0], "allow", scope="once")
                return
            time.sleep(0.05)

    approver = threading.Thread(target=approve_when_listed, daemon=True)
    approver.start()
    code, out, _ = run(capsys, "demo_wipe", "path=/tmp/x")
    approver.join(5)
    assert code == 0 and json.loads(out) == "wiped /tmp/x"


def test_yes_is_refused_without_terminal(home, capsys):
    code, _, err = run(capsys, "demo_wipe", "path=/tmp/x", "--yes")
    assert code == 2 and "terminal" in err


def test_yes_is_refused_under_a_harness(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeTerminal())
    monkeypatch.setenv(HARNESS_ENV, "claude")
    code, _, err = run(capsys, "demo_wipe", "path=/tmp/x", "--yes")
    assert code == 2 and "HAL_HARNESS" in err


def test_json_and_yes_work_together(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeTerminal())
    monkeypatch.delenv(HARNESS_ENV, raising=False)
    code, out, err = run(capsys, "call", "demo_wipe", "--json", '{"path": "/tmp/x"}', "--yes")
    assert code == 0 and json.loads(out) == "wiped /tmp/x"
    assert "demo_wipe" in err and "--yes" in err  # the person sees what --yes approved


# ---- configuration errors (REQ-HUB-55) ---------------------------------------------------------


def test_malformed_config_exits_2_naming_the_file_without_traceback(hal_home, capsys):
    hal_home.config.write_text("[hub]\nmodule_paths = = []\n")
    code, _, err = run(capsys, "tools")
    assert code == 2 and str(hal_home.config) in err and "Traceback" not in err


def test_mcp_with_an_unknown_profile_exits_2_before_serving(home, capsys):
    code, out, err = run(capsys, "mcp", "--profile", "no-such-profile")
    assert code == 2 and "no-such-profile" in err and out == ""


def test_mcp_with_an_unknown_option_exits_2(home, capsys):
    code, _, err = run(capsys, "mcp", "--transport", "carrier-pigeon")
    assert code == 2 and "invalid choice" in err


def test_malformed_policy_exits_2_naming_the_file(home, capsys):
    home.policy.write_text("schema_version = 1\n[tools.demo_echo\n")
    code, _, err = run(capsys, "demo_echo", "text=hi")
    assert code == 2 and str(home.policy) in err and "Traceback" not in err


def test_config_flag_points_at_another_file(home, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_CONFIG", str(home.config))  # restored after the test
    other = tmp_path / "other.toml"
    other.write_text("[hub]\nuse_entry_points = false\n")
    code, out, _ = run(capsys, "--config", str(other), "tools", "--json")
    assert code == 0 and [t["name"] for t in json.loads(out)] == ["hal_find_tools", "hal_modules"]


# ---- listings ----------------------------------------------------------------------------------


def test_tools_lists_usable_tools_and_finds_matches(home, capsys):
    code, out, _ = run(capsys, "tools", "--json")
    names = [t["name"] for t in json.loads(out)]
    assert code == 0 and "demo_echo" in names and "hal_find_tools" in names
    _, out, _ = run(capsys, "tools", "pretend", "to", "delete", "--json")
    assert json.loads(out)[0]["name"] == "demo_wipe"


def test_tools_all_shows_denied_tools_marked(home, capsys):
    home.policy.write_text('schema_version = 1\n[tools.demo_echo]\nverdict = "deny"\n')
    _, out, _ = run(capsys, "tools", "--json")
    assert "demo_echo" not in [t["name"] for t in json.loads(out)]
    _, out, _ = run(capsys, "tools", "--all", "--json")
    assert {t["name"]: t["status"] for t in json.loads(out)}["demo_echo"] == "denied"


def test_show_prints_tier_flags_and_verdict_per_profile(home, capsys):
    code, out, _ = run(capsys, "show", "demo_wipe", "--json")
    info = json.loads(out)
    assert code == 0 and info["tier"] == "act"
    assert info["verdicts"]["default"] == "ask" and info["verdicts"]["trusted"] == "allow"


def test_modules_lists_status_and_source(home, capsys):
    code, out, _ = run(capsys, "modules", "--json")
    rows = {m["module"]: m for m in json.loads(out)["modules"]}
    assert code == 0 and rows["hub"]["source"] == "builtin" and rows["demo"]["status"] == "ok"


def test_version_prints_the_package_version(capsys):
    assert run(capsys, "version")[:2] == (0, f"{__version__}\n")


# ---- doctor (REQ-HUB-57) -----------------------------------------------------------------------


def test_doctor_starts_workers_and_reports_signature_mismatches(home, capsys):
    code, out, _ = run(capsys, "doctor", "--json")
    report = json.loads(out)
    modules = {m["module"]: m for m in report["modules"]}
    assert code == 1 and report["failed"]
    assert modules["demo"]["errors"] == [] and modules["demo"]["worker_ms"] is not None
    assert modules["hub"]["errors"] == []
    errors = " | ".join(modules["scratch"]["errors"])
    assert "does not accept manifest parameter 'size'" in errors
    assert "requires 'colour'" in errors


# ---- approvals, approve, deny (REQ-HUB-22) -----------------------------------------------------


@pytest.fixture
def waiting(home, monkeypatch):
    """A pending request on a control socket of this process, and the decision it gets."""
    from datetime import timedelta

    from halhub._approvals_standin import ApprovalRequest
    from halhub._policy_standin import Origin, Scope, Taint, Tier
    from halkit.timeutil import iso_z, utc_now

    monkeypatch.setattr(control, "peer_in_harness", lambda pid: False)
    now = utc_now()
    req = ApprovalRequest(
        id="apr_00000000000000aa",
        session="00000000aaaa",
        origin=Origin.CLI,
        channel=None,
        pack="demo",
        pack_version="0",
        tool="demo_wipe",
        tier=Tier.ACT,
        key="sha256:" + "c" * 64,
        reason="tier:act - act tools ask",
        args={"path": "/tmp/x"},
        display="demo_wipe  [act]  demo\n  path: /tmp/x",
        summary="Allow demo_wipe (act, pack demo)?",
        content=False,
        challenge=None,
        scopes=(Scope.ONCE, Scope.SESSION),
        taint=Taint.UNTRUSTED,
        guard=None,
        created_at=iso_z(now, ms=True),
        expires_at=iso_z(now + timedelta(seconds=60), ms=True),
    )
    server = control.ControlServer("00000000aaaa")
    box: dict = {}
    thread = threading.Thread(target=lambda: box.setdefault("decision", server.decide(req, 10)), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not control.list_pending() and time.monotonic() < deadline:
        time.sleep(0.02)
    box["join"] = lambda: thread.join(5)
    yield box
    server.close()


def at_terminal(monkeypatch, answer: str = "") -> None:
    monkeypatch.setattr(cli, "person_at_terminal", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO(answer))


def test_approve_shows_the_call_and_allows_once(waiting, capsys, monkeypatch):
    at_terminal(monkeypatch, "y\n")
    code, out, _ = run(capsys, "approve", "apr_00000000000000aa")
    waiting["join"]()
    decision = waiting["decision"]
    assert code == 0 and "path: /tmp/x" in out and "Allow this exact call once?" in out
    assert (decision.verdict, decision.scope.value, decision.source) == ("allow", "once", "control")


def test_deny_refuses_the_request(waiting, capsys, monkeypatch):
    at_terminal(monkeypatch, "yes\n")
    assert run(capsys, "deny", "apr_00000000000000aa")[0] == 0
    waiting["join"]()
    assert waiting["decision"].verdict == "deny"


def test_approve_without_a_yes_sends_nothing(waiting, capsys, monkeypatch):
    at_terminal(monkeypatch, "n\n")
    code, out, _ = run(capsys, "approve", "apr_00000000000000aa")
    assert code == 0 and "Nothing sent" in out and "decision" not in waiting


def test_approvals_lists_pending_requests(waiting, capsys, monkeypatch):
    at_terminal(monkeypatch)
    code, out, _ = run(capsys, "approvals", "--json")
    assert code == 0 and [r["id"] for r in json.loads(out)] == ["apr_00000000000000aa"]


def test_approve_unknown_id_exits_2(waiting, capsys, monkeypatch):
    at_terminal(monkeypatch, "y\n")
    code, _, err = run(capsys, "approve", "apr_ffffffffffffffff")
    assert code == 2 and "no pending request" in err


@pytest.mark.parametrize("command", ["approvals", "approve", "deny"])
def test_approve_without_a_terminal_is_refused(waiting, capsys, command):
    code, _, err = run(capsys, command, *([] if command == "approvals" else ["apr_00000000000000aa"]))
    assert code == 2 and "person at a terminal" in err and "decision" not in waiting


def test_approve_under_a_harness_is_refused(waiting, capsys, monkeypatch):
    monkeypatch.setenv(HARNESS_ENV, "1")
    monkeypatch.setattr("sys.stdin", FakeTerminal())
    monkeypatch.setattr("sys.stdout", FakeTerminal())
    assert cli.main(["approve", "apr_00000000000000aa"]) == 2
