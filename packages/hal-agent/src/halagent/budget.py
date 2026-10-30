"""Turn budgets, the repeat-call watchdog and the fixed nudge texts.

Everything here is enforced by the loop in code, never by prompt text alone (REQ-AGT-23): a
model that loops on one command stops following prompt rules, so the harness stops it.
The clock is injectable so tests need no sleeping.
"""

from __future__ import annotations

import json
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

NUDGES_FILE = Path(__file__).with_name("prompts") / "nudges.toml"
WARN_SECONDS = 90.0  # the gemma prompt's "submit when <90 s remain" (03-agent 3.5.4)
FREE_TOOLS = frozenset({"todo", "finish", "hal_find_tools"})  # planning and ending must not cost budget


@dataclass(frozen=True)
class Budget:
    """Limits of one turn; zero or negative values are rejected by the settings loader."""

    max_tool_calls: int
    max_model_requests: int
    max_wall_s: float
    warn_calls: int = 10
    warn_seconds: float = WARN_SECONDS
    max_calls_per_response: int = 8
    max_consecutive_nudges: int = 3  # the gemma harness ends a task after 3 nudges
    loop_repeat_limit: int = 3  # the third identical call is refused
    max_consecutive_tool_errors: int = 6


class BudgetClock:
    """Tracks one turn against a `Budget`."""

    def __init__(self, budget: Budget, now: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget
        self._now = now
        self._start = now()
        self.calls_used = 0

    @property
    def seconds_left(self) -> float:
        """Wall-clock seconds left in the turn, never negative."""
        return max(0.0, self.budget.max_wall_s - (self._now() - self._start))

    @property
    def calls_left(self) -> int:
        """Tool calls left in the turn."""
        return max(0, self.budget.max_tool_calls - self.calls_used)

    @property
    def warning(self) -> bool:
        """True when calls or time are nearly used up."""
        return self.calls_left <= self.budget.warn_calls or self.seconds_left <= self.budget.warn_seconds

    def warning_line(self) -> str:
        """The `[hal] budget_warning:` line that ends a tool result, or an empty string."""
        if not self.warning:
            return ""
        minutes = int(self.seconds_left // 60)
        return (
            f"\n[hal] budget_warning: {self.calls_left} tool calls and {minutes} min left. "
            "Start no new exploration: finish the change you have."
        )


def canonical(name: str, raw_args: object) -> str:
    """A stable key for a call: the name plus its arguments as sorted compact JSON."""
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError:
            return f"{name}:{raw_args.strip()}"
    return f"{name}:{json.dumps(raw_args, sort_keys=True, separators=(',', ':'))}"


class Watchdog:
    """Counts consecutive identical calls and consecutive error results."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self._last = ""
        self.repeats = 0
        self.errors = 0

    def observe(self, key: str) -> str:
        """Records a call; returns "run", "refuse" (3rd identical) or "stop" (4th identical)."""
        self.repeats = self.repeats + 1 if key == self._last else 1
        self._last = key
        if self.repeats > self.budget.loop_repeat_limit:
            return "stop"
        return "refuse" if self.repeats == self.budget.loop_repeat_limit else "run"

    def result(self, failed: bool) -> bool:
        """Records a result; True when too many errors came in a row."""
        self.errors = self.errors + 1 if failed else 0
        return self.errors >= self.budget.max_consecutive_tool_errors


def loop_message(name: str, raw_args: object, count: int) -> str:
    """The JSON error returned instead of running a call made `count` times in a row."""
    shown = json.dumps(raw_args if not isinstance(raw_args, str) else raw_args)[:200]
    text = (
        f"You have made this exact call {count} times in a row ({name} {shown}). It did not run "
        "again: the result will not change. Try something different: read the code again, or "
        "change the command."
    )
    return json.dumps({"status": "error", "error_type": "loop", "message": text})


def nudge_text(case: str) -> str:
    """The fixed nudge text for `case` (empty, truncated or finish)."""
    with NUDGES_FILE.open("rb") as fh:
        return str(tomllib.load(fh)[case])
