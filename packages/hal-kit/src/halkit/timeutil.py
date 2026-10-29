"""UTC timestamps in one format: ISO 8601 with a trailing `Z` (REQ-SYS-14).

Every machine-readable timestamp in Hal goes through this module, so no other module needs
`datetime.now()` or `strftime`. Naive datetimes are rejected rather than guessed at, because
a silent local-time assumption corrupts logs across a daylight saving change.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """The current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def iso_z(dt: datetime | None = None, *, ms: bool = False) -> str:
    """Formats `dt` (default: now) as `2026-10-23T08:15:30Z`, with milliseconds when `ms`.

    A datetime in another zone is converted to UTC. Raises `ValueError` for a naive datetime.
    """
    if dt is None:
        dt = utc_now()
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("iso_z needs a timezone-aware datetime")
    dt = dt.astimezone(UTC)
    if ms:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_z(text: str) -> datetime:
    """Parses an ISO 8601 timestamp that carries `Z` or an explicit offset; returns UTC.

    Raises `ValueError` for text without a zone, so a naive local time is never accepted.
    """
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not an ISO 8601 timestamp: {text!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp has no Z or offset: {text!r}")
    return parsed.astimezone(UTC)
