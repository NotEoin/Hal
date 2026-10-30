"""Terminal rendering of what the boxed agent sends (06-core-cli "TUI", REQ-SEC-25 display rules).

Every string that came from the agent or a tool is passed through `halkit.termsafe.escape`
before it reaches the terminal, so a hostile model cannot move the cursor, retitle the window
or paint a fake approval prompt. Only the fixed labels this module writes are raw.
"""

from __future__ import annotations

import sys
from typing import TextIO

from halkit import termsafe

MAX_LINE_CHARS = 200  # one status line per tool call; the transcript keeps the rest
MAX_DIFF_LINES = 40  # a diff preview, not the whole patch (`/diff` shows the patch)
STATUS_MARK = {"pending": "...", "in_progress": "...", "completed": "ok", "failed": "FAILED"}
PLAN_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
Update = dict[str, object]


def safe(text: object, *, multiline: bool = False) -> str:
    """Escapes agent-controlled text for the terminal."""
    return termsafe.escape(str(text), multiline=multiline)


def clip(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """Cuts `text` to `limit` characters, marking the cut."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Renderer:
    """Turns ACP `session/update` objects into terminal lines; keeps the latest todo list."""

    def __init__(self, out: TextIO | None = None) -> None:
        self.out = out or sys.stdout
        self.todo: list[Update] = []
        self._streaming = False

    def _line(self, text: str) -> None:
        if self._streaming:
            print(file=self.out)
            self._streaming = False
        print(text, file=self.out, flush=True)

    def update(self, update: Update) -> None:
        """Renders one update."""
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = _text(update)
            if text:
                self._streaming = True
                print(safe(text, multiline=True), end="", file=self.out, flush=True)
        elif kind == "agent_thought_chunk":
            return  # reasoning stays in the transcript; `hal sessions export --reasoning` shows it
        elif kind == "tool_call":
            self._line(f"  * {clip(safe(update.get('title', 'tool')))}")
            self._diff(update)
        elif kind == "tool_call_update":
            status = str(update.get("status", ""))
            if status in ("completed", "failed"):
                title = clip(safe(update.get("title", "tool")))
                self._line(f"    {STATUS_MARK[status]}: {title}")
                self._diff(update)
        elif kind == "plan":
            entries = update.get("entries")
            self.todo = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
            self._line(self.todo_text())

    def _diff(self, update: Update) -> None:
        content = update.get("content")
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") == "diff":
                old, new = str(item.get("oldText") or ""), str(item.get("newText") or "")
                shown = [f"-{line}" for line in old.splitlines()] + [f"+{line}" for line in new.splitlines()]
                self._line(f"    diff {clip(safe(item.get('path', '?')))}")
                for line in shown[:MAX_DIFF_LINES]:
                    self._line("    " + clip(safe(line)))
                if len(shown) > MAX_DIFF_LINES:
                    self._line(f"    ... {len(shown) - MAX_DIFF_LINES} more lines (use /diff)")

    def todo_text(self) -> str:
        """The todo list as lines, or a note when there is none."""
        if not self.todo:
            return "(no todo list)"
        return "\n".join(
            f"  {PLAN_MARK.get(str(e.get('status')), '[ ]')} {clip(safe(e.get('content', '')))}" for e in self.todo
        )

    def end_turn(self) -> None:
        """Finishes the streamed line."""
        if self._streaming:
            print(file=self.out, flush=True)
            self._streaming = False


def _text(update: Update) -> str:
    content = update.get("content")
    text = content.get("text") if isinstance(content, dict) else None
    return text if isinstance(text, str) else ""
