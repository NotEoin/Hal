"""Tests for `halagent.hooks`: parsing, blocking, notes and lifecycle commands."""

import pytest

from halagent.hooks import Hooks, parse_hooks
from halagent.loop import Agent
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.model_server import Reply


def hooks_in(tmp_path, text, **scripts):
    for name, body in scripts.items():
        (tmp_path / f"{name}.sh").write_text(body)
    (tmp_path / ".hal").mkdir()
    (tmp_path / ".hal" / "hooks.toml").write_text(text)
    return Hooks.load(tmp_path, tmp_path / "no-config")


def agent(server, tmp_path, hooks):
    return Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path), hooks=hooks)


@pytest.mark.parametrize(
    "text",
    [
        '[[hook]]\nevent = "nope"\ncommand = "x"',
        '[[hook]]\nevent = "pre_tool"',
        '[[hook]]\nevent = "pre_tool"\ncommand = "x"\ntimeout = 999',
    ],
)
def test_a_bad_hook_entry_is_rejected_with_its_position(text):
    with pytest.raises(ValueError, match="hooks.toml: hook 1"):
        parse_hooks(text, "hooks.toml")


def test_a_broken_hooks_file_is_skipped_not_fatal(tmp_path):
    assert not hooks_in(tmp_path, "this is = = not toml")


def test_pre_tool_hook_exiting_2_blocks_the_call_and_the_model_sees_the_reason(server, tmp_path):
    hooks = hooks_in(
        tmp_path,
        '[[hook]]\nevent = "pre_tool"\ntools = ["write"]\ncommand = "sh block.sh"',
        block="echo no writes today >&2; exit 2",
    )
    server.script(Reply(tool_calls=[("write", {"path": "n.txt", "content": "x"})]), "ok")
    agent(server, tmp_path, hooks).run("write")
    assert not (tmp_path / "n.txt").exists()
    tool = [m for m in server.requests[1]["body"]["messages"] if m["role"] == "tool"][0]["content"]
    assert "hook_blocked" in tool and "no writes today" in tool


def test_pre_tool_hook_for_another_tool_does_not_block(server, tmp_path):
    hooks = hooks_in(
        tmp_path, '[[hook]]\nevent = "pre_tool"\ntools = ["execute"]\ncommand = "sh block.sh"', block="exit 2"
    )
    server.script(Reply(tool_calls=[("write", {"path": "n.txt", "content": "x"})]), "ok")
    agent(server, tmp_path, hooks).run("write")
    assert (tmp_path / "n.txt").read_text() == "x"


def test_failing_post_tool_hook_appends_a_note_to_the_result(server, tmp_path):
    hooks = hooks_in(
        tmp_path, '[[hook]]\nevent = "post_tool"\ncommand = "sh lint.sh"', lint="echo lint failed >&2; exit 1"
    )
    server.script(Reply(tool_calls=[("write", {"path": "n.txt", "content": "x"})]), "ok")
    agent(server, tmp_path, hooks).run("write")
    tool = [m for m in server.requests[1]["body"]["messages"] if m["role"] == "tool"][0]["content"]
    assert "lint failed" in tool and (tmp_path / "n.txt").exists()


def test_hook_receives_the_call_as_json_on_stdin(server, tmp_path):
    hooks = hooks_in(tmp_path, '[[hook]]\nevent = "pre_tool"\ncommand = "sh seen.sh"', seen="cat > seen.json")
    server.script(Reply(tool_calls=[("write", {"path": "n.txt", "content": "x"})]), "ok")
    agent(server, tmp_path, hooks).run("write")
    assert '"tool": "write"' in (tmp_path / "seen.json").read_text()


def test_session_start_and_stop_hooks_run_once_each(server, tmp_path):
    hooks = hooks_in(
        tmp_path,
        '[[hook]]\nevent = "session_start"\ncommand = "sh s.sh"\n'
        '[[hook]]\nevent = "session_stop"\ncommand = "sh e.sh"',
        s="echo s >> log",
        e="echo e >> log",
    )
    a = agent(server, tmp_path, hooks)
    a.close()
    a.close()
    assert (tmp_path / "log").read_text().split() == ["s", "e"]


def test_a_timed_out_hook_does_not_block(tmp_path):
    hooks = hooks_in(
        tmp_path, '[[hook]]\nevent = "pre_tool"\ncommand = "sh slow.sh"\ntimeout = 1', slow="sleep 5; exit 2"
    )
    assert hooks.pre_tool("write", {}) is None


def test_hooks_in_settings_toml_are_loaded(tmp_path):
    (tmp_path / ".hal").mkdir()
    (tmp_path / ".hal" / "settings.toml").write_text('[[hook]]\nevent = "pre_tool"\ncommand = "true"\n')
    assert len(Hooks.load(tmp_path, tmp_path / "no-config").hooks) == 1


def test_a_failing_user_prompt_hook_refuses_the_prompt_with_its_message(server, tmp_path):
    hooks = hooks_in(
        tmp_path,
        '[[hook]]\nevent = "user_prompt_submit"\ncommand = "sh deny.sh"\n',
        deny="echo no secrets in prompts >&2; exit 2\n",
    )
    a = agent(server, tmp_path, hooks)
    result = a.run("my password is hunter2")
    assert result.stopped == "blocked"
    assert "no secrets in prompts" in result.text
    assert not server.requests
