"""Tests for `halagent.headless`: output formats, exit codes, timeout and signals."""

import io
import json
import os
import signal
import subprocess
import sys
import threading

import pytest

from halagent import cli
from halagent.headless import OUTCOMES, Renderer, run_headless
from halagent.loop import Agent
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.model_server import Reply

FINISH = Reply(tool_calls=[("finish", {"summary": "all done"})])


def run(server, tmp_path, fmt="json", script=(), **kw):
    server.script(*script)
    agent = Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path, task_mode=True))
    out = io.StringIO()
    code = run_headless(agent, "go", fmt=fmt, out=out, session_id="abc123abc123", cwd=str(tmp_path),
                        install_signals=False, **kw)  # fmt: skip
    return code, out.getvalue()


def test_json_result_has_the_documented_fields(server, tmp_path):
    code, out = run(
        server, tmp_path, script=[Reply(tool_calls=[("write", {"path": "a.txt", "content": "x"})]), FINISH]
    )
    obj = json.loads(out)
    assert code == 0 and obj["type"] == "result" and obj["subtype"] == "success"
    assert obj["result"] == "all done" and obj["stop_reason"] == "finished" and obj["exit_code"] == 0
    assert obj["num_model_requests"] == 2 and obj["num_tool_calls"] == 1
    assert obj["files_changed"] == ["a.txt"] and obj["stats"]["first_edit_call"] == 1
    assert obj["ts"].endswith("Z") and obj["usage"]["input_tokens"] > 0


def test_text_format_prints_only_the_final_text(server, tmp_path):
    code, out = run(server, tmp_path, fmt="text", script=[FINISH])
    assert code == 0 and out == "all done\n"


def test_stream_json_has_one_object_per_line_ending_with_the_result(server, tmp_path):
    (tmp_path / "f.txt").write_text("hi")
    code, out = run(server, tmp_path, fmt="stream-json",
                    script=[Reply(tool_calls=[("read", {"path": "f.txt"})]), FINISH])  # fmt: skip
    rows = [json.loads(line) for line in out.splitlines()]
    assert [r["type"] for r in rows][0] == "system" and rows[0]["subtype"] == "init"
    assert rows[-1]["type"] == "result" and all(r["session_id"] == "abc123abc123" for r in rows)
    kinds = [b["type"] for r in rows if "message" in r for b in r["message"]["content"]]
    assert "tool_use" in kinds and "tool_result" in kinds


def test_limit_maps_to_error_max_turns_and_exit_four(server, tmp_path):
    server.script(*[Reply(tool_calls=[("glob", {"pattern": str(n)})]) for n in range(5)])
    agent = Agent(AgentSettings(endpoint=server.url, max_turns=2), Toolset(tmp_path, task_mode=True))
    out = io.StringIO()
    code = run_headless(
        agent, "go", fmt="json", out=out, session_id="s", cwd=str(tmp_path), install_signals=False
    )
    assert code == 4 and json.loads(out.getvalue())["subtype"] == "error_max_turns"


def test_http_error_is_exit_one_and_unreachable_is_exit_three(server, tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    code, out = run(server, tmp_path, script=[Reply(status=400)])
    assert code == 1 and json.loads(out)["subtype"] == "error_during_execution"
    agent = Agent(AgentSettings(endpoint="http://127.0.0.1:1"), Toolset(tmp_path, task_mode=True))
    buf = io.StringIO()
    assert (
        run_headless(
            agent, "go", fmt="json", out=buf, session_id="s", cwd=str(tmp_path), install_signals=False
        )
        == 3
    )
    assert json.loads(buf.getvalue())["subtype"] == "error_model_unreachable"


def test_timeout_cancels_the_run_with_exit_124(server, tmp_path):
    code, out = run(server, tmp_path, timeout=0.2,
                    script=[Reply(text="slow", delay=1.0), Reply(text="never")])  # fmt: skip
    assert code == 124 and json.loads(out)["subtype"] == "error_timeout"


def test_every_documented_stop_reason_has_an_outcome():
    assert {"finished", "max_tool_calls", "loop", "refusal", "timeout", "cancelled"} <= set(OUTCOMES)


def test_unknown_format_is_rejected():
    with pytest.raises(ValueError):
        Renderer("xml", io.StringIO(), "s")


def test_task_dash_reads_the_task_from_stdin(server, tmp_path, capsys, monkeypatch):
    server.script(FINISH)
    monkeypatch.setattr("sys.stdin", io.StringIO("do it\n"))
    assert cli.main(["-p", "-", "-C", str(tmp_path), "--endpoint", server.url, "--no-session"]) == 0
    assert server.requests[0]["body"]["messages"][-1]["content"].startswith("do it")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_cancels_the_run_and_exits_130(server, tmp_path):
    server.script(Reply(text="slow", delay=3.0))
    agent = Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path, task_mode=True))
    out = io.StringIO()
    threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()
    code = run_headless(agent, "go", fmt="json", out=out, session_id="s", cwd=str(tmp_path))
    assert code == 130 and json.loads(out.getvalue())["subtype"] == "error_cancelled"


def test_files_changed_comes_from_git_status(tmp_path):
    from halagent.headless import files_changed

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=30)
    (tmp_path / "new.py").write_text("x = 1\n")
    assert files_changed(str(tmp_path), []) == ["new.py"]
