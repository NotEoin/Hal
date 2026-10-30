"""Effort policy (REQ-GW-41), ported from `EffortRouter` and `with_effort` in `hal-route.py`.

On a request with tools, the effort note is appended as a final system message at a decision
point only, so routine steps stay fast. The note is template specific: the bundled
`effort-xhigh.txt` holds the wording of `qwen3.8-chat.jinja`.
"""

from __future__ import annotations

from pathlib import Path

from .. import modes
from . import RequestContext
from .text import append_system, decision_point


def load_note(hal: object | None) -> str:
    """The effort note of mode settings `hal`, or the bundled one; raises `OSError` if unreadable."""
    path = getattr(hal, "effort_note", None) or modes.EFFORT_NOTE
    return Path(path).read_text(encoding="utf-8").strip()


class EffortPolicy:
    """Asks for extra reasoning at the decision points of a turn."""

    name = "effort"

    def __init__(self, note: str) -> None:
        self.note = note

    def rewrite(self, ctx: RequestContext) -> None:
        """Appends the note at a decision point and records `xhigh (<reason>)` or `medium`."""
        if not ctx.body.get("tools"):
            return
        reason = decision_point(ctx.body.get("messages") or [], ctx.fmt)
        if not reason:
            ctx.decisions[self.name] = "medium"
            return
        append_system(ctx.body, ctx.fmt, self.note)
        ctx.decisions[self.name] = f"xhigh ({reason})"
