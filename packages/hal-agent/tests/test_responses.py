"""Tests for `halagent.responses`: input conversion and the Responses round trip on a fake server."""

import pytest

from halagent.loop import Agent
from halagent.responses import ResponsesProvider, convert_messages
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.model_server import Reply

HISTORY = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "read a.txt"},
    {
        "role": "assistant",
        "content": "ok",
        "tool_calls": [{"id": "c1", "function": {"name": "read", "arguments": '{"path": "a.txt"}'}}],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "hello"},
]  # fmt: skip


def test_history_converts_to_instructions_and_input_items():
    instructions, items = convert_messages(HISTORY)
    assert instructions == "be brief"
    assert items[0] == {"role": "user", "content": "read a.txt"}
    assert items[1] == {"role": "assistant", "content": "ok"}
    assert items[2]["type"] == "function_call" and items[2]["call_id"] == "c1"
    assert items[3] == {"type": "function_call_output", "call_id": "c1", "output": "hello"}


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError, match="role"):
        convert_messages([{"role": "narrator", "content": "x"}])


def test_stream_is_refused_for_responses():
    with pytest.raises(ValueError, match="stream"):
        ResponsesProvider("http://127.0.0.1:1", "m", stream=True)


def test_incomplete_reply_maps_to_length():
    data = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}
    assert ResponsesProvider._parse(data).finish == "length"


def test_agent_runs_a_tool_turn_over_the_responses_api(server, tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    server.script(Reply(tool_calls=[("read", {"path": "a.txt"})]), "it says hello")
    agent = Agent(AgentSettings(endpoint=server.url, api="responses"), Toolset(tmp_path))
    result = agent.run("read a.txt")
    assert (result.text, result.stopped, result.tool_calls) == ("it says hello", "done", 1)
    posts = [r for r in server.requests if r["method"] == "POST"]
    assert [r["path"] for r in posts] == ["/v1/responses", "/v1/responses"]
    body = posts[-1]["body"]
    assert body["store"] is False and body["max_output_tokens"] > 0 and body["instructions"]
    assert body["input"][-1]["type"] == "function_call_output"
    assert body["tools"][0]["type"] == "function" and "name" in body["tools"][0]
