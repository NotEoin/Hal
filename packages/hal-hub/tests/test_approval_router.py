"""Tests for approval requests, answers and the router (`halhub.approvals`, REQ-SEC-22/23/30/94)."""

from __future__ import annotations

import threading

import pytest
from hubkit import FakeSurface

from halhub.approvals import (
    VOICE_MAX_CHARS,
    ApprovalRequest,
    ApprovalRouter,
    Decision,
    decision_from_json,
    decision_json,
    new_challenge,
    new_request_id,
    request_json,
)
from halhub.policy.model import Origin, Scope, Taint, Tier

SID = "0123456789ab"


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


def make_request(
    *, content: bool = False, display: str = "demo_tool  [act]  demo", scopes=None
) -> ApprovalRequest:
    return ApprovalRequest(
        id=new_request_id(),
        session=SID,
        origin=Origin.CLI,
        channel=None,
        pack="demo",
        pack_version="0.1.0",
        tool="demo_tool",
        tier=Tier.ACT,
        key="sha256:k",
        reason="tier:act",
        args={"x": "1"},
        display=display,
        summary="Run demo_tool?",
        content=content,
        challenge=None,
        scopes=scopes or (Scope.ONCE, Scope.SESSION),
        taint=Taint.UNTRUSTED,
        guard=None,
        created_at="2026-10-23T08:15:30.000Z",
        expires_at="2026-10-23T08:17:30.000Z",
    )


def allow(scope: Scope = Scope.ONCE, key: str | None = None):
    return lambda r: Decision(r.id, key or r.key, "allow", scope, "spoofed")


def test_request_ids_have_their_prefix_and_16_hex():
    rid = new_request_id()
    assert rid.startswith("apr_") and len(rid) == 20
    int(rid[4:], 16)


def test_challenge_is_two_digits():
    for _ in range(50):
        c = new_challenge()
        assert len(c) == 2 and c.isdigit()


def test_router_returns_the_first_matching_answer_with_the_surface_as_source():
    slow, fast = FakeSurface("slow"), FakeSurface("fast", allow())
    decision = ApprovalRouter([slow, fast]).decide(make_request(), timeout_s=5)
    assert (decision.verdict, decision.source) == ("allow", "fast")
    assert slow.withdrawn and fast.withdrawn


def test_router_drops_and_audits_an_answer_with_another_key():
    sink = Sink()
    router = ApprovalRouter([FakeSurface("liar", allow(key="sha256:other"))], audit=sink)
    decision = router.decide(make_request(), timeout_s=1)
    assert decision.verdict == "deny"
    assert sink.events[0][0] == "approval_mismatch"
    assert sink.events[0][1]["key_got"] == "sha256:other"


def test_router_drops_an_answer_for_another_request_id():
    other = lambda r: Decision("apr_" + "0" * 16, r.key, "allow", Scope.ONCE, "x")  # noqa: E731 - a one-line answer
    assert ApprovalRouter([FakeSurface("x", other)]).decide(make_request(), timeout_s=1).verdict == "deny"


def test_router_times_out_to_deny():
    decision = ApprovalRouter([FakeSurface("silent")]).decide(make_request(), timeout_s=0.1)
    assert (decision.verdict, decision.source) == ("deny", "timeout")


def test_router_is_unavailable_when_no_surface_is():
    router = ApprovalRouter([FakeSurface("off", available=False)])
    assert not router.available()
    assert router.decide(make_request(), timeout_s=1).source == "no-approver"


def test_failing_surface_counts_as_no_answer():
    def boom(request):
        raise RuntimeError("surface broke")

    assert ApprovalRouter([FakeSurface("broken", boom)]).decide(make_request(), timeout_s=1).verdict == "deny"


def test_surface_timeout_deny_does_not_beat_another_surface():
    def own_timeout(request):
        return Decision(request.id, request.key, "deny", None, "timeout")

    def late_allow(request):
        threading.Event().wait(0.2)
        return allow()(request)

    router = ApprovalRouter([FakeSurface("quick", own_timeout), FakeSurface("person", late_allow)])
    assert router.decide(make_request(), timeout_s=2).source == "person"


def test_content_call_never_reaches_voice_or_notification():
    voice = FakeSurface("voice", allow(), shows_all_args=False)
    notify = FakeSurface("notify", allow(), shows_all_args=False)
    tui = FakeSurface("tui", allow())
    decision = ApprovalRouter([voice, notify, tui]).decide(make_request(content=True), timeout_s=1)
    assert decision.source == "tui"
    assert voice.seen == [] and notify.seen == []


def test_long_display_never_reaches_voice():
    voice = FakeSurface("voice", allow(), shows_all_args=False)
    request = make_request(display="x" * (VOICE_MAX_CHARS + 1))
    assert ApprovalRouter([voice]).decide(request, timeout_s=1).source == "no-approver"
    assert voice.seen == []


def test_limited_surfaces_are_offered_only_once_scope():
    notify = FakeSurface("notify", allow(Scope.ONCE), shows_all_args=False)
    decision = ApprovalRouter([notify]).decide(make_request(), timeout_s=1)
    assert decision.verdict == "allow"
    assert notify.seen[0].scopes == (Scope.ONCE,)


def test_session_answer_from_a_limited_surface_is_dropped():
    sink = Sink()
    notify = FakeSurface("notify", allow(Scope.SESSION), shows_all_args=False)
    decision = ApprovalRouter([notify], audit=sink).decide(make_request(), timeout_s=1)
    assert decision.verdict == "deny"
    assert sink.events[0][0] == "approval_mismatch"


def test_scope_the_request_did_not_offer_is_dropped():
    tui = FakeSurface("tui", allow(Scope.SESSION))
    assert ApprovalRouter([tui]).decide(make_request(scopes=(Scope.ONCE,)), timeout_s=1).verdict == "deny"


def test_voice_copy_carries_a_challenge_and_the_others_do_not():
    voice = FakeSurface("voice", shows_all_args=False)
    tui = FakeSurface("tui", allow())
    ApprovalRouter([voice, tui]).decide(make_request(), timeout_s=1)
    assert tui.seen[0].challenge is None
    assert voice.seen[0].challenge is not None and len(voice.seen[0].challenge) == 2


def test_request_json_matches_the_wire_shape():
    request = make_request()
    obj = request_json(request)
    assert obj["type"] == "approval_request" and obj["v"] == 1
    assert obj["scopes"] == ["once", "session"] and obj["taint"] == "untrusted"
    assert obj["origin"] == "cli" and obj["tier"] == "act"


def test_decision_round_trips_and_source_comes_from_the_host():
    d = Decision("apr_" + "1" * 16, "sha256:k", "allow", Scope.SESSION, "tui", "ok")
    wire = {**decision_json(d), "source": "reviewer"}
    assert decision_from_json(wire, source="control") == Decision(
        d.request_id, d.key, "allow", Scope.SESSION, "control", "ok"
    )


@pytest.mark.parametrize(
    "wire",
    [
        {"request_id": "apr_x", "key": "k", "verdict": "yes"},
        {"request_id": "apr_x", "key": "k", "verdict": "allow", "scope": "always"},
        {"request_id": "apr_x", "key": "k", "verdict": "allow"},
        {"request_id": 1, "key": "k", "verdict": "deny"},
    ],
)
def test_malformed_or_always_answers_are_refused(wire):
    with pytest.raises(ValueError):
        decision_from_json(wire, source="control")
