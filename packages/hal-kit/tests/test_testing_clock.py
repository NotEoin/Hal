"""Tests for `halkit.testing.clock`."""

from datetime import UTC, datetime, timedelta

import pytest

from halkit.testing import FakeClock


def test_clock_starts_at_the_default_instant():
    assert FakeClock().now() == datetime(2026, 10, 23, 8, 15, 30, tzinfo=UTC)


def test_advance_moves_now_time_and_monotonic_together():
    clock = FakeClock()
    start = clock.time()
    clock.advance(90)
    assert clock.now() == datetime(2026, 10, 23, 8, 17, 0, tzinfo=UTC)
    assert clock.time() == start + 90
    assert clock.monotonic() == 90


def test_sleep_advances_without_waiting():
    clock = FakeClock()
    clock.sleep(3600)
    assert clock.now() - datetime(2026, 10, 23, 8, 15, 30, tzinfo=UTC) == timedelta(hours=1)


def test_clock_refuses_to_run_backwards():
    with pytest.raises(ValueError, match="backwards"):
        FakeClock().advance(-1)


def test_clock_needs_a_timezone_aware_start():
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 1, 1))
