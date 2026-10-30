"""Tests for `halagent.subagents`: the `subagent` tool runs children with their own context."""

import json

import pytest

from halagent.loop import Agent
from halagent.settings import AgentSettings
from halagent.subagents import MAX_PARALLEL, split_tasks
from halagent.tools import ToolError, Toolset
from halkit.testing.model_server import Reply


def parent(server, tmp_path, mode="auto"):
    a = Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path))
    a.mode = mode
    return a


def tool_text(server, index):
    return [m for m in server.requests[index]["body"]["messages"] if m["role"] == "tool"][0]["content"]


def test_split_tasks_cuts_on_a_line_of_three_dashes_and_caps_the_count():
    assert split_tasks("one\n---\ntwo") == ["one", "two"]
    with pytest.raises(ToolError):
        split_tasks("\n---\n".join("t" for _ in range(MAX_PARALLEL + 1)))
    with pytest.raises(ToolError):
        split_tasks("  ")


def test_subagent_report_comes_back_and_the_child_context_stays_out_of_the_parent(server, tmp_path):
    (tmp_path / "a.txt").write_text("secret-content")
    server.script(
        Reply(tool_calls=[("subagent", {"prompt": "look at a.txt"})]),
        Reply(tool_calls=[("read", {"path": "a.txt"})]),
        "the file holds one secret",
        "parent done",
    )
    a = parent(server, tmp_path)
    assert a.run("delegate").text == "parent done"
    assert "[subagent 1] the file holds one secret" in tool_text(server, 3)
    assert "secret-content" not in json.dumps(a.messages)


def test_read_only_subagent_cannot_write(server, tmp_path):
    server.script(
        Reply(tool_calls=[("subagent", {"prompt": "write a file"})]),
        Reply(tool_calls=[("write", {"path": "n.txt", "content": "x"})]),
        "could not",
        "ok",
    )
    parent(server, tmp_path).run("go")
    assert not (tmp_path / "n.txt").exists()
    child_tools = [t["function"]["name"] for t in server.requests[1]["body"]["tools"]]
    assert "write" not in child_tools and "subagent" not in child_tools


def test_subagent_with_write_tools_is_refused_outside_auto_edit_mode(server, tmp_path):
    server.script(Reply(tool_calls=[("subagent", {"prompt": "x", "tools": "all"})]), "ok")
    parent(server, tmp_path, mode="review").run("go")
    assert "not_allowed" in tool_text(server, 1)


def test_writing_subagent_in_auto_edit_mode_changes_the_workspace(server, tmp_path):
    server.script(
        Reply(tool_calls=[("subagent", {"prompt": "make n.txt", "tools": "all"})]),
        Reply(tool_calls=[("write", {"path": "n.txt", "content": "x"})]),
        "made it",
        "ok",
    )
    parent(server, tmp_path).run("go")
    assert (tmp_path / "n.txt").read_text() == "x"
