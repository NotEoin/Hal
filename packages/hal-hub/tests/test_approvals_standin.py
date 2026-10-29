"""Tests for `halhub._approvals_standin`: grants, the router and display escaping.

Rows of 01-security.md, "The approvals module" and "Display rules", for the parts the stand-in
implements; 01's `test_grants.py` and `test_approval_router.py` replace this file in phase 2.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from halhub._approvals_standin import (
    ApprovalRequest,
    ApprovalRouter,
    Decision,
    GrantStore,
    escape,
    new_grant_id,
    new_request_id,
)
from halhub._policy_standin import Grant, Origin, Scope, Taint, Tier
from halkit.testing import FakeClock

SID = "0123456789ab"
SILENT_CAP_S = 5.0  # a silent surface waits to be withdrawn, never past this


def make_grant(clock: FakeClock, scope: Scope = Scope.ONCE, session: str = SID, ttl: float = 600) -> Grant:
    return Grant(
        id=new_grant_id(),
        scope=scope,
        key="sha256:k",
        pack="demo",
        tool="demo_tool",
        session=session,
        routine_id=None,
        steps_sha256=None,
        step_keys={},
        meta_sha256="sha256:m",
        created=clock.now(),
        expires=clock.now() + timedelta(seconds=ttl),
        used=False,
        source="test",
    )


def make_request(content: bool = False) -> ApprovalRequest:
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
        args={},
        display="demo_tool  [act]  demo",
        summary="Run demo_tool?",
        content=content,
        challenge=None,
        scopes=(Scope.ONCE, Scope.SESSION),
        taint=Taint.UNTRUSTED,
        guard=None,
        created_at="2026-10-23T08:15:30.000Z",
        expires_at="2026-10-23T08:17:30.000Z",
    )


class Surface:
    """A scripted surface: answers with `answer(request)`, or blocks until withdrawn."""

    def __init__(
        self, name: str, answer=None, *, shows_all_args: bool = True, available: bool = True
    ) -> None:
        self.name = name
        self.shows_all_args = shows_all_args
        self._answer = answer
        self._available = available
        self.seen: list[ApprovalRequest] = []
        self.withdrawn: list[str] = []
        self._gone = threading.Event()

    def available(self) -> bool:
        return self._available

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        self.seen.append(request)
        if self._answer is None:
            self._gone.wait(SILENT_CAP_S)
            return Decision(request.id, request.key, "deny", None, self.name)
        return self._answer(request)

    def withdraw(self, request_id: str) -> None:
        self.withdrawn.append(request_id)
        self._gone.set()


def allow(scope: Scope = Scope.ONCE, key: str | None = None):
    return lambda r: Decision(r.id, key or r.key, "allow", scope, "spoofed")


def test_request_and_grant_ids_have_their_prefix_and_16_hex():
    assert new_request_id().startswith("apr_") and len(new_request_id()) == 20
    assert new_grant_id().startswith("g_") and len(new_grant_id()) == 18


def test_once_grant_is_consumed_exactly_once():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    g = make_grant(clock)
    store.add(g)
    assert store.consume(g.id) is True
    assert store.consume(g.id) is False


def test_expired_grant_is_neither_listed_nor_consumed():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    g = make_grant(clock, ttl=10)
    store.add(g)
    clock.advance(11)
    assert store.snapshot(SID) == () and store.consume(g.id) is False


def test_snapshot_lists_only_the_sessions_own_grants():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    mine, other = make_grant(clock), make_grant(clock, session="ffffffffffff")
    store.add(mine)
    store.add(other)
    assert store.snapshot(SID) == (mine,)


def test_end_session_drops_once_and_session_grants():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    store.add(make_grant(clock, Scope.SESSION))
    store.add(make_grant(clock, Scope.ONCE))
    store.end_session(SID)
    assert store.snapshot(SID) == ()


def test_duplicate_grant_id_is_refused():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    g = make_grant(clock)
    store.add(g)
    with pytest.raises(ValueError, match="already exists"):
        store.add(g)


def test_router_returns_the_first_matching_answer_with_the_surface_as_source():
    slow, fast = Surface("slow"), Surface("fast", allow())
    decision = ApprovalRouter([slow, fast]).decide(make_request(), timeout_s=5)
    assert decision.verdict == "allow" and decision.source == "fast"
    assert slow.withdrawn and fast.withdrawn


def test_router_drops_and_audits_an_answer_with_another_key():
    events = []
    router = ApprovalRouter(
        [Surface("liar", allow(key="sha256:other"))], audit=lambda e, **f: events.append((e, f))
    )
    decision = router.decide(make_request(), timeout_s=1)
    assert decision.verdict == "deny"
    assert events[0][0] == "approval_mismatch" and events[0][1]["key_got"] == "sha256:other"


def test_router_times_out_to_deny():
    decision = ApprovalRouter([Surface("silent")]).decide(make_request(), timeout_s=0.1)
    assert decision.verdict == "deny" and decision.source == "timeout"


def test_content_request_skips_surfaces_without_all_args():
    voice = Surface("voice", allow(), shows_all_args=False)
    router = ApprovalRouter([voice])
    assert router.decide(make_request(content=True), timeout_s=1).source == "no-approver"
    assert voice.seen == []


def test_router_is_unavailable_when_no_surface_is():
    assert not ApprovalRouter([Surface("off", available=False)]).available()


def test_failing_surface_counts_as_no_answer():
    def boom(request):
        raise RuntimeError("surface broke")

    decision = ApprovalRouter([Surface("broken", boom)]).decide(make_request(), timeout_s=1)
    assert decision.verdict == "deny"


def test_escape_shows_control_and_bidi_characters_as_code_points():
    text = "a\x1b[31mb" + chr(0x202E) + "c\td\ne"
    assert escape(text) == "a\\u001b[31mb\\u202ec\\td\ne"
    assert escape("x\ny", multiline=False) == "x\\ny"


def _routine_grant(clock: FakeClock) -> Grant:
    return Grant(
        id=new_grant_id(),
        scope=Scope.ROUTINE,
        key=None,
        pack=None,
        tool=None,
        session=None,
        routine_id="morning",
        steps_sha256="ab" * 32,
        step_keys={"sha256:k": "sha256:m"},
        meta_sha256=None,
        created=clock.now(),
        expires=clock.now() + timedelta(days=90),
        used=False,
        source="scripted",
    )


def test_routine_grant_is_persisted_for_another_store(tmp_path):
    clock = FakeClock()
    path = tmp_path / "state" / "routine-grants.jsonl"
    grant = _routine_grant(clock)
    GrantStore(path, clock=clock.now).add(grant, steps=[{"tool": "demo_echo", "args": {}}])
    other = GrantStore(path, clock=clock.now)
    assert other.snapshot("ffffffffffff") == (grant,)
    assert other.routine_grants()[0].steps == ({"tool": "demo_echo", "args": {}},)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_unparsable_routine_grant_line_is_skipped(tmp_path):
    clock = FakeClock()
    path = tmp_path / "routine-grants.jsonl"
    store = GrantStore(path, clock=clock.now)
    store.add(_routine_grant(clock))
    with path.open("a") as fh:
        fh.write('{"op": "grant", "id": "g_broken"}\n')
    assert len(GrantStore(path, clock=clock.now).snapshot(SID)) == 1


def test_routine_grant_needs_a_file():
    with pytest.raises(ValueError, match="no routine grant file"):
        GrantStore(clock=FakeClock().now).add(_routine_grant(FakeClock()))
