"""Tests for `halagent.acp`: the JSON-RPC lifecycle, event mapping, cancel and errors."""

import io
import json
import queue
import threading
import time

import pytest

from halagent import cli
from halagent.acp.server import AcpServer
from halagent.loop import Agent
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.model_server import Reply


class Pipe(io.TextIOBase):
    """A line pipe: the server reads what the test writes, in order."""

    def __init__(self):
        self.q: queue.Queue[str | None] = queue.Queue()

    def put(self, obj):
        self.q.put(json.dumps(obj) + "\n")

    def close_input(self):
        self.q.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.q.get()
        if line is None:
            raise StopIteration
        return line


class Client:
    def __init__(self, server_url, root):
        self.inp, self.out = Pipe(), io.StringIO()
        settings = AgentSettings(endpoint=server_url)

        def factory(cwd, sid, on_update):
            return Agent(settings, Toolset(cwd), on_update=on_update)

        self.srv = AcpServer(root, factory, self.inp, self.out)
        self.thread = threading.Thread(target=self.srv.serve)
        self.thread.start()
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        self.inp.put({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        return self.n

    def sid(self):
        """The id of the first session, once the server has created it."""
        deadline = time.monotonic() + 5
        while not self.srv.sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        return next(iter(self.srv.sessions))

    def finish(self):
        self.inp.close_input()
        self.thread.join(10)
        return [json.loads(line) for line in self.out.getvalue().splitlines()]


def reply_to(msgs, rid):
    return next(m for m in msgs if m.get("id") == rid)


def test_initialize_reports_capabilities_without_http_mcp(server, tmp_path):
    c = Client(server.url, tmp_path)
    rid = c.call("initialize", {"protocolVersion": 1})
    res = reply_to(c.finish(), rid)["result"]
    assert res["protocolVersion"] == 1 and res["agentCapabilities"]["mcpCapabilities"] == {
        "http": False,
        "sse": False,
    }
    assert res["authMethods"] == []


def test_prompt_streams_tool_calls_and_replies_with_end_turn(server, tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    server.script(Reply(tool_calls=[("read", {"path": "a.txt"})]), "it says hello")
    c = Client(server.url, tmp_path)
    new = c.call("session/new", {"cwd": str(tmp_path), "_meta": {"hal": {"sessionId": "3f9a1c0b7e2d"}}})
    prompt = c.call(
        "session/prompt", {"sessionId": "3f9a1c0b7e2d", "prompt": [{"type": "text", "text": "read it"}]}
    )
    msgs = c.finish()
    assert reply_to(msgs, new)["result"]["sessionId"] == "3f9a1c0b7e2d"
    assert reply_to(msgs, prompt)["result"] == {"stopReason": "end_turn"}
    kinds = [m["params"]["update"]["sessionUpdate"] for m in msgs if m.get("method") == "session/update"]
    assert kinds == ["tool_call", "tool_call_update", "agent_message_chunk"]
    call = next(m for m in msgs if m.get("method") == "session/update")["params"]["update"]
    assert (call["title"], call["kind"], call["status"]) == ("read", "read", "pending")


def test_turn_limit_maps_to_max_turn_requests_with_hal_meta(server, tmp_path):
    server.script(*[Reply(tool_calls=[("glob", {"pattern": "*"})]) for _ in range(40)])
    c = Client(server.url, tmp_path)
    c.call("session/new", {"cwd": str(tmp_path)})
    sid = c.sid()
    c.srv.sessions[sid].settings = AgentSettings(endpoint=server.url, max_turns=2)
    rid = c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "x"}]})
    res = reply_to(c.finish(), rid)["result"]
    assert res == {"stopReason": "max_turn_requests", "_meta": {"hal": {"stopReason": "turn_limit"}}}


def test_cancel_stops_a_running_prompt(server, tmp_path):
    server.script(*[Reply(tool_calls=[("run", {"command": "sleep 0.3"})]) for _ in range(30)])
    c = Client(server.url, tmp_path)
    c.call("session/new", {"cwd": str(tmp_path)})
    sid = c.sid()
    rid = c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "x"}]})
    time.sleep(0.5)
    c.call("session/cancel", {"sessionId": sid})
    assert reply_to(c.finish(), rid)["result"]["stopReason"] == "cancelled"


def test_cwd_outside_the_workspace_is_invalid_params(server, tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    c = Client(server.url, root)
    rid = c.call("session/new", {"cwd": str(tmp_path)})
    assert reply_to(c.finish(), rid)["error"]["code"] == -32602


@pytest.mark.parametrize("bad", ["../x", "ABC", "3f9a1c0b7e2dd"])
def test_malformed_session_id_hint_is_rejected(server, tmp_path, bad):
    c = Client(server.url, tmp_path)
    rid = c.call("session/new", {"cwd": str(tmp_path), "_meta": {"hal": {"sessionId": bad}}})
    assert reply_to(c.finish(), rid)["error"]["code"] == -32602


def test_unknown_method_garbage_line_and_review_mode_are_errors(server, tmp_path):
    c = Client(server.url, tmp_path)
    a = c.call("fs/read_text_file")
    c.inp.q.put("not json\n")
    b = c.call("session/set_mode", {"sessionId": "x", "modeId": "review"})
    msgs = c.finish()
    assert reply_to(msgs, a)["error"]["code"] == -32601 and reply_to(msgs, b)["error"]["code"] == -32602
    assert any(m.get("error", {}).get("code") == -32700 for m in msgs)


def test_second_prompt_while_running_is_refused(server, tmp_path):
    server.script(*[Reply(tool_calls=[("run", {"command": "sleep 0.3"})]) for _ in range(3)], "end")
    c = Client(server.url, tmp_path)
    c.call("session/new", {"cwd": str(tmp_path)})
    sid = c.sid()
    p = {"sessionId": sid, "prompt": [{"type": "text", "text": "x"}]}
    c.call("session/prompt", p)
    time.sleep(0.2)
    second = c.call("session/prompt", p)
    assert reply_to(c.finish(), second)["error"]["code"] == -32002


def test_cli_acp_mode_serves_over_stdio(server, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(req) + "\n"))
    assert cli.main(["--acp", "-C", str(tmp_path), "--endpoint", server.url]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["agentInfo"]["name"] == "hal-agent"
