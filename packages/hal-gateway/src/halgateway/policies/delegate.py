"""Delegate policy (REQ-GW-45), ported from `orchestrate()` in `hal-route.py`.

For a request to the strong model, the tools that write or execute are removed and a note is
added to the system prompt, so the strong model has to hand that work to a subagent, which runs
on the fast model.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ..modes import HalSettings
from . import RequestContext


def load_delegate_note(hal: HalSettings | None) -> str:
    """The delegate note file of mode settings `hal`; raises `OSError` when unreadable."""
    path = hal.delegate_note if hal else None
    return Path(path).read_text(encoding="utf-8").strip() if path else ""


def _tool_name(tool: object) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    return str(function.get("name", "")) if isinstance(function, dict) else str(tool.get("name", ""))


def strip_tools(body: dict, names: Iterable[str]) -> None:  # type: ignore[type-arg]
    """Removes the tools called `names` (Anthropic `name`, OpenAI `function.name`)."""
    drop = set(names)
    body["tools"] = [t for t in body.get("tools") or [] if _tool_name(t) not in drop]


def add_system_note(body: dict, fmt: str, note: str) -> None:  # type: ignore[type-arg]
    """Appends `note` to the system prompt in the wire format of `fmt`."""
    if fmt == "anthropic":
        system = body.get("system")
        if not system:
            body["system"] = note
        elif isinstance(system, str):
            body["system"] = f"{system}\n\n{note}"
        else:
            body["system"] = [*system, {"type": "text", "text": note}]
        return
    messages = list(body.get("messages") or [])
    for index, message in enumerate(messages):
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            messages[index] = {**message, "content": f"{message['content']}\n\n{note}"}
            break
    else:
        messages.insert(0, {"role": "system", "content": note})
    body["messages"] = messages


class DelegatePolicy:
    """Keeps the strong model from doing the work itself."""

    name = "delegate"

    def __init__(self, tools: Iterable[str], note: str, strong: str) -> None:
        self.tools, self.note, self.strong = tuple(tools), note, strong

    def rewrite(self, ctx: RequestContext) -> None:
        """Strips the tools and adds the note when the request still targets the strong model."""
        if ctx.model != self.strong or not ctx.body.get("tools"):
            return  # the route policy sent it to the fast model, or there is nothing to strip
        strip_tools(ctx.body, self.tools)
        if self.note:
            add_system_note(ctx.body, ctx.fmt, self.note)
        ctx.decisions[self.name] = "tools stripped"
