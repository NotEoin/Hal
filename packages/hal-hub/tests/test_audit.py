"""Tests for the audit log (`halhub.audit`, REQ-SEC-39 to REQ-SEC-44, REQ-SEC-48).

The per-front-end cases of REQ-SEC-39 are in `test_audit_sink.py`; this file covers the
writer, `tail`, `prune` and `verify`.
"""

from __future__ import annotations

import json
import stat

import pytest

from halhub.audit import (
    MAX_RECORD_BYTES,
    MAX_STRING_CHARS,
    AuditLog,
    AuditUnavailable,
    default_directory,
    summary,
    tail,
    verify,
)
from halhub.policy.model import AuditSettings
from halkit.testing import FakeClock

SID = "0123456789ab"


def test_record_has_common_fields_and_goes_to_the_utc_day_file(hal_home):
    AuditLog(clock=FakeClock().now).append("decision", session=SID, tool="demo_echo")
    record = json.loads((default_directory() / "2026-10-23.jsonl").read_text())
    assert record["v"] == 1 and record["ts"] == "2026-10-23T08:15:30.000Z"
    assert record["event"] == "decision" and record["tool"] == "demo_echo"
    assert isinstance(record["pid"], int) and isinstance(record["seq"], int)
    assert {k: record[k] for k in ("origin", "remote_addr", "channel")} == dict.fromkeys(
        ("origin", "remote_addr", "channel")
    )


def test_audit_files_are_private(hal_home):
    AuditLog().append("decision")
    directory = default_directory()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.glob("*.jsonl"))


def test_known_secret_values_and_secret_keys_are_redacted(hal_home):
    AuditLog(secrets=lambda: ["hunter2-canary"]).append(
        "decision", args={"to": "a@b.org", "smtpPass": "x", "q": "url with hunter2-canary in it"}
    )
    args = next(tail())["args"]
    assert args == {"to": "a@b.org", "smtpPass": "[redacted]", "q": "url with [redacted] in it"}


def test_long_strings_are_cut_after_redaction(hal_home):
    secret = "s3cret-canary-value"
    text = "x" * (MAX_STRING_CHARS - 5) + secret + "y" * 100
    AuditLog(secrets=lambda: [secret]).append("review", reason=text)
    reason = next(tail())["reason"]
    assert secret not in reason and "s3cr" not in reason
    assert reason.endswith(f"[+{len(text) - len(secret) + len('[redacted]') - MAX_STRING_CHARS} chars]")


def test_oversized_record_replaces_args_with_a_summary(hal_home):
    args = {f"p{i}": "z" * 4000 for i in range(30)}
    AuditLog().append("decision", session=SID, call="c_1", args=args)
    record = next(tail())
    assert set(record["args"]) == {"chars", "sha256"}
    assert record["session"] == SID and record["call"] == "c_1"
    line = (default_directory()).glob("*.jsonl").__next__().read_text()
    assert len(line) <= MAX_RECORD_BYTES + 1


def test_oversized_record_without_args_keeps_its_envelope(hal_home):
    AuditLog().append("approval_request", call="c_1", surfaces=["s" * 4000] * 30)
    record = next(tail())
    assert (
        record["call"] == "c_1"
        and "surfaces" not in record
        and set(record["oversize"]) == {"chars", "sha256"}
    )


def test_summary_is_length_and_digest():
    assert summary("abc") == {
        "chars": 3,
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    }


def test_unwritable_directory_raises_audit_unavailable(hal_home, tmp_path):
    blocked = tmp_path / "audit"
    blocked.write_text("a file where the directory should be")
    with pytest.raises(AuditUnavailable):
        AuditLog(blocked).append("decision")
    assert issubclass(AuditUnavailable, OSError)


def test_failed_record_leaves_a_gap_that_verify_reports(hal_home, tmp_path):
    good = AuditLog()
    good.append("decision")
    blocked = tmp_path / "audit"
    blocked.write_text("not a directory")
    with pytest.raises(AuditUnavailable):
        AuditLog(blocked).append("result")
    good.append("decision")
    first, second = (r["seq"] for r in tail())
    assert second == first + 2
    (problem,) = verify()
    assert "seq gap" in problem


def test_clean_log_verifies(hal_home):
    log = AuditLog()
    log.append("approval_request", key="sha256:k")
    log.append("approval", key="sha256:k", verdict="allow")
    log.append("approval", key="sha256:k", verdict="deny")
    assert verify() == []


def test_allow_without_a_matching_request_is_reported(hal_home):
    log = AuditLog()
    log.append("approval_request", key="sha256:a")
    log.append("approval", key="sha256:b", verdict="allow")
    (problem,) = verify()
    assert "no approval_request" in problem


def test_bad_lines_are_reported_and_skipped_by_tail(hal_home):
    AuditLog(clock=FakeClock().now).append("decision")
    path = default_directory() / "2026-10-23.jsonl"
    with path.open("a") as fh:
        fh.write("{broken\n")
        fh.write('{"v":1,"ts":"2026-10-23 08:15","pid":1,"seq":1,"event":"decision"}\n')
        fh.write('{"v":1,"ts":"2026-10-23T08:15:30.000Z","pid":1,"seq":2,"event":"bogus"}\n')
    problems = verify(date="2026-10-23")
    assert len(problems) == 3
    assert (
        "not a JSON record" in problems[0] and "timestamp" in problems[1] and "unknown event" in problems[2]
    )
    assert len(list(tail(n=0))) == 3
    assert verify(date="2026-10-24") == []


def test_tail_filters_by_session_and_reads_rotated_files_in_order(hal_home):
    log = AuditLog(settings=AuditSettings(max_file_mb=1))
    log.append("decision", session="aaaaaaaaaaaa")
    day = next(default_directory().glob("*.jsonl"))
    day.rename(day.with_name(day.name + ".1"))  # what rotation leaves behind
    log.append("decision", session="bbbbbbbbbbbb")
    assert [r["session"] for r in tail()] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    assert [r["session"] for r in tail(session="bbbbbbbbbbbb")] == ["bbbbbbbbbbbb"]


def test_prune_deletes_files_past_retention(hal_home):
    clock = FakeClock()
    log = AuditLog(settings=AuditSettings(retention_days=2), clock=clock.now)
    log.append("decision")
    old = default_directory() / "2026-10-23.jsonl"
    old.with_name(old.name + ".1").write_text("")
    clock.advance(3 * 86400)
    log.append("decision")
    assert log.prune() == 2
    assert sorted(p.name for p in default_directory().iterdir()) == [
        "2026-10-26.jsonl",
        "2026-10-26.jsonl.lock",
    ]
