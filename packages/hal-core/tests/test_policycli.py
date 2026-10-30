"""Tests for `halcore.policycli`: `hal policy` and `hal audit` (REQ-CLI-70, 71, 102)."""

import io
import json

import pytest

from halcore import policycli
from halhub.audit import AuditLog
from halkit.testing import TempHalHome

BUILTIN_TOOL = "hal_find_tools"  # a built-in read tool of the hub
ALLOW_ARGS = '{"query": "lamp"}'
DEFAULT_POLICY = 'schema_version = 1\nprofile = "default"\n'


@pytest.fixture
def home():
    with TempHalHome() as h:
        h.config.write_text("")
        yield h


class Terminal(io.StringIO):
    """A stdin that claims to be a terminal and holds the typed answer."""

    def isatty(self) -> bool:
        return True


def run(*argv):
    return policycli.main(list(argv))


def test_init_writes_a_policy_that_check_accepts(home, capsys):
    assert run("policy", "init") == 0
    assert run("policy", "check") == 0
    assert "ok" in capsys.readouterr().out


def test_init_does_not_replace_an_existing_policy_without_force(home, capsys):
    home.policy.write_text('schema_version = 1\nprofile = "strict"\n')
    assert run("policy", "init") == 1
    assert home.policy.read_text() == 'schema_version = 1\nprofile = "strict"\n'
    assert run("policy", "init", "--force") == 0
    assert (home.policy.parent / "policy.toml.bak").read_text() == 'schema_version = 1\nprofile = "strict"\n'


def test_check_reports_a_malformed_policy_and_exits_1(home, capsys):
    home.policy.write_text("profile = [\n")
    assert run("policy", "check") == 1
    assert "hal policy:" in capsys.readouterr().err


def test_explain_prints_the_decision_without_running_the_tool(home, capsys):
    assert run("policy", "explain", BUILTIN_TOOL, "--args", ALLOW_ARGS) == 0
    assert "allow" in capsys.readouterr().out


def test_explain_exit_code_maps_the_verdict(home):
    home.policy.write_text(f'schema_version = 1\n[tools.{BUILTIN_TOOL}]\nverdict = "deny"\n')
    home.policy.chmod(0o600)  # the loader refuses a policy others can edit
    assert run("policy", "explain", BUILTIN_TOOL, "--args", ALLOW_ARGS, "--exit-code") == policycli.EXIT_DENY
    assert run("policy", "explain", BUILTIN_TOOL, "--args", ALLOW_ARGS) == 0


def test_explain_of_an_unknown_tool_is_a_usage_error(home, capsys):
    assert run("policy", "explain", "no_such_tool") == policycli.EXIT_USAGE
    assert "no tool named" in capsys.readouterr().err


def test_show_json_names_the_profile(home, capsys):
    assert run("policy", "show", "--json") == 0
    assert json.loads(capsys.readouterr().out)["profile"] == "default"


def test_edit_installs_a_valid_file_and_keeps_a_backup(home, monkeypatch):
    home.policy.write_text(DEFAULT_POLICY)
    monkeypatch.setenv("VISUAL", "sed -i s/default/strict/")
    assert run("policy", "edit") == 0
    assert 'profile = "strict"' in home.policy.read_text()
    assert 'profile = "default"' in (home.policy.parent / "policy.toml.bak").read_text()


def test_an_invalid_policy_edit_is_not_installed(home, monkeypatch, capsys):
    home.policy.write_text(DEFAULT_POLICY)
    monkeypatch.setenv("VISUAL", "sed -i s/default/no-such-profile/")
    assert run("policy", "edit") == 1
    assert home.policy.read_text() == DEFAULT_POLICY


def test_allow_writes_the_rule_only_after_a_typed_y(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", Terminal("y\n"))
    assert run("policy", "allow", BUILTIN_TOOL, "--args", ALLOW_ARGS) == 0
    assert BUILTIN_TOOL in home.policy.read_text()
    assert BUILTIN_TOOL in capsys.readouterr().out  # the diff was shown first


def test_allow_writes_nothing_for_another_answer(home, monkeypatch):
    monkeypatch.setattr("sys.stdin", Terminal("n\n"))
    assert run("policy", "allow", BUILTIN_TOOL, "--args", ALLOW_ARGS) == 1
    assert not home.policy.exists()


def test_allow_refuses_without_a_terminal_so_a_pipe_cannot_extend_the_policy(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert run("policy", "allow", BUILTIN_TOOL, "--args", ALLOW_ARGS) == 1
    assert not home.policy.exists()
    assert "needs a terminal" in capsys.readouterr().err


def test_audit_tail_escapes_control_characters_from_the_log(home, capsys):
    log = AuditLog()
    log.append("call", session="s1", tool="evil\x1b[31m")
    assert run("audit", "tail", "-n", "5") == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "call" in out


def test_audit_verify_exits_1_on_a_damaged_log(home, capsys):
    log = AuditLog()
    log.append("call", session="s1", tool="t")
    day = next((home.state / "audit").glob("*.jsonl"))
    day.write_text(day.read_text() + "not json\n")
    assert run("audit", "verify") == 1
