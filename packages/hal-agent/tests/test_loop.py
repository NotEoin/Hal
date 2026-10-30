"""Tests for `halagent.loop` with a fake model server and a fake broker."""

import json

from halagent.loop import Agent
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.broker import FakeBroker
from halkit.testing.model_server import Reply


def make(server, tmp_path, **kw):
    s = AgentSettings(endpoint=server.url, **kw)
    return Agent(s, Toolset(tmp_path))


def test_native_tool_call_runs_and_final_text_is_returned(server, tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    server.script(Reply(tool_calls=[("read", {"path": "a.txt"})]), "it says hello")
    r = make(server, tmp_path).run("read a.txt")
    assert (r.text, r.stopped, r.turns, r.tool_calls) == ("it says hello", "done", 2, 1)
    tool_msgs = [m for m in server.requests[1]["body"]["messages"] if m["role"] == "tool"]
    assert "hello" in tool_msgs[0]["content"]


def test_write_tool_changes_the_workspace(server, tmp_path):
    server.script(Reply(tool_calls=[("write", {"path": "n.txt", "content": "x"})]), "done")
    make(server, tmp_path).run("write")
    assert (tmp_path / "n.txt").read_text() == "x"


def test_tool_error_is_reported_to_the_model_not_raised(server, tmp_path):
    server.script(Reply(tool_calls=[("read", {"path": "../x"})]), "ok")
    make(server, tmp_path).run("go")
    msgs = server.requests[1]["body"]["messages"]
    assert "outside_workspace" in [m for m in msgs if m["role"] == "tool"][0]["content"]


def test_text_format_fallback_parses_tool_calls(server, tmp_path):
    call = '<tool_call>{"name": "write", "arguments": {"path": "t.txt", "content": "hi"}}</tool_call>'
    server.script(call, "all done")
    r = make(server, tmp_path, tool_format="prompt").run("write t")
    assert r.text == "all done" and (tmp_path / "t.txt").read_text() == "hi"
    body = server.requests[0]["body"]
    assert "tools" not in body and "<tool_call>" in body["messages"][0]["content"]


def test_turn_limit_stops_the_loop(server, tmp_path):
    server.script(*[Reply(tool_calls=[("glob", {"pattern": "*"})]) for _ in range(5)])
    r = make(server, tmp_path, max_turns=3).run("loop")
    assert r.stopped == "max_model_requests" and r.turns == 3


def test_tool_call_limit_stops_the_loop(server, tmp_path):
    server.script(Reply(tool_calls=[("glob", {"pattern": "*"})] * 4))
    r = make(server, tmp_path, max_tool_calls=2).run("many")
    assert r.stopped == "max_tool_calls" and r.tool_calls == 2


def test_hub_tool_is_offered_and_called(server, tmp_path):
    with FakeBroker(tmp_path / "b.sock") as broker:
        broker.add_tool("web_read", "page", description="Read a page")
        server.script(Reply(tool_calls=[("web_read", {"url": "u"})]), "got it")
        agent = make(server, tmp_path, broker=str(broker.socket_path))
        r = agent.run("read")
        agent.close()
        names = [t["function"]["name"] for t in server.requests[0]["body"]["tools"]]
    assert "web_read" in names and r.text == "got it" and broker.calls == [("web_read", {"url": "u"})]


def test_history_persists_across_prompts(server, tmp_path):
    server.script("one", "two")
    agent = make(server, tmp_path)
    agent.run("a")
    agent.run("b")
    roles = [m["role"] for m in json.loads(json.dumps(server.requests[1]["body"]["messages"]))]
    assert roles == ["system", "user", "assistant", "user"]


def test_finish_ends_a_task_turn_and_todo_is_free(server, tmp_path):
    server.script(
        Reply(tool_calls=[("todo", {"plan": "- [ ] a"})]),
        Reply(tool_calls=[("finish", {"summary": "all done"})]),
    )
    agent = Agent(AgentSettings(endpoint=server.url, max_tool_calls=1), Toolset(tmp_path, task_mode=True))
    r = agent.run("go")
    assert (r.text, r.stopped, r.tool_calls) == ("all done", "finished", 0)


def test_unknown_tool_error_is_json_with_error_type(server, tmp_path):
    server.script(Reply(tool_calls=[("teleport", {})]), "ok")
    make(server, tmp_path).run("go")
    tool = [m for m in server.requests[1]["body"]["messages"] if m["role"] == "tool"][0]
    assert json.loads(tool["content"])["error_type"] == "unknown_tool"


def test_aliased_tool_name_and_argument_run_the_real_tool(server, tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    server.script(Reply(tool_calls=[("read_file", {"file_path": "a.txt"})]), "ok")
    make(server, tmp_path).run("go")
    tool = [m for m in server.requests[1]["body"]["messages"] if m["role"] == "tool"][0]
    assert "hello" in tool["content"]


def test_call_leaked_into_native_text_is_executed(server, tmp_path):
    leak = '<tool_call>{"name": "write", "arguments": {"path": "l.txt", "content": "z"}}</tool_call>'
    server.script(leak, "done")
    r = make(server, tmp_path).run("go")
    assert (tmp_path / "l.txt").read_text() == "z" and r.text == "done"


def test_truncated_tool_call_gets_a_nudge_then_the_turn_continues(server, tmp_path):
    cut = '<tool_call>{"name": "write", "arguments": {"path": "t.txt", "content": "ha'
    server.script(Reply(text=cut, finish_reason="length"), "recovered")
    r = make(server, tmp_path).run("go")
    assert r.stopped == "done" and r.text == "recovered"
    sent = server.requests[1]["body"]["messages"]
    assert "cut off" in sent[-1]["content"] and sent[-1]["role"] == "user"


def test_repeated_truncation_stops_the_turn(server, tmp_path):
    cut = '<tool_call>{"name": "write", "arguments": {"path": "t'
    server.script(*[Reply(text=cut, finish_reason="length")] * 5)
    assert make(server, tmp_path).run("go").stopped == "nudge_limit"


def test_profile_sampling_reaches_the_request(server, tmp_path):
    server.script("ok")
    make(server, tmp_path, model="gemma4:12b").run("hi")
    body = server.requests[0]["body"]
    assert body["temperature"] == 1.0 and body["top_k"] == 64


def test_counters_record_repairs_unknown_calls_and_nudges(server, tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    cut = '<tool_call>{"name": "write", "arguments": {"path": "t'
    server.script(
        Reply(tool_calls=[("read_file", {"file_path": "a.txt"})]),
        Reply(tool_calls=[("teleport", {})]),
        Reply(text=cut, finish_reason="length"),
        "ok",
    )
    agent = make(server, tmp_path)
    agent.run("go")
    assert dict(agent.counters) == {"repaired": 1, "unknown": 1, "nudges": 1, "truncated": 1}
