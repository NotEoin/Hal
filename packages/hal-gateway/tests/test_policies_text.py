"""Tests for `policies.text` and the effort policy (REQ-GW-41), in both wire formats."""

import json

import pytest

from halgateway.policies import RequestContext, run_rewrites
from halgateway.policies.effort import EffortPolicy
from halgateway.policies.text import conversation_key, decision_point

REMINDER = "<system-reminder>synthetic context</system-reminder>"


def a_use(name, tool_id="t1", **inp):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}


def a_result(text, tool_id="t1", error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": text}
    if error:
        block["is_error"] = True
    return {"role": "user", "content": [block]}


def o_use(name, tool_id="t1", **args):
    call = {"id": tool_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return {"role": "assistant", "content": None, "tool_calls": [call]}


def o_result(text, tool_id="t1"):
    return {"role": "tool", "tool_call_id": tool_id, "content": text}


USER = {"role": "user", "content": "fix the bug"}

# (case, anthropic messages, openai messages, expected reason)
CASES = [
    ("user text", [USER], [USER], "user message"),
    ("reminder only", [{"role": "user", "content": REMINDER}], [{"role": "user", "content": REMINDER}], ""),
    (
        "subagent report",
        [USER, a_use("Task"), a_result("done")],
        [USER, o_use("Task"), o_result("done")],
        "subagent report",
    ),
    (
        "agent message text",
        [{"role": "user", "content": "<agent-message from=x>hi</agent-message>"}],
        [{"role": "user", "content": "<agent-message from=x>hi</agent-message>"}],
        "subagent report",
    ),
    (
        "failing bash",
        [USER, a_use("Bash", command="pytest"), a_result("Traceback (most recent call last):\n  x")],
        [USER, o_use("Bash", command="pytest"), o_result("Traceback (most recent call last):\n  x")],
        "failing command",
    ),
    (
        "exit code 1 of a build",
        [USER, a_use("Bash", command="make"), a_result("Exit code 1")],
        [USER, o_use("Bash", command="make"), o_result("Exit code 1")],
        "failing command",
    ),
    (
        "exit code 1 of a search",
        [USER, a_use("Bash", command="cat f | grep x"), a_result("Exit code 1")],
        [USER, o_use("Bash", command="cat f | grep x"), o_result("Exit code 1")],
        "",
    ),
    (
        "plain tool result",
        [USER, a_use("Read", path="f"), a_result("ValueError: in source code")],
        [USER, o_use("Read", path="f"), o_result("ValueError: in source code")],
        "",
    ),
]


@pytest.mark.parametrize(("fmt", "index"), [("anthropic", 1), ("openai", 2)])
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_decision_points(case, fmt, index):
    assert decision_point(case[index], fmt) == case[3]


def test_anthropic_tool_error_is_a_decision_point_and_openai_has_no_such_flag():
    messages = [USER, a_use("Read", path="f"), a_result("denied", error=True)]
    assert decision_point(messages, "anthropic") == "tool error"


def test_system_messages_are_skipped_when_finding_the_last_turn():
    assert decision_point([USER, {"role": "system", "content": "note"}], "openai") == "user message"


def test_conversation_key_depends_on_the_first_message_only():
    one = conversation_key([USER, {"role": "assistant", "content": "a"}])
    assert one == conversation_key([USER]) != conversation_key([{"role": "user", "content": "other"}])


@pytest.mark.parametrize("fmt", ["anthropic", "openai"])
def test_effort_note_is_appended_at_a_decision_point_only(fmt):
    policy = EffortPolicy("NOTE")
    ctx = RequestContext(fmt, {"tools": [{"name": "Bash"}], "messages": [USER]}, "hal/main")
    run_rewrites([policy], ctx)
    assert ctx.body["messages"][-1] == {"role": "system", "content": "NOTE"}
    assert ctx.decisions == {"effort": "xhigh (user message)"}
    routine = RequestContext(
        fmt, {"tools": [1], "messages": [USER, a_use("Read"), a_result("ok")]}, "hal/main"
    )
    run_rewrites([policy], routine)
    assert len(routine.body["messages"]) == 3 and routine.decisions == {"effort": "medium"}


def test_effort_leaves_a_request_without_tools_unchanged():
    ctx = RequestContext("openai", {"messages": [USER]}, "hal/main")
    run_rewrites([EffortPolicy("NOTE")], ctx)
    assert ctx.body == {"messages": [USER]} and ctx.decisions == {}


def test_a_failing_policy_is_skipped_and_logged_as_an_error():
    class Broken:
        name = "broken"

        def rewrite(self, ctx):
            raise KeyError("x")

    ctx = RequestContext("openai", {"messages": [USER]}, "a")
    run_rewrites([Broken()], ctx)
    assert ctx.decisions == {"broken": "error: KeyError"}
