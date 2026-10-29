"""Tests for per-session rate limits (`halhub.policy.rate`, REQ-SEC-12), including ATK-26."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from halhub.policy import decide, load_policy
from halhub.policy.model import (
    Context,
    Origin,
    ParamSpec,
    RateSnapshot,
    Taint,
    Tier,
    ToolCall,
    ToolMeta,
    Verdict,
)
from halhub.policy.rate import RateLimiter
from halkit.testing import FakeClock

SID = "0123456789ab"
OTHER = "ba9876543210"
MAIL_SEND = ToolMeta("mail_send", "comms", "0.1.0", Tier.SEND, (ParamSpec("to", required=True),))
NOTE_ADD = ToolMeta("notes_add", "organiser", "0.1.0", Tier.WRITE, (ParamSpec("text", required=True),))
CTX = Context(
    now=datetime(2026, 10, 29, tzinfo=UTC),
    session_kind="chat",
    taint=Taint.CLEAN,
    grants=(),
    rate=RateSnapshot(),
    approver_available=True,
    paths={},
    workspaces=(),
    home=Path("/home/u"),
)


def policy(tmp_path: Path, text: str = ""):
    path = tmp_path / "policy.toml"
    path.write_text("schema_version = 1\n" + text)
    path.chmod(0o600)
    return load_policy(path)


def call(meta: ToolMeta, value: str = "x") -> ToolCall:
    name = meta.params[0].name
    return ToolCall(meta.name, {name: value}, SID, Origin.TUI, "c_" + "0" * 16)


def test_31st_send_in_an_hour_is_denied_atk_26(tmp_path):
    pol, clock = policy(tmp_path), FakeClock()
    rl = RateLimiter(clock=clock.monotonic)
    for _ in range(30):
        rl.record_call(SID, MAIL_SEND)
    d = decide(call(MAIL_SEND), MAIL_SEND, pol, replace(CTX, rate=rl.snapshot(SID, pol, MAIL_SEND)))
    assert (d.verdict, d.rule) == (Verdict.DENY, "rate:send")


def test_41st_ask_in_an_hour_is_denied_atk_26(tmp_path):
    pol, clock = policy(tmp_path), FakeClock()
    rl = RateLimiter(clock=clock.monotonic)
    for _ in range(40):
        rl.record_ask(SID)
    d = decide(call(MAIL_SEND), MAIL_SEND, pol, replace(CTX, rate=rl.snapshot(SID, pol, MAIL_SEND)))
    assert (d.verdict, d.rule) == (Verdict.DENY, "rate:asks")


def test_ask_budget_does_not_block_allowed_calls(tmp_path):
    pol = policy(tmp_path)
    d = decide(call(NOTE_ADD), NOTE_ADD, pol, replace(CTX, rate=RateSnapshot({"asks": 40})))
    assert d.verdict is Verdict.ALLOW


def test_window_slides_so_old_calls_stop_counting(tmp_path):
    pol, clock = policy(tmp_path), FakeClock()
    rl = RateLimiter(clock=clock.monotonic)
    for _ in range(30):
        rl.record_call(SID, MAIL_SEND)
    clock.advance(3600)
    snap = rl.snapshot(SID, pol, MAIL_SEND)
    assert snap.counts["send"] == 0
    assert decide(call(MAIL_SEND), MAIL_SEND, pol, replace(CTX, rate=snap)).rule == "tier:send"


def test_counts_are_kept_per_session(tmp_path):
    pol = policy(tmp_path)
    rl = RateLimiter(clock=FakeClock().monotonic)
    for _ in range(30):
        rl.record_call(SID, MAIL_SEND)
    assert rl.snapshot(OTHER, pol, MAIL_SEND).counts["send"] == 0


def test_tool_rate_limits_that_tool_only(tmp_path):
    pol = policy(tmp_path, "[tools.notes_add]\nrate = '2/m'\n")
    clock = FakeClock()
    rl = RateLimiter(clock=clock.monotonic)
    rl.record_call(SID, NOTE_ADD)
    rl.record_call(SID, NOTE_ADD)
    d = decide(call(NOTE_ADD), NOTE_ADD, pol, replace(CTX, rate=rl.snapshot(SID, pol, NOTE_ADD)))
    assert (d.verdict, d.rule) == (Verdict.DENY, "rate:tool:notes_add")
    clock.advance(61)
    d = decide(call(NOTE_ADD), NOTE_ADD, pol, replace(CTX, rate=rl.snapshot(SID, pol, NOTE_ADD)))
    assert d.verdict is Verdict.ALLOW


def test_session_limit_covers_every_tier(tmp_path):
    pol = policy(tmp_path, "[rate]\nsession = '3/h'\n")
    rl = RateLimiter(clock=FakeClock().monotonic)
    for _ in range(3):
        rl.record_call(SID, NOTE_ADD)
    d = decide(call(NOTE_ADD), NOTE_ADD, pol, replace(CTX, rate=rl.snapshot(SID, pol, NOTE_ADD)))
    assert (d.verdict, d.rule) == (Verdict.DENY, "rate:session")


def test_forgotten_session_starts_from_zero(tmp_path):
    pol = policy(tmp_path)
    rl = RateLimiter(clock=FakeClock().monotonic)
    rl.record_call(SID, MAIL_SEND)
    rl.record_ask(SID)
    rl.forget(SID)
    assert set(rl.snapshot(SID, pol, MAIL_SEND).counts.values()) == {0}
