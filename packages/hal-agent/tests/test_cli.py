"""Tests for `halagent.cli`: one-shot and REPL modes against the fake model server."""

import io

from halagent import cli
from halkit.testing.model_server import Reply


def test_one_shot_prints_the_answer_and_exits_zero(server, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "f.txt").write_text("data")
    server.script(
        Reply(tool_calls=[("read", {"path": "f.txt"})]),
        Reply(tool_calls=[("finish", {"summary": "file has data"})]),
    )
    code = cli.main(["-p", "look", "-C", str(tmp_path), "--endpoint", server.url])
    assert code == 0 and capsys.readouterr().out.strip() == "file has data"


def test_max_model_requests_exits_four(server, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    server.script(*[Reply(tool_calls=[("glob", {"pattern": "*"})]) for _ in range(3)])
    code = cli.main(["-p", "x", "-C", str(tmp_path), "--endpoint", server.url, "--max-turns", "2"])
    assert code == 4


def test_unreachable_endpoint_exits_three(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr("time.sleep", lambda s: None)  # skip the retry backoff
    code = cli.main(["-p", "x", "-C", str(tmp_path), "--endpoint", "http://127.0.0.1:1"])
    assert code == 3 and "model request failed" in capsys.readouterr().out


def test_repl_runs_prompts_until_exit(server, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\n/exit\n"))
    server.script("hi there")
    code = cli.main(["-C", str(tmp_path), "--endpoint", server.url])
    assert code == 0 and "hi there" in capsys.readouterr().out


def test_profiles_show_prints_values_with_their_sources(capsys):
    assert cli.main(["profiles", "show", "gemma4:12b"]) == 0
    out = capsys.readouterr().out
    assert (
        "family gemma4" in out and 'leak_parsers = ["gemma4", "json", "name_json"]  # shipped:gemma4' in out
    )


def test_profiles_list_names_every_family(capsys):
    assert cli.main(["profiles", "list"]) == 0
    assert "qwen35" in capsys.readouterr().out
