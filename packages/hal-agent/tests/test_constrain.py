"""Tests for `halagent.constrain`: grammar and schema for text-fallback tool calls."""

import pytest

from halagent.constrain import request_keys, tool_grammar, tool_schema, unwrap_answer
from halagent.loop import Agent
from halagent.profiles import Profile, resolve_profile
from halagent.settings import AgentSettings
from halagent.tools import Toolset


def agent_with(server, tmp_path, constraint):
    base = resolve_profile("some-model")
    profile = Profile("some-model", "default", {**base.values, "tool_constraint": constraint}, {})
    settings = AgentSettings(endpoint=server.url, tool_format="prompt")
    return Agent(settings, Toolset(tmp_path), profile=profile)


def test_grammar_lists_tool_names_in_sorted_order():
    g = tool_grammar(["write", "read"])
    assert 'name ::= "\\"read\\"" | "\\"write\\""' in g and g.startswith("root ::= call | answer")


def test_schema_enumerates_names_and_allows_an_answer():
    s = tool_schema(["b", "a"])
    assert s["anyOf"][0]["properties"]["name"]["enum"] == ["a", "b"]


def test_unknown_mode_fails_loudly():
    with pytest.raises(ValueError, match="tool_constraint"):
        request_keys("regex", ["read"])


def test_unwrap_answer_only_unwraps_a_lone_answer_key():
    assert unwrap_answer('{"answer": "hi"}') == "hi"
    assert unwrap_answer('{"answer": "hi", "x": 1}') == '{"answer": "hi", "x": 1}'
    assert unwrap_answer("plain") == "plain"


def test_grammar_mode_sends_the_same_grammar_every_turn(server, tmp_path):
    server.script("one", "two")
    a = agent_with(server, tmp_path, "grammar")
    a.run("first")
    a.run("second")
    g1, g2 = (r["body"]["grammar"] for r in server.requests[-2:])
    assert g1 == g2 and "read" in g1


def test_json_schema_mode_sets_response_format_and_unwraps_the_answer(server, tmp_path):
    server.script('{"answer": "all done"}')
    result = agent_with(server, tmp_path, "json_schema").run("go")
    assert server.requests[-1]["body"]["response_format"]["type"] == "json_schema"
    assert result.text == "all done"


def test_no_constraint_keys_without_the_profile_key(server, tmp_path):
    server.script("ok")
    agent_with(server, tmp_path, "none").run("go")
    body = server.requests[-1]["body"]
    assert "grammar" not in body and "response_format" not in body
