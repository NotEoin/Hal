"""Tests for model roles: `[agent.models]`, the fast model, `/model` and the architect/editor split."""

from dataclasses import replace

import pytest

from halagent.loop import Agent
from halagent.sessions import SessionStore
from halagent.settings import AgentSettings, load_settings
from halagent.tools import Toolset
from halkit.testing.model_server import Reply


def settings(server, **roles):
    return AgentSettings(endpoint=server.url, model="big", models=roles)


def test_agent_models_table_is_read_from_the_config_file(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[agent]\nendpoint = "http://x"\n[agent.models]\nmain = "qwen3-coder:30b"\nfast = "gemma"\n'
    )
    s = load_settings(path=cfg, environ={})
    assert s.model == "qwen3-coder:30b" and s.models == {"main": "qwen3-coder:30b", "fast": "gemma"}


def test_switch_model_accepts_a_role_name(server, tmp_path):
    a = Agent(settings(server, fast="small", editor="ed"), Toolset(tmp_path))
    a.switch_model("fast")
    assert a.provider.model == "small"
    a.switch_model("main")
    assert a.provider.model == "big"


def test_fast_model_summarises_when_compacting(server, tmp_path):
    a = Agent(settings(server, fast="small"), Toolset(tmp_path))
    server.script("first answer", "a summary of the work")
    a.run("do a thing")
    a.compact()
    assert [r["body"]["model"] for r in server.requests] == ["big", "small"]


def test_fast_model_writes_the_session_title_after_the_first_turn(server, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    from halagent import boot

    writer = boot.open_writer(store, "", tmp_path, settings(server, fast="small"), "")
    a = Agent(settings(server, fast="small"), Toolset(tmp_path), session=writer)
    server.script("answer", "Fix the login bug")
    a.run("please look at why login fails")
    assert server.requests[-1]["body"]["model"] == "small"
    assert any(i.title == "Fix the login bug" for i in store.list())


def test_split_turn_plans_with_the_architect_then_edits_with_the_editor(server, tmp_path):
    a = Agent(settings(server, architect="arch", editor="ed"), Toolset(tmp_path))
    a.split = True
    server.script(
        "1. write hello.txt",
        Reply(tool_calls=[("write", {"path": "hello.txt", "content": "hi"})]),
        "done",
    )
    assert a.run("make a greeting").text == "done"
    assert [r["body"]["model"] for r in server.requests] == ["arch", "ed", "ed"]
    assert (tmp_path / "hello.txt").read_text() == "hi"
    planner_tools = {t["function"]["name"] for t in server.requests[0]["body"]["tools"]}
    assert "write" not in planner_tools
    assert "1. write hello.txt" in server.requests[1]["body"]["messages"][-1]["content"]
    assert a.provider.model == "big"  # back on the main model


def test_split_without_both_roles_fails_loudly(server, tmp_path):
    a = Agent(settings(server, architect="arch"), Toolset(tmp_path))
    a.split = True
    with pytest.raises(ValueError, match="architect and editor"):
        a.run("x")


def test_unconfigured_roles_leave_the_main_model_alone(server, tmp_path):
    a = Agent(replace(settings(server), models={}), Toolset(tmp_path))
    server.script("ok")
    a.run("hi")
    assert server.requests[-1]["body"]["model"] == "big"
