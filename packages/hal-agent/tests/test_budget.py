"""Tests for `halagent.budget` and the loop's budgets, watchdog and nudges."""

import json

from halagent.budget import Budget, BudgetClock, Watchdog, canonical
from halagent.loop import Agent
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.model_server import Reply

BUDGET = Budget(max_tool_calls=20, max_model_requests=50, max_wall_s=900)


def agent(server, tmp_path, task=False, **kw):
    return Agent(AgentSettings(endpoint=server.url, **kw), Toolset(tmp_path, task_mode=task))


def glob_call(pattern="*"):
    return Reply(tool_calls=[("glob", {"pattern": pattern})])


def test_canonical_key_ignores_argument_order_and_json_spacing():
    assert canonical("run", '{"b": 1, "a": 2}') == canonical("run", {"a": 2, "b": 1})


def test_watchdog_refuses_third_and_stops_at_fourth_identical_call():
    dog = Watchdog(BUDGET)
    assert [dog.observe("k") for _ in range(4)] == ["run", "run", "refuse", "stop"]
    assert dog.observe("other") == "run"


def test_watchdog_counts_consecutive_errors_only():
    dog = Watchdog(BUDGET)
    assert [dog.result(True) for _ in range(5)] == [False] * 5
    assert dog.result(False) is False and dog.errors == 0
    assert [dog.result(True) for _ in range(6)][-1] is True


def test_budget_warning_appears_near_the_end_of_calls_or_time():
    now = [0.0]
    clock = BudgetClock(Budget(30, 50, 900), lambda: now[0])
    assert clock.warning_line() == ""
    clock.calls_used = 21
    assert "9 tool calls" in clock.warning_line()
    clock.calls_used = 0
    now[0] = 850.0
    assert "0 min left" in clock.warning_line()


def test_third_identical_call_is_refused_and_fourth_ends_the_turn(server, tmp_path):
    server.script(*[glob_call()] * 5)
    r = agent(server, tmp_path).run("go")
    assert r.stopped == "loop"
    results = [m["content"] for m in server.requests[-1]["body"]["messages"] if m["role"] == "tool"]
    assert json.loads(results[2])["error_type"] == "loop"


def test_six_error_results_in_a_row_end_the_turn(server, tmp_path):
    server.script(*[Reply(tool_calls=[("read", {"path": f"missing{n}"})]) for n in range(8)])
    assert agent(server, tmp_path).run("go").stopped == "tool_errors"


def test_tool_call_limit_leaves_every_call_answered(server, tmp_path):
    calls = Reply(tool_calls=[("glob", {"pattern": "a"}), ("glob", {"pattern": "b"})])
    server.script(calls, Reply(tool_calls=[("glob", {"pattern": "c"})]))
    a = agent(server, tmp_path, max_tool_calls=2)
    assert a.run("go").stopped == "max_tool_calls"
    ids = [c["id"] for m in a.messages for c in m.get("tool_calls", [])]
    answered = [m["tool_call_id"] for m in a.messages if m["role"] == "tool"]
    assert ids == answered


def test_empty_reply_is_nudged_then_answered(server, tmp_path):
    server.script("", "the answer")
    r = agent(server, tmp_path).run("go")
    assert r.text == "the answer"
    assert "[hal] Your last reply had no text" in server.requests[1]["body"]["messages"][-1]["content"]


def test_task_mode_text_reply_gets_the_finish_nudge_and_three_end_the_turn(server, tmp_path):
    server.script("done?", "done?", "done?")
    r = agent(server, tmp_path, task=True).run("go")
    assert r.stopped == "nudge_limit" and len(server.requests) == 3
    assert "call finish" in server.requests[1]["body"]["messages"][-1]["content"]


def test_wall_clock_limit_ends_the_turn(server, tmp_path, monkeypatch):
    import halagent.loop as loop

    ticks = iter([0.0] + [10_000.0] * 20)
    monkeypatch.setattr(loop, "BudgetClock", lambda budget: BudgetClock(budget, lambda: next(ticks)))
    server.script("never asked")
    assert agent(server, tmp_path).run("go").stopped == "max_time"


def test_usage_update_reports_tokens(server, tmp_path):
    server.script(Reply(text="hi", prompt_tokens=12, completion_tokens=4))
    seen: list[dict] = []
    a = Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path), on_update=seen.append)
    a.run("go")
    usage = {
        "kind": "usage", "prompt_tokens": 12, "completion_tokens": 4, "window": 32768,
        "context_used": 16, "tokens_per_s": 8.0,
    }  # fmt: skip
    assert usage in seen
