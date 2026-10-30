"""Tests for `halagent.anthropic`: message conversion and the Messages round trip on a fake server."""

import json

import pytest

from halagent.anthropic import AnthropicProvider, convert_messages
from halagent.loop import Agent
from halagent.provider import make_provider
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.model_server import Reply

HISTORY = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "read a.txt"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c1", "function": {"name": "read", "arguments": '{"path": "a.txt"}'}}],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "hello"},
    {"role": "user", "content": "thanks"},
]  # fmt: skip


def test_history_converts_to_system_tool_use_and_tool_result_blocks():
    system, msgs = convert_messages(HISTORY)
    assert system == "be brief"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == [
        {"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "a.txt"}}
    ]
    assert msgs[2]["content"][0] == {"type": "tool_result", "tool_use_id": "c1", "content": "hello"}
    assert msgs[2]["content"][1] == {"type": "text", "text": "thanks"}


def test_unparseable_call_arguments_become_an_empty_input():
    bad = {
        "role": "assistant",
        "content": "x",
        "tool_calls": [{"id": "c", "function": {"name": "n", "arguments": "{"}}],
    }
    assert convert_messages([bad])[1][0]["content"][1]["input"] == {}


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError, match="role"):
        convert_messages([{"role": "narrator", "content": "x"}])


def test_stream_is_refused_for_anthropic():
    with pytest.raises(ValueError, match="stream"):
        AnthropicProvider("http://127.0.0.1:1", "m", stream=True)


def test_unsupported_api_name_is_refused():
    with pytest.raises(ValueError, match="grpc"):
        make_provider(AgentSettings(endpoint="http://127.0.0.1:1", api="grpc"))


def test_agent_runs_a_tool_turn_over_the_messages_api(server, tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    server.script(Reply(tool_calls=[("read", {"path": "a.txt"})]), "it says hello")
    agent = Agent(AgentSettings(endpoint=server.url, api="anthropic"), Toolset(tmp_path))
    result = agent.run("read a.txt")
    assert (result.text, result.stopped, result.tool_calls) == ("it says hello", "done", 1)
    paths = [r["path"] for r in server.requests if r["method"] == "POST"]
    assert paths == ["/v1/messages", "/v1/messages"]
    body = server.requests[-1]["body"]
    assert body["max_tokens"] > 0 and body["system"] and "tools" in body
    assert body["messages"][-1]["content"][0]["type"] == "tool_result"
    assert json.dumps(body).count("hello") >= 1
