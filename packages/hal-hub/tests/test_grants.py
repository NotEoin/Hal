"""Tests for approval grants and the grant store (`halhub.approvals.grants`, REQ-SEC-20/21/28/31/99)."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from halhub.approvals.grants import (
    GrantError,
    GrantStore,
    UnsafeGrantFile,
    new_grant_id,
    once_grant,
    routine_grant,
    session_grant,
)
from halhub.approvals.keys import approval_key, security_digest
from halhub.policy import PROFILES, decide
from halhub.policy.model import (
    ApprovalSettings,
    Context,
    Origin,
    ParamSpec,
    Policy,
    RateSnapshot,
    Taint,
    Tier,
    ToolCall,
    ToolMeta,
    Verdict,
)
from halkit.testing import FakeClock

SID = "0123456789ab"
OTHER = "ba9876543210"
SETTINGS = ApprovalSettings()
DEFAULT = Policy(profile=PROFILES["default"])
MAIL = ToolMeta(
    "mail_send",
    "comms",
    "0.1.0",
    Tier.SEND,
    (ParamSpec("to", required=True), ParamSpec("body", content=True)),
)
ARGS_A = {"to": "ann@example.org", "body": "hi"}
KEY_A = approval_key("comms", "mail_send", ARGS_A)


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


def grant_once(clock: FakeClock, **kw):
    return once_grant(
        key=KEY_A, meta=MAIL, session=SID, settings=SETTINGS, now=clock.now(), source="tui", **kw
    )


def grant_routine(clock: FakeClock, routine_id: str = "r1", settings: ApprovalSettings = SETTINGS):
    return routine_grant(
        routine_id=routine_id,
        steps_sha256="ab" * 32,
        step_keys={KEY_A: security_digest(MAIL)},
        settings=settings,
        now=clock.now(),
        source="tui",
    )


STEPS = ({"tool": "mail_send", "tier": "send", "args": ARGS_A},)


def ctx(clock: FakeClock, store: GrantStore, session: str = SID, **kw) -> Context:
    return Context(
        now=clock.now(),
        session_kind="chat",
        taint=Taint.CLEAN,
        grants=store.snapshot(session),
        rate=RateSnapshot(),
        approver_available=True,
        paths={},
        workspaces=(),
        home=Path("/home/u"),
        **kw,
    )


def mail_call(args=ARGS_A, session=SID, **kw):
    return ToolCall("mail_send", args, session, kw.pop("origin", Origin.TUI), "c_" + "0" * 16, **kw)


def test_grant_ids_have_their_prefix_and_16_hex():
    gid = new_grant_id()
    assert gid.startswith("g_") and len(gid) == 18
    int(gid[2:], 16)


def test_grant_for_args_a_does_not_cover_args_b():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    store.add(
        session_grant(key=KEY_A, meta=MAIL, session=SID, settings=SETTINGS, now=clock.now(), source="tui")
    )
    assert decide(mail_call(), MAIL, DEFAULT, ctx(clock, store)).rule.startswith("grant:")
    for other in (
        {**ARGS_A, "to": "ann@example.org "},
        {**ARGS_A, "to": "Ann@example.org"},
        {**ARGS_A, "body": "hi!"},
    ):
        d = decide(mail_call(other), MAIL, DEFAULT, ctx(clock, store))
        assert (d.verdict, d.rule) == (Verdict.ASK, "tier:send"), other


def test_once_grant_is_consumed_exactly_once():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    g = grant_once(clock)
    store.add(g)
    assert store.consume(g.id) is True
    assert store.consume(g.id) is False
    assert decide(mail_call(), MAIL, DEFAULT, ctx(clock, store)).verdict is Verdict.ASK


def test_once_grant_expires_after_once_ttl():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    g = grant_once(clock)
    store.add(g)
    clock.advance(SETTINGS.once_ttl_s)
    assert store.snapshot(SID) == ()
    assert store.consume(g.id) is False


def test_session_grant_is_valid_only_in_its_session():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    store.add(
        session_grant(key=KEY_A, meta=MAIL, session=SID, settings=SETTINGS, now=clock.now(), source="tui")
    )
    assert store.snapshot(OTHER) == ()
    d = decide(mail_call(session=OTHER), MAIL, DEFAULT, ctx(clock, store, OTHER))
    assert d.verdict is Verdict.ASK


def test_session_grant_expires_after_session_max_hours():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    store.add(
        session_grant(key=KEY_A, meta=MAIL, session=SID, settings=SETTINGS, now=clock.now(), source="tui")
    )
    clock.advance(SETTINGS.session_max_h * 3600 - 1)
    assert len(store.snapshot(SID)) == 1
    clock.advance(1)
    assert store.snapshot(SID) == ()


def test_routine_grant_expires_after_max_days(tmp_path):
    clock = FakeClock()
    settings = ApprovalSettings(routine_max_days=7)
    store = GrantStore(tmp_path / "state" / "routine-grants.jsonl", clock=clock.now, settings=settings)
    store.add(grant_routine(clock, settings=settings), steps=STEPS)
    clock.advance(7 * 86400 - 1)
    assert len(store.routine_grants()) == 1
    clock.advance(1)
    assert store.routine_grants() == ()


@pytest.mark.parametrize(
    ("scope_grant", "extra"),
    [
        ("once", timedelta(seconds=1)),
        ("session", timedelta(seconds=1)),
        ("routine", timedelta(seconds=1)),
    ],
)
def test_grant_longer_than_its_bound_is_refused(tmp_path, scope_grant, extra):
    clock = FakeClock()
    store = GrantStore(tmp_path / "routine-grants.jsonl", clock=clock.now)
    g = {
        "once": grant_once(clock),
        "session": session_grant(
            key=KEY_A, meta=MAIL, session=SID, settings=SETTINGS, now=clock.now(), source="t"
        ),
        "routine": grant_routine(clock),
    }[scope_grant]
    with pytest.raises(GrantError, match="longer"):
        store.add(replace(g, expires=g.expires + extra))


def test_grant_missing_its_binding_is_refused():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    with pytest.raises(GrantError, match="needs"):
        store.add(replace(grant_once(clock), meta_sha256=None))


def test_duplicate_grant_id_is_refused():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    g = grant_once(clock)
    store.add(g)
    with pytest.raises(GrantError, match="already exists"):
        store.add(g)


def test_grant_stops_matching_when_tool_metadata_changes():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    store.add(
        session_grant(key=KEY_A, meta=MAIL, session=SID, settings=SETTINGS, now=clock.now(), source="tui")
    )
    changed = replace(
        MAIL, params=(ParamSpec("to", required=True), ParamSpec("body", content=True, max_length=10))
    )
    assert decide(mail_call(), changed, DEFAULT, ctx(clock, store)).verdict is Verdict.ASK
    renamed_version = replace(MAIL, pack_version="0.2.0")
    assert decide(mail_call(), renamed_version, DEFAULT, ctx(clock, store)).verdict is Verdict.ALLOW


def test_session_end_drops_once_and_session_grants():
    clock = FakeClock()
    store = GrantStore(clock=clock.now)
    store.add(grant_once(clock))
    store.add(
        session_grant(key=KEY_A, meta=MAIL, session=SID, settings=SETTINGS, now=clock.now(), source="tui")
    )
    store.end_session(SID)
    assert store.snapshot(SID) == ()


def test_routine_grant_is_persisted_for_another_store(tmp_path):
    clock = FakeClock()
    path = tmp_path / "state" / "routine-grants.jsonl"
    g = grant_routine(clock)
    GrantStore(path, clock=clock.now).add(g, steps=STEPS)
    assert (path.stat().st_mode & 0o777) == 0o600
    other = GrantStore(path, clock=clock.now)
    assert [v.grant for v in other.routine_grants()] == [g]
    call = mail_call(origin=Origin.ROUTINE, routine_id="r1")
    d = decide(call, MAIL, DEFAULT, ctx(clock, other, routine_steps_sha256="ab" * 32))
    assert d.rule == f"grant:{g.id}"


def test_list_routine_grants_shows_steps(tmp_path):
    clock = FakeClock()
    store = GrantStore(tmp_path / "routine-grants.jsonl", clock=clock.now)
    store.add(grant_routine(clock), steps=STEPS)
    (view,) = store.routine_grants()
    assert view.grant.routine_id == "r1"
    assert view.steps == STEPS


def test_revoke_one_routine_grant(tmp_path):
    clock, sink = FakeClock(), Sink()
    path = tmp_path / "routine-grants.jsonl"
    store = GrantStore(path, clock=clock.now, audit=sink)
    keep, drop = grant_routine(clock, "r1"), grant_routine(clock, "r2")
    store.add(keep, steps=STEPS)
    store.add(drop, steps=STEPS)
    assert store.revoke(drop.id, by="cli") is True
    assert store.revoke(drop.id, by="cli") is False
    assert [v.grant.id for v in GrantStore(path, clock=clock.now).routine_grants()] == [keep.id]
    assert sink.events == [("grant_revoked", {"by": "cli", "grant": drop.id})]


def test_routine_edit_revokes_every_grant_of_that_routine(tmp_path):
    clock, sink = FakeClock(), Sink()
    store = GrantStore(tmp_path / "routine-grants.jsonl", clock=clock.now, audit=sink)
    store.add(grant_routine(clock, "r1"), steps=STEPS)
    store.add(grant_routine(clock, "r1"), steps=STEPS)
    store.add(grant_routine(clock, "r2"), steps=STEPS)
    assert store.revoke_routine("r1", by="routines") == 2
    assert [v.grant.routine_id for v in store.routine_grants()] == ["r2"]
    assert sink.events[-1] == ("grant_revoked", {"by": "routines", "routine_id": "r1", "count": 2})


def test_regrant_after_routine_revoke_is_live(tmp_path):
    clock = FakeClock()
    path = tmp_path / "routine-grants.jsonl"
    store = GrantStore(path, clock=clock.now)
    store.add(grant_routine(clock, "r1"), steps=STEPS)
    store.revoke_routine("r1", by="routines")
    again = grant_routine(clock, "r1")
    store.add(again, steps=STEPS)
    assert [v.grant.id for v in GrantStore(path, clock=clock.now).routine_grants()] == [again.id]


def test_file_is_compacted_when_most_lines_are_dead(tmp_path):
    clock = FakeClock()
    path = tmp_path / "routine-grants.jsonl"
    store = GrantStore(path, clock=clock.now)
    keep = grant_routine(clock, "keep")
    store.add(keep, steps=STEPS)
    for i in range(3):
        g = grant_routine(clock, f"r{i}")
        store.add(g, steps=STEPS)
        store.revoke(g.id, by="cli")
    lines = path.read_text().splitlines()
    assert len(lines) <= 2
    assert json.loads(lines[0])["id"] == keep.id
    assert [v.grant.id for v in GrantStore(path, clock=clock.now).routine_grants()] == [keep.id]


def test_unparsable_routine_grant_line_is_skipped(tmp_path):
    clock = FakeClock()
    path = tmp_path / "routine-grants.jsonl"
    store = GrantStore(path, clock=clock.now)
    g = grant_routine(clock)
    store.add(g, steps=STEPS)
    with path.open("a") as fh:
        fh.write("{not json\n")
    assert [v.grant for v in GrantStore(path, clock=clock.now).routine_grants()] == [g]


def test_routine_grant_file_with_open_permissions_is_refused(tmp_path):
    clock = FakeClock()
    path = tmp_path / "routine-grants.jsonl"
    GrantStore(path, clock=clock.now).add(grant_routine(clock), steps=STEPS)
    os.chmod(path, 0o644)
    store = GrantStore(path, clock=clock.now)
    with pytest.raises(UnsafeGrantFile):
        store.check_file()
    with pytest.raises(UnsafeGrantFile):
        store.snapshot(SID)


def test_routine_grant_needs_a_file():
    clock = FakeClock()
    with pytest.raises(GrantError, match="no routine grant file"):
        GrantStore(clock=clock.now).add(grant_routine(clock), steps=STEPS)
