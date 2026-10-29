"""Session taint: `clean`, `untrusted` or `flagged`, and the rules that move it (REQ-SEC-32).

01-security "Taint, guard and reviewer" owns the design. One `TaintTracker` per session, held
by the hub's session object (and so by the broker and `hal-hub mcp`); `decide` reads its level
through `Context.taint`.

Design decisions:

- The tracker only computes levels and returns each change as a `TaintChange`; the caller
  writes the `taint_change` audit record. Keeping the audit out of here keeps the rules pure
  and lets a failed record never undo a raise (raising is the safe direction).
- Sessions of kind `code`, `box`, `remote` and `hub` start `untrusted`; every other kind
  starts `clean`. A resumed session (after a broker restart) starts `untrusted` whatever its
  kind, because nothing records what the human last saw (F5); a later human message still
  resets it to its kind's starting level.
- A human message resets taint to the starting level, except from the `headless` and
  `routine` surfaces (they record text but are not a person reading the turn, REQ-SEC-93) and
  except `voice` on a `flagged` session (audio played by a page or another person can speak).
- The tracker is thread-safe: the hub's pipeline and a front end's human-message hook may run
  on different threads.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal, get_args

from .model import Taint

UNTRUSTED_START_KINDS = frozenset({"code", "box", "remote", "hub"})  # REQ-SEC-32
RECORD_ONLY_SURFACES = frozenset({"headless", "routine"})  # REQ-SEC-93: no taint reset
Surface = Literal["tui", "acp", "telegram", "voice", "console", "headless", "routine"]
SURFACES = frozenset(get_args(Surface))  # `Broker.on_human_message`'s surfaces
Cause = Literal["untrusted_output", "guard_flag", "guard_error", "human_message"]


@dataclass(frozen=True)
class TaintChange:
    """One change of level, for the caller's `taint_change` audit record."""

    before: Taint
    after: Taint
    cause: str
    surface: str | None = None


def start_level(kind: str, *, resumed: bool = False) -> Taint:
    """The level a session of `kind` starts at; `resumed` sessions start `untrusted` (F5)."""
    if resumed or kind in UNTRUSTED_START_KINDS:
        return Taint.UNTRUSTED
    return Taint.CLEAN


class TaintTracker:
    """One session's taint level; raises never lower it, a human message resets it."""

    def __init__(self, kind: str, *, resumed: bool = False) -> None:
        self.reset_level = start_level(kind)
        self._level = start_level(kind, resumed=resumed)
        self._lock = threading.Lock()

    @property
    def level(self) -> Taint:
        """The level now."""
        with self._lock:
            return self._level

    def raise_to(self, level: Taint, *, cause: str) -> TaintChange | None:
        """Raises the level to at least `level`; returns the change, or None when it stays."""
        with self._lock:
            before = self._level
            if level <= before:
                return None
            self._level = level
        return TaintChange(before, level, cause)

    def human_message(self, surface: str) -> TaintChange | None:
        """Applies a human message from `surface`; returns the change, or None when it stays.

        Raises `ValueError` for a surface that is not one of `SURFACES`, so a typo in a front
        end cannot reset taint by accident.
        """
        if surface not in SURFACES:
            raise ValueError(f"unknown surface {surface!r}; expected one of {', '.join(sorted(SURFACES))}")
        with self._lock:
            before = self._level
            if surface in RECORD_ONLY_SURFACES or (surface == "voice" and before is Taint.FLAGGED):
                return None
            self._level = self.reset_level
        if self.reset_level == before:
            return None
        return TaintChange(before, self.reset_level, "human_message", surface)
