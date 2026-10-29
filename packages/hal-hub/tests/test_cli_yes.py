"""Tests for `hal-hub call --yes`: it answers one ask and never widens the policy (RESOLUTIONS B4).

`--yes` works only when stdin is a terminal and `HAL_HARNESS` is unset (`test_cli.py` covers
the refusals). These cases show what it may approve once allowed: exactly one `once` grant
for an `ask`, recorded with the `tty` surface as its source, and nothing that the policy
denies or that a self-approval argument claims.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from hubkit import DEMO_TOKEN, FakeTerminal, short_approval_timeout

from halhub import cli
from halhub.audit import tail
from halhub.surfaces import HARNESS_ENV


@pytest.fixture
def at_terminal(hal_home, fixtures, tmp_path, monkeypatch):
    """A person at a terminal, outside any harness, with only the `demo` pack installed."""
    token = tmp_path / "demo-token"
    token.write_text(DEMO_TOKEN + "\n")
    token.chmod(0o600)
    hal_home.config.write_text(
        textwrap.dedent(
            f"""
            [hub]
            module_paths = ["{fixtures / "demo"}"]
            use_entry_points = false
            [hub.runner]
            bwrap = "off"
            [demo]
            token_file = "{token}"
            """
        )
    )
    monkeypatch.setattr("sys.stdin", FakeTerminal())
    monkeypatch.delenv(HARNESS_ENV, raising=False)
    return hal_home


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def events(name: str) -> list[dict]:
    return [r for r in tail(n=0) if r["event"] == name]


def test_yes_answers_one_ask_once_from_the_tty_surface(at_terminal, capsys):
    code, out, _ = run(capsys, "demo_wipe", "path=~/Downloads/x", "--yes")
    assert code == cli.EXIT_OK and json.loads(out) == f"wiped {Path.home() / 'Downloads' / 'x'}"
    (approval,) = events("approval")
    assert (approval["verdict"], approval["scope"], approval["source"]) == ("allow", "once", "tty")


def test_yes_does_not_carry_over_to_the_next_call(at_terminal, capsys, monkeypatch):
    short_approval_timeout(monkeypatch)
    assert run(capsys, "demo_wipe", "path=~/Downloads/x", "--yes")[0] == cli.EXIT_OK
    code, _, _ = run(capsys, "call", "demo_wipe", "--json", '{"path": "~/Downloads/x"}')
    assert code == cli.EXIT_DENIED  # the same call asks again, and nobody answers
    assert len(events("approval_request")) == 2
    assert [a["verdict"] for a in events("approval")][0] == "allow"
    assert all(a["verdict"] != "allow" for a in events("approval")[1:])


def test_yes_cannot_run_a_tool_the_policy_denies(at_terminal, capsys):
    at_terminal.policy.write_text('schema_version = 1\n[tools.demo_wipe]\nverdict = "deny"\n')
    code, out, _ = run(capsys, "demo_wipe", "path=~/Downloads/x", "--yes")
    assert code in (cli.EXIT_USAGE, cli.EXIT_DENIED) and out == ""
    assert events("approval") == []


def test_yes_cannot_approve_a_path_outside_the_roots(at_terminal, capsys):
    code, out, err = run(capsys, "demo_wipe", "path=~/.ssh/id_ed25519", "--yes")
    assert code == cli.EXIT_DENIED and out == "" and "path:" in err
    assert events("approval") == []


def test_yes_with_a_confirmed_argument_still_records_the_attempt(at_terminal, capsys):
    args = json.dumps({"path": "~/Downloads/x", "_confirmed": True})
    code, _, _ = run(capsys, "call", "demo_wipe", "--json", args, "--yes")
    assert code == cli.EXIT_OK
    assert [e["keys"] for e in events("self_approval_attempt")] == [["_confirmed"]]
    assert events("approval")[0]["source"] == "tty"
