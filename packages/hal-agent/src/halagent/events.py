"""Turn events: what happened during an agent turn (design 03, 3.2.3).

The loop emits `Event` objects; the ACP server, the headless renderer and the transcript
writer all consume them. Each type has required `data` fields, checked in `make_event`, so a
malformed event fails where it is built and not in a renderer. Display caps live here so every
consumer cuts text the same way.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from halkit.timeutil import iso_z

EventType = Literal[
    "turn_start",
    "text",
    "reasoning",
    "tool_call",
    "tool_start",
    "tool_result",
    "plan",
    "usage",
    "compaction",
    "notice",
    "permission",
    "turn_end",
]

PROMPT_DISPLAY_CAP = 2_000  # characters of the prompt kept in turn_start
RESULT_DISPLAY_CAP = 4_000  # characters of tool output kept in tool_result
DIFF_MAX_BYTES = 64 * 1024  # larger files carry no diff in tool_result

# Required `data` keys per event type (design 03, 3.2.3 table).
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "turn_start": ("prompt", "mode", "budget"),
    "text": ("delta", "message_id"),
    "reasoning": ("delta", "message_id"),
    "tool_call": ("id", "name", "arguments", "kind", "title", "locations", "repair"),
    "tool_start": ("id",),
    "tool_result": ("id", "ok", "error_type", "title", "text", "diff", "data"),
    "plan": ("entries",),
    "usage": (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "estimated",
        "context_used",
        "context_size",
    ),
    "compaction": ("id", "kind", "status", "before_tokens", "after_tokens", "summary"),
    "notice": ("level", "code", "text"),
    "permission": ("id", "decision"),
    "turn_end": ("stop_reason", "final_text", "stats"),
}

PLAN_STATUSES = ("pending", "in_progress", "completed")
PLAN_PRIORITIES = ("high", "medium", "low")
NOTICE_LEVELS = ("info", "warning", "error")
PERMISSION_DECISIONS = (
    "allow_once",
    "allow_always",
    "reject_once",
    "reject_always",
    "cancelled",
)


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened during a turn; JSON-serialisable through `to_json()`."""

    type: EventType
    session: str
    turn: int
    data: dict[str, Any]
    ts: str  # UTC ISO 8601 with Z

    def to_json(self) -> dict[str, Any]:
        """Plain-dict form."""
        return {
            "type": self.type,
            "session": self.session,
            "turn": self.turn,
            "data": self.data,
            "ts": self.ts,
        }

    def dumps(self) -> str:
        """One compact JSON line, ASCII-escaped."""
        return json.dumps(self.to_json(), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Event:
        """Rebuild and validate an event from `to_json()` output."""
        return make_event(raw["type"], raw["session"], int(raw["turn"]), raw["data"], ts=raw["ts"])


def cap_text(text: str, limit: int) -> str:
    """Cut `text` to `limit` characters, marking the cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[cut]"


def make_event(
    type_: str, session: str, turn: int, data: Mapping[str, Any], *, ts: str | None = None
) -> Event:
    """Build an `Event`, checking the type and its required `data` fields.

    Raises `ValueError` for an unknown type, a missing field or a value outside the
    documented enums.
    """
    required = REQUIRED_FIELDS.get(type_)
    if required is None:
        raise ValueError(f"unknown event type: {type_!r}")
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"{type_} event is missing {', '.join(missing)}")
    _check_enums(type_, data)
    return Event(type_, session, turn, dict(data), ts or iso_z())  # type: ignore[arg-type]


def _check_enums(type_: str, data: Mapping[str, Any]) -> None:
    if type_ == "notice" and data["level"] not in NOTICE_LEVELS:
        raise ValueError(f"unknown notice level: {data['level']!r}")
    if type_ == "permission" and data["decision"] not in PERMISSION_DECISIONS:
        raise ValueError(f"unknown permission decision: {data['decision']!r}")
    if type_ == "plan":
        for entry in data["entries"]:
            if entry.get("status") not in PLAN_STATUSES:
                raise ValueError(f"unknown plan status: {entry.get('status')!r}")
            if entry.get("priority") not in PLAN_PRIORITIES:
                raise ValueError(f"unknown plan priority: {entry.get('priority')!r}")
