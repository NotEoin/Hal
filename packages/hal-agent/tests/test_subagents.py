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


def write_agent(tmp_path, name, text):
    folder = tmp_path / ".hal" / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(text)


def test_agent_files_and_builtins_are_discovered_and_a_bad_file_is_skipped(tmp_path):
    from halagent.subagents import discover_agents

    write_agent(
        tmp_path, "docs", "---\ndescription: Writes docs\ntools: read, write\nmax_turns: 5\n---\nBe brief."
    )
    write_agent(tmp_path, "broken", "no frontmatter here")
    found = discover_agents(tmp_path)
    assert set(found) == {"explore", "review", "docs"}
    assert found["docs"].writable and found["docs"].max_turns == 5 and found["docs"].prompt == "Be brief."


def test_tool_description_lists_the_named_agents(server, tmp_path):
    write_agent(tmp_path, "docs", "---\ndescription: Writes docs\n---\nBe brief.")
    a = parent(server, tmp_path)
    assert "docs: Writes docs" in a.toolset.tools["subagent"].description


def test_named_agent_gets_its_instructions_and_only_its_tools(server, tmp_path):
    write_agent(tmp_path, "finder", "---\ndescription: Finds\ntools: grep\n---\nAlways answer in French.")
    server.script(
        Reply(tool_calls=[("subagent", {"prompt": "find x", "agent": "finder"})]), "trouve", "parent done"
    )
    a = parent(server, tmp_path)
    assert a.run("delegate").text == "parent done"
    child = server.requests[1]["body"]
    assert "Always answer in French." in child["messages"][0]["content"]
    assert [t["function"]["name"] for t in child["tools"]] == ["grep"]


def test_unknown_agent_name_is_an_error_listing_the_known_ones(server, tmp_path):
    a = parent(server, tmp_path)
    with pytest.raises(ToolError, match="explore"):
        a.toolset.tools["subagent"].fn({"prompt": "x", "agent": "nope"})


def test_review_agent_cannot_commit(server, tmp_path):
    server.script(
        Reply(tool_calls=[("subagent", {"prompt": "review", "agent": "review"})]),
        Reply(tool_calls=[("git", {"action": "commit", "message": "x"})]),
        "reviewed",
        "parent done",
    )
    a = parent(server, tmp_path)
    a.run("delegate")
    assert "git commit is not allowed" in tool_text(server, 2)


def test_children_stop_at_the_gateway_slot_count_and_report_progress(server, tmp_path):
    updates = []
    server.script(Reply(tool_calls=[("subagent", {"prompt": "a\n---\nb"})]), "ra", "rb", "done")
    a = parent(server, tmp_path)
    a.on_update = updates.append
    a.model_slots = 1
    a.run("go")
    phases = [(u["id"], u["phase"]) for u in updates if u["kind"] == "subagent"]
    assert phases.index((1, "done")) < phases.index((2, "start")) or phases.index((2, "done")) < phases.index(
        (1, "start")
    )
