"""Tests for the `hal-remote` CLI skeleton."""

import json

import pytest

from halkit import config
from halremote import authorize, cli, serve, servercheck


@pytest.mark.parametrize("command", sorted(set(cli.COMMANDS) - cli.IMPLEMENTED))
def test_unimplemented_command_exits_2_and_says_so(command, capsys):
    assert cli.main([command]) == cli.EXIT_USAGE
    assert "not implemented" in capsys.readouterr().err


def test_missing_command_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == cli.EXIT_USAGE


def test_remote_tables_are_registered_with_the_documented_defaults():
    tables = {t.name: t for t in config.known_tables()}
    assert {k.name: k.default for k in tables["remote"].keys}["desktop_tools"] is True
    server = {k.name: k.default for k in tables["remote.server"].keys}
    assert server["max_sessions"] == 4
    assert server["delivery_ttl_s"] == 86400
    assert (server["from_prefix_v4"], server["from_prefix_v6"]) == (24, 64)


@pytest.mark.parametrize("key", ["from_prefix_v4", "from_prefix_v6"])
def test_prefix_check_rejects_zero(key):
    spec = next(k for k in config.known_tables() if k.name == "remote.server").keys
    check = next(k for k in spec if k.name == key).check
    assert check(0) is not None
    assert check(24) is None


def test_package_carries_the_workspace_version():
    import halremote

    assert halremote.__version__ == "0.1.0"


def test_server_check_prints_json_and_exits_1_on_a_critical_failure(monkeypatch, capsys):
    bad = servercheck.CheckResult("SC03", "t", "critical", "fail", "d", "f")
    monkeypatch.setattr(servercheck, "run_checks", lambda **kw: [bad])
    assert cli.main(["server-check", "--json"]) == cli.EXIT_FAILED
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["checks"][0]["id"] == "SC03"


def test_server_check_exits_0_and_prints_a_line_per_check(monkeypatch, capsys):
    ok = servercheck.CheckResult("SC15", "sshrc absent", "warn", "pass", "d", "")
    monkeypatch.setattr(servercheck, "run_checks", lambda **kw: [ok])
    assert cli.main(["server-check"]) == cli.EXIT_OK
    assert capsys.readouterr().out.startswith("SC15 ")


def test_serve_receives_its_options_unparsed(monkeypatch):
    seen = []
    monkeypatch.setattr(serve, "main", lambda argv: seen.append(list(argv)) or 64)
    assert cli.main(["serve", "--client", "laptop", "--hub", "/x"]) == 64
    assert seen == [["--client", "laptop", "--hub", "/x"]]


def test_revoke_exits_1_and_names_what_failed(monkeypatch, capsys):
    monkeypatch.setattr(authorize, "revoke", lambda *a, **k: ["gateway token: boom"])
    assert cli.main(["revoke", "laptop"]) == cli.EXIT_FAILED
    assert "gateway token: boom" in capsys.readouterr().err


def test_authorize_refusal_exits_1_with_the_message(monkeypatch, capsys):
    monkeypatch.setattr(
        authorize, "resolve_paths", lambda: (_ for _ in ()).throw(authorize.AuthorizeError("no hal"))
    )
    assert cli.main(["authorize", "--client", "laptop", "--stdin"]) == cli.EXIT_FAILED
    assert "no hal" in capsys.readouterr().err


def test_clients_json_lists_the_registry(monkeypatch, capsys):
    from halremote.model import ClientRecord

    rec = ClientRecord("laptop", "c", "u", "any", "", "SHA256:a", "SHA256:b", (8741,), "remote-laptop")
    monkeypatch.setattr(authorize, "clients", lambda: [rec])
    assert cli.main(["clients", "--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)[0]["forwards"] == [8741]


def test_status_of_a_profile_that_never_ran_exits_4(tmp_path, monkeypatch, capsys):
    from halremote import pairing
    from halremote.model import Forward, Profile

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "config.toml"))
    pairing.save_profile(
        Profile("desktop", "laptop", "desk.lan", "me", 22, (Forward("gateway", 8741, 8741),))
    )
    assert cli.main(["status"]) == 4
    assert "not running" in capsys.readouterr().out


def test_status_without_any_profile_exits_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "config.toml"))
    assert cli.main(["status"]) == 3
    assert "hal remote setup" in capsys.readouterr().err
