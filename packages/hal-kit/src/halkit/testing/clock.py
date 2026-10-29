"""A manual clock for tests of expiry, rate limits and timeouts, so none of them sleep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

DEFAULT_START = datetime(2026, 10, 23, 8, 15, 30, tzinfo=UTC)  # the date the specs were verified on


class FakeClock:
    """Time that moves only when the test says so.

    `now`, `time` and `monotonic` agree with each other, and `sleep` advances the clock
    instead of waiting, so code that takes a `clock` or `sleep` argument runs instantly.
    """

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start")
        self._start = start.astimezone(UTC)
        self._elapsed = 0.0

    def advance(self, seconds: float) -> None:
        """Moves time forward; raises `ValueError` for a negative step."""
        if seconds < 0:
            raise ValueError("a clock does not run backwards")
        self._elapsed += seconds

    def sleep(self, seconds: float) -> None:
        """Same as `advance`, shaped like `time.sleep`."""
        self.advance(seconds)

    def now(self) -> datetime:
        """The current time as an aware UTC datetime."""
        return self._start + timedelta(seconds=self._elapsed)

    def time(self) -> float:
        """Seconds since the epoch, like `time.time`."""
        return self.now().timestamp()

    def monotonic(self) -> float:
        """Seconds since the clock was created, like `time.monotonic`."""
        return self._elapsed
