"""Routine grants for routine-defining tools (REQ-HUB-67, 01 REQ-SEC-31, RESOLUTIONS B10).

Integration, hermetic: the synthetic `routines` pack saves a step list through the pipeline, and
a fake routine runner replays the steps unattended through a second `Hub` on the same state
directory, as the routines daemon does from its own process.
"""

from __future__ import annotations

import json

import pytest
from hubkit import ScriptedApprover, SpyRunner, answer

from halhub.hub import RoutineRun
from halkit.testing import FakeClock

STEPS = json.dumps(
    [
        {"tool": "demo_echo", "args": {"text": "good morning"}},
        {"tool": "demo_wipe", "args": {"path": "~/Downloads/old.log"}},
    ]
)


def save(hub, approver, steps: str = STEPS, name: str = "morning"):
    s = hub.open_session(kind="chat", origin="cli", approver=approver)
    return hub.call("routines_save", {"name": name, "steps": steps}, s, front_end="python")


def run_routine(hub, steps: str = STEPS, name: str = "morning") -> list:
    """The fake routines daemon: every step unattended, origin `routine`, with the stored steps."""
    s = hub.open_session(kind="routine", origin="routine")
    results = []
    for step in json.loads(steps):
        results.append(
            hub.call(step["tool"], step["args"], s, front_end="python", routine=RoutineRun(name, steps))
        )
    return results


@pytest.fixture
def hubs(make_hub):
    spy = SpyRunner()
    return make_hub("demo", "routines", runner=spy), make_hub("demo", "routines", runner=spy), spy


def test_saving_a_routine_shows_every_step(hubs):
    hub, _, _ = hubs
    approver = ScriptedApprover(answer("deny"))
    save(hub, approver)
    request = approver.requests[0]
    assert request.content and request.tool == "routines_save"
    assert "step 1: demo_echo  [read]  allow" in request.display
    assert '{"text":"good morning"}' in request.display
    assert "step 2: demo_wipe  [act]  ask" in request.display


def test_approved_save_grants_only_ask_steps(hubs):
    hub, daemon, spy = hubs
    assert save(hub, ScriptedApprover()).status == "ok"
    (view,) = hub.grants.routine_grants()
    assert view.grant.routine_id == "morning" and len(view.grant.step_keys) == 1
    assert [s["tool"] for s in view.steps] == ["demo_echo", "demo_wipe"]
    assert [r.status for r in run_routine(daemon)] == ["ok", "ok"]
    assert [c[0] for c in spy.calls] == ["routines_save", "demo_echo", "demo_wipe"]


def test_edited_steps_do_not_match_grants(hubs):
    hub, daemon, _ = hubs
    save(hub, ScriptedApprover())
    edited = STEPS.replace("~/Downloads/old.log", "~/Downloads/other.log")
    assert [r.rule for r in run_routine(daemon, edited)][1] == "no-approver"
    longer = json.dumps([*json.loads(STEPS), {"tool": "demo_echo", "args": {"text": "x"}}])
    assert run_routine(daemon, longer)[1].rule == "no-approver"
    assert run_routine(daemon, STEPS, name="evening")[1].rule == "no-approver"


def test_unattended_step_without_grant_is_denied(hubs):
    _, daemon, spy = hubs
    echo, wipe = run_routine(daemon)
    assert echo.status == "ok" and (wipe.status, wipe.rule) == ("denied", "no-approver")
    assert [c[0] for c in spy.calls] == ["demo_echo"]


def test_declined_save_creates_no_routine_grant(hubs):
    hub, _, _ = hubs
    save(hub, ScriptedApprover(answer("deny")))
    assert hub.grants.routine_grants() == ()


@pytest.mark.parametrize(
    "steps",
    [
        "not json",
        "[]",
        json.dumps([{"tool": "nope_tool", "args": {}}]),
        json.dumps([{"tool": "demo_echo", "args": {"text": "hi", "loud": True}}]),
        json.dumps([{"tool": "demo_echo", "args": {"text": "hi", "times": 9}}]),
        json.dumps([{"tool": "demo_echo"}, "extra"]),
    ],
)
def test_invalid_step_makes_the_save_invalid_before_any_prompt(hubs, steps):
    hub, _, spy = hubs
    approver = ScriptedApprover()
    r = save(hub, approver, steps)
    assert r.error_type == "invalid_arguments" and r.rule.startswith(
        "bad-args:"
    )  # json: the param's own check
    assert approver.requests == [] and spy.calls == []


def test_single_object_is_a_one_step_list(hubs):
    hub, daemon, _ = hubs
    one = json.dumps({"tool": "demo_wipe", "args": {"path": "~/Downloads/old.log"}})
    save(hub, ScriptedApprover(), one)
    s = daemon.open_session(kind="routine", origin="routine")
    r = daemon.call(
        "demo_wipe",
        {"path": "~/Downloads/old.log"},
        s,
        front_end="python",
        routine=RoutineRun("morning", one),
    )
    assert r.status == "ok"


def test_routine_grant_expires_after_max_days(make_hub):
    clock = FakeClock()
    hub = make_hub("demo", "routines", clock=clock.now)
    save(hub, ScriptedApprover())
    clock.advance(91 * 86400)  # past approval.routine_max_days = 90
    assert run_routine(hub)[1].rule == "no-approver"


def test_routine_grant_file_with_open_permissions_denies_the_call(hubs, hal_home):
    hub, daemon, _ = hubs
    save(hub, ScriptedApprover())
    (hal_home.state / "routine-grants.jsonl").chmod(0o644)
    assert run_routine(daemon)[1].rule == "internal-error"
