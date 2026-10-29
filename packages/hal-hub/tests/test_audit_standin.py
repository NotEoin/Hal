"""Tests for `halhub._audit_standin`: the record format, file modes, `seq` gaps and failures.

Rows of 01-security.md, "Audit log", for the parts the stand-in implements; 01's
`test_audit.py` replaces this file in phase 2.
"""

from __future__ import annotations

import json
import stat

import pytest

from halhub._audit_standin import AuditLog, AuditUnavailable, default_directory, tail
from halkit.testing import FakeClock


def test_record_has_common_fields_and_goes_to_the_utc_day_file(hal_home):
    log = AuditLog(clock=FakeClock().now)
    log.append("decision", session="0123456789ab", tool="demo_echo")
    path = default_directory() / "2026-10-23.jsonl"
    record = json.loads(path.read_text())
    assert record["v"] == 1 and record["ts"] == "2026-10-23T08:15:30.000Z"
    assert record["event"] == "decision" and record["tool"] == "demo_echo"
    assert isinstance(record["pid"], int) and isinstance(record["seq"], int)


def test_audit_files_are_private(hal_home):
    AuditLog().append("decision")
    directory = default_directory()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.glob("*.jsonl"))


def test_known_secret_values_are_redacted(hal_home):
    AuditLog(secrets=lambda: ["hunter2-canary"]).append("result", message="token hunter2-canary used")
    record = next(tail())
    assert record["message"] == "token [redacted] used"


def test_unwritable_directory_raises_audit_unavailable(hal_home, tmp_path):
    blocked = tmp_path / "audit"
    blocked.write_text("a file where the directory should be")
    with pytest.raises(AuditUnavailable):
        AuditLog(blocked).append("decision")


def test_failed_record_leaves_a_gap_in_seq(hal_home, tmp_path):
    good = AuditLog()
    good.append("decision")
    blocked = tmp_path / "audit"
    blocked.write_text("not a directory")
    with pytest.raises(AuditUnavailable):
        AuditLog(blocked).append("result")
    good.append("decision")
    first, second = (r["seq"] for r in tail())
    assert second == first + 2


def test_tail_filters_by_session(hal_home):
    log = AuditLog()
    log.append("decision", session="aaaaaaaaaaaa")
    log.append("decision", session="bbbbbbbbbbbb")
    assert [r["session"] for r in tail(session="bbbbbbbbbbbb")] == ["bbbbbbbbbbbb"]
