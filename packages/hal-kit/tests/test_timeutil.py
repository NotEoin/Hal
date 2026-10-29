"""Tests for `halkit.timeutil`."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from halkit import timeutil


def test_iso_z_formats_a_utc_datetime_with_z():
    assert timeutil.iso_z(datetime(2026, 10, 23, 8, 15, 30, tzinfo=UTC)) == "2026-10-23T08:15:30Z"


def test_iso_z_converts_another_zone_to_utc():
    plus_two = timezone(timedelta(hours=2))
    assert timeutil.iso_z(datetime(2026, 10, 23, 10, 15, 30, tzinfo=plus_two)) == "2026-10-23T08:15:30Z"


def test_iso_z_with_ms_keeps_three_digits():
    stamp = datetime(2026, 10, 23, 8, 15, 30, 7000, tzinfo=UTC)
    assert timeutil.iso_z(stamp, ms=True) == "2026-10-23T08:15:30.007Z"


def test_iso_z_rejects_a_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        timeutil.iso_z(datetime(2026, 10, 23, 8, 15, 30))


def test_iso_z_without_argument_ends_in_z():
    assert timeutil.iso_z().endswith("Z")


def test_utc_now_is_timezone_aware_utc():
    assert timeutil.utc_now().utcoffset() == timedelta(0)


def test_parse_iso_z_reads_z_suffix():
    assert timeutil.parse_iso_z("2026-10-23T08:15:30Z") == datetime(2026, 10, 23, 8, 15, 30, tzinfo=UTC)


def test_parse_iso_z_converts_an_offset_to_utc():
    assert timeutil.parse_iso_z("2026-10-23T10:15:30+02:00") == datetime(2026, 10, 23, 8, 15, 30, tzinfo=UTC)


def test_parse_iso_z_rejects_text_without_a_zone():
    with pytest.raises(ValueError, match="no Z or offset"):
        timeutil.parse_iso_z("2026-10-23T08:15:30")


def test_parse_iso_z_rejects_garbage():
    with pytest.raises(ValueError, match="not an ISO 8601"):
        timeutil.parse_iso_z("yesterday")


def test_iso_z_round_trips_through_parse():
    stamp = timeutil.utc_now().replace(microsecond=0)
    assert timeutil.parse_iso_z(timeutil.iso_z(stamp)) == stamp
