"""Per-session sliding-window rate counts for the policy (01-security REQ-SEC-12).

`RateLimiter` is the stateful half: the host records each call that ran (`record_call`) and
each approval prompt raised (`record_ask`), and before every decision takes a `RateSnapshot`
whose counts are measured over each scope's own window. `decide` stays pure and compares the
snapshot with the limits (`check_rate`, and the `rate:asks` check before a prompt).

Scopes: `session` (every call), `asks` (prompts), the tiers `act`, `send` and `system`, and
`tool:<name>` for a tool with its own `rate` in `policy.toml`. Exceeding one gives `deny` with
rule `rate:<scope>`. Counts live in memory only: a broker restart starts the windows again,
which is acceptable because every limit is an hour or a day and the audit log keeps the record.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from .model import RATE_UNITS, Policy, RateSnapshot, Tier, ToolMeta, ToolPolicy

LIMITED_TIERS = (Tier.ACT, Tier.SEND, Tier.SYSTEM)  # the tiers with a [rate] key
MAX_WINDOW_S = RATE_UNITS["d"]  # the longest window the rate syntax can express


class RateLimiter:
    """Timestamps of calls and prompts per session and scope; safe to share between threads."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def _add(self, session: str, scope: str) -> None:
        now = self._clock()
        events = self._events.setdefault((session, scope), deque())
        events.append(now)
        while events and events[0] <= now - MAX_WINDOW_S:
            events.popleft()

    def record_call(self, session: str, meta: ToolMeta) -> None:
        """Counts one call of `meta` that ran in `session`."""
        with self._lock:
            self._add(session, "session")
            if meta.tier in LIMITED_TIERS:
                self._add(session, meta.tier.value)
            self._add(session, f"tool:{meta.name}")

    def record_ask(self, session: str) -> None:
        """Counts one approval prompt raised in `session`."""
        with self._lock:
            self._add(session, "asks")

    def _count(self, session: str, scope: str, window_s: int, now: float) -> int:
        events = self._events.get((session, scope), ())
        return sum(1 for t in events if t > now - window_s)

    def snapshot(self, session: str, policy: Policy, meta: ToolMeta | None = None) -> RateSnapshot:
        """The counts `decide` needs for a call in `session`, each over its scope's window."""
        limits = policy.rate
        with self._lock:
            now = self._clock()
            counts = {
                "session": self._count(session, "session", limits.session.window_s, now),
                "asks": self._count(session, "asks", limits.asks.window_s, now),
            }
            for tier in LIMITED_TIERS:
                counts[tier.value] = self._count(
                    session, tier.value, getattr(limits, tier.value).window_s, now
                )
            if meta is not None:
                tool_rate = policy.tool(meta).rate
                if tool_rate is not None:
                    scope = f"tool:{meta.name}"
                    counts[scope] = self._count(session, scope, tool_rate.window_s, now)
        return RateSnapshot(counts)

    def forget(self, session: str) -> None:
        """Drops a finished session's counts."""
        with self._lock:
            for key in [k for k in self._events if k[0] == session]:
                del self._events[key]


def check_rate(meta: ToolMeta, tp: ToolPolicy, policy: Policy, rate: RateSnapshot) -> str | None:
    """The `rate:<scope>` rule this call would exceed, or None (pure; prompts are checked apart)."""
    limits = policy.rate
    if rate.counts.get("session", 0) >= limits.session.count:
        return "rate:session"
    if (
        meta.tier in LIMITED_TIERS
        and rate.counts.get(meta.tier.value, 0) >= getattr(limits, meta.tier.value).count
    ):
        return f"rate:{meta.tier.value}"
    if tp.rate is not None and rate.counts.get(f"tool:{meta.name}", 0) >= tp.rate.count:
        return f"rate:tool:{meta.name}"
    return None
