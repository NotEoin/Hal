"""Tests for `halcore.connect` (`hal connect HARNESS`)."""

import json
import stat

import pytest

from halcore import cli, connect
from halkit.testing import TempHalHome


@pytest.fixture
def home():
    with TempHalHome() as h:
        yield h


def test_connect_prints_unsandboxed_line_first(home):
    assert connect.render("qwen").splitlines()[0].startswith("# UNSANDBOXED: Qwen Code")


def test_snippet_refers_to_the_token_file_and_never_the_secret(home):
    text = connect.render("codex")
    assert f"$(cat {connect.token_file('codex')})" in text
    assert "OPENAI_BASE_URL=http://127.0.0.1:8741/v1" in text and "HAL_HARNESS=1" in text


def test_claude_gets_the_anthropic_root_url(home):
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:8741\n" in connect.render("claude")


def test_issue_stores_a_named_token_in_a_private_file(home, monkeypatch, capsys):
    seen = []

    class Done:
        returncode, stdout, stderr = 0, b"s3cret\n", b""

    monkeypatch.setattr(connect.proc, "run", lambda argv, **kw: seen.append(argv) or Done())
    assert connect.main(["goose", "--issue"]) == 0
    path = connect.token_file("goose")
    assert path.read_text() == "s3cret\n" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "connect-goose" in seen[0] and "s3cret" not in capsys.readouterr().out


def test_failed_issue_exits_1_without_a_token_file(home, monkeypatch, capsys):
    class Done:
        returncode, stdout, stderr = 1, b"", b"no gateway"

    monkeypatch.setattr(connect.proc, "run", lambda argv, **kw: Done())
    assert connect.main(["goose", "--issue"]) == 1
    assert not connect.token_file("goose").exists() and "no gateway" in capsys.readouterr().err


def test_list_and_json_and_missing_harness(home, capsys):
    assert connect.main(["--list"]) == 0 and "opencode" in capsys.readouterr().out
    assert connect.main(["cline", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0].startswith("# UNSANDBOXED")
    assert connect.main([]) == 2


def test_connect_is_a_command():
    assert cli.resolve(["connect", "qwen"])[0].module == "connect"
