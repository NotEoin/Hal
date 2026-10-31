"""Terminal rendering of what the boxed agent sends (06-core-cli "TUI", REQ-SEC-25 display rules).

Every string that came from the agent or a tool is cleaned by `clean` (terminal controls stripped,
bidirectional and zero-width characters shown as `\\uXXXX`) before it reaches the terminal, and
it is printed through a rich console with markup off, so a hostile model cannot move the cursor,
retitle the window, paint a fake approval prompt or restyle text. Markdown links are drawn as
visible `text (url)`, never as OSC 8 hyperlinks, so the label cannot hide the target.

Streaming: completed markdown blocks are committed to scrollback as they arrive; the unfinished
tail and a spinner line live in a rich `Live` region (terminals only), so scrollback stays plain
text a user can select and search. Only the fixed labels this module writes are trusted.
"""

from __future__ import annotations

import contextlib
import difflib
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO
from collections.abc import Iterator

from halkit import termsafe
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from .theme import Look, load_look, make_console

MAX_LINE_CHARS = 200  # one status line per tool call; the transcript keeps the rest
MAX_VERBOSE_LINES = 400  # even verbose output is capped, `/diff` and the transcript hold the rest
CHARS_PER_TOKEN = 4  # rough English and code average, used only until the server reports usage
DIFF_CONTEXT = 2
TAB_WIDTH = 4
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)")
FENCE = "```"
Update = dict[str, object]


def clean(text: object) -> str:
    """Model or tool text made safe for the terminal: controls stripped, hiding characters made visible."""
    stripped = termsafe.strip_terminal_controls(str(text)).replace("\t", " " * TAB_WIDTH)
    return termsafe.escape(stripped, multiline=True)


def safe(text: object, *, multiline: bool = False) -> str:
    """Escapes agent-controlled text for the terminal (nothing is removed, so the user sees all of it)."""
    return termsafe.escape(str(text), multiline=multiline)


def clip(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """Cuts `text` to `limit` characters, marking the cut."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def estimate_tokens(chars: int) -> int:
    """A token count guessed from characters, for the spinner line before the server reports usage."""
    return chars // CHARS_PER_TOKEN


@dataclass
class TurnStats:
    """What one turn did, for the spinner line and the end-of-turn summary."""

    started: float = field(default_factory=time.monotonic)
    out_chars: int = 0
    thought_chars: int = 0
    files: set[str] = field(default_factory=set)
    added: int = 0
    removed: int = 0
    tools: int = 0
    used: int = 0  # context tokens in use, from `usage_update`, 0 when unknown
    size: int = 0

    @property
    def elapsed(self) -> float:
        """Seconds since the turn began."""
        return time.monotonic() - self.started

    @property
    def tokens(self) -> int:
        """Output tokens so far (answer plus reasoning), estimated."""
        return estimate_tokens(self.out_chars + self.thought_chars)

    @property
    def tok_s(self) -> float:
        """Estimated output tokens per second."""
        return self.tokens / self.elapsed if self.elapsed > 0.05 else 0.0


@dataclass
class Card:
    """One tool call being tracked."""

    title: str
    started: float
    depth: int


def diff_rows(old: str, new: str) -> list[tuple[str, int, int, str]]:
    """Unified diff rows `(kind, old_no, new_no, text)`; kind is `hunk`, `add`, `del` or `ctx`."""
    rows: list[tuple[str, int, int, str]] = []
    old_no = new_no = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=DIFF_CONTEXT):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            m = HUNK_RE.match(line)
            old_no, new_no = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            rows.append(("hunk", old_no, new_no, ""))
        elif line.startswith("+"):
            rows.append(("add", 0, new_no, line[1:]))
            new_no += 1
        elif line.startswith("-"):
            rows.append(("del", old_no, 0, line[1:]))
            old_no += 1
        else:
            rows.append(("ctx", old_no, new_no, line[1:]))
            old_no += 1
            new_no += 1
    return rows


def diff_text(rows: list[tuple[str, int, int, str]], look: Look, limit: int) -> list[Text]:
    """Coloured, line-numbered diff lines for `rows`, cut at `limit` with a note."""
    pal = look.palette
    out: list[Text] = []
    for kind, old_no, new_no, text in rows[:limit]:
        if kind == "hunk":
            out.append(Text(f"    @@ {new_no}", style=pal.muted))
            continue
        number = new_no or old_no
        mark, style = {"add": ("+", pal.ok), "del": ("-", pal.bad)}.get(kind, (" ", pal.muted))
        line = Text(f"    {number:>4} ", style=pal.muted)
        line.append(f"{mark} {clip(clean(text))}", style=style)
        out.append(line)
    if len(rows) > limit:
        out.append(Text(f"    ... {len(rows) - limit} more lines (use /diff)", style=pal.muted))
    return out


class Renderer:
    """Turns ACP `session/update` objects into terminal output; keeps the latest todo list and turn stats."""

    def __init__(self, out: TextIO | None = None, look: Look | None = None, console: Console | None = None) -> None:
        self.out = out or sys.stdout
        self.look = look or load_look(stream=self.out)
        self.console = console or make_console(self.look, self.out)
        self.todo: list[Update] = []
        self.verbose = False
        self.show_todo = True
        self.stats = TurnStats()
        self.last_folded = ""  # the full text of the last folded tool output, shown by Ctrl-O
        self.status_provider: Callable[[], Text | None] | None = None
        self._buffer = ""
        self._thought = ""
        self._cards: dict[str, Card] = {}
        self._live: Live | None = None

    # -- scrollback --------------------------------------------------------------------

    def line(self, text: str, style: str = "") -> None:
        """Prints one already trusted or cleaned line."""
        self.console.print(Text(text, style=style))

    def info(self, text: str) -> None:
        """Prints cleaned text in the muted style (command output, notices)."""
        self.console.print(Text(clean(text), style=self.look.palette.muted))

    def plain(self, text: str) -> None:
        """Prints cleaned text without styling."""
        self.console.print(Text(clean(text)))

    def _markdown(self, block: str) -> Markdown:
        return Markdown(clean(block), hyperlinks=False, code_theme=self.look.palette.code)

    def _commit_blocks(self, final: bool = False) -> None:
        """Moves complete markdown blocks from the buffer to scrollback, keeping an open fence in the tail."""
        text = self._buffer
        if final:
            block, self._buffer = text, ""
            if block.strip():
                self.console.print(self._markdown(block.rstrip()))
            return
        cut, fenced, pos = 0, False, 0
        for raw in text.splitlines(keepends=True):
            pos += len(raw)
            if raw.lstrip().startswith(FENCE):
                fenced = not fenced
            elif not fenced and not raw.strip() and pos < len(text):
                cut = pos
        if cut:
            block, self._buffer = text[:cut], text[cut:]
            if block.strip():
                self.console.print(self._markdown(block.rstrip()))

    def _end_thought(self) -> None:
        if not self._thought:
            return
        tokens = estimate_tokens(len(self._thought))
        pal, g = self.look.palette, self.look.glyphs
        if self.look.show_thinking or self.verbose:
            self.console.print(Text(clean(self._thought.strip()), style=f"dim {pal.muted}"))
        self.console.print(Text(f"{g.think} thought for ~{tokens} tokens", style=f"dim {pal.muted}"))
        self._thought = ""

    # -- live region -------------------------------------------------------------------

    def _live_view(self) -> RenderableType:
        pal = self.look.palette
        parts: list[RenderableType] = []
        if self._buffer.strip():
            parts.append(self._markdown(self._buffer))
        spin = Text.assemble(
            (f" working {self.stats.elapsed:.0f}s", pal.accent),
            (f"  {self.look.glyphs.sep}  ~{self.stats.tokens} tok", pal.muted),
            (f"  {self.look.glyphs.sep}  {self.stats.tok_s:.0f} tok/s", pal.muted),
            (f"  {self.look.glyphs.sep}  Esc interrupts", pal.muted),
        )
        parts.append(Group(Spinner(self.look.glyphs.spinner, text=spin, style=pal.accent)))
        status = self.status_provider() if self.status_provider else None
        if status is not None:
            parts.append(status)
        return Group(*parts)

    def begin_turn(self) -> None:
        """Resets the turn stats and starts the live tail on a terminal."""
        self.stats = TurnStats()
        self._buffer = ""
        self._cards.clear()
        if self.console.is_terminal and self._live is None:
            self._live = Live(
                get_renderable=self._live_view, console=self.console, refresh_per_second=self.look.refresh_hz, transient=True
            )
            self._live.start()

    def end_turn(self) -> TurnStats:
        """Commits the unfinished tail, stops the live region and returns the turn stats."""
        self._end_thought()
        self._commit_blocks(final=True)
        if self._live is not None:
            self._live.stop()
            self._live = None
        return self.stats

    @contextlib.contextmanager
    def suspended(self) -> Iterator[None]:
        """Hides the live region while something else (an approval prompt) draws, then restores it."""
        live = self._live
        if live is not None:
            live.stop()
            self._live = None
        try:
            yield
        finally:
            if live is not None and self.console.is_terminal:
                self._live = Live(
                    get_renderable=self._live_view, console=self.console, refresh_per_second=self.look.refresh_hz, transient=True
                )
                self._live.start()

    def toggle_verbose(self) -> bool:
        """Flips folded tool output on and off; turning it on shows the last folded output in full."""
        self.verbose = not self.verbose
        if self.verbose and self.last_folded:
            self.print_output(self.last_folded, force_full=True)
            self.last_folded = ""
        return self.verbose

    def toggle_todo(self) -> bool:
        """Flips the todo panel; turning it on prints the current list."""
        self.show_todo = not self.show_todo
        if self.show_todo and self.todo:
            self.console.print(self.todo_panel())
        return self.show_todo

    # -- updates -----------------------------------------------------------------------

    def update(self, update: Update) -> None:
        """Renders one update."""
        kind = update.get("sessionUpdate")
        if kind == "agent_thought_chunk":
            self._thought += _text(update)
            self.stats.thought_chars += len(_text(update))
            return
        if kind in ("agent_message_chunk", "tool_call", "tool_call_update", "plan"):
            self._end_thought()
        if kind == "agent_message_chunk":
            text = _text(update)
            self.stats.out_chars += len(text)
            self._buffer += text
            self._commit_blocks()
        elif kind == "tool_call":
            self._tool_call(update)
        elif kind == "tool_call_update":
            self._tool_update(update)
        elif kind == "plan":
            entries = update.get("entries")
            self.todo = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
            if self.show_todo:
                self.console.print(self.todo_panel())
        elif kind == "usage_update":
            used, size = update.get("used"), update.get("size")
            if isinstance(used, int) and isinstance(size, int):
                self.stats.used, self.stats.size = used, size

    def _flush_for_card(self) -> None:
        self._commit_blocks(final=True)

    def _depth(self, update: Update) -> int:
        meta = update.get("_meta")
        parent = update.get("parentToolCallId") or (meta.get("parentToolCallId") if isinstance(meta, dict) else None)
        card = self._cards.get(str(parent)) if parent else None
        return card.depth + 1 if card else 0

    def _tool_call(self, update: Update) -> None:
        cid = str(update.get("toolCallId", ""))
        self._cards[cid] = Card(clip(str(update.get("title", "tool"))), time.monotonic(), self._depth(update))
        self.stats.tools += 1
        if str(update.get("status", "")) in ("completed", "failed"):
            self._tool_update(update)

    def _tool_update(self, update: Update) -> None:
        status = str(update.get("status", ""))
        if status not in ("completed", "failed"):
            return
        cid = str(update.get("toolCallId", ""))
        card = self._cards.pop(cid, None) or Card(clip(str(update.get("title", "tool"))), time.monotonic(), 0)
        title = str(update.get("title") or card.title)
        self._flush_for_card()
        pal, g = self.look.palette, self.look.glyphs
        ok = status == "completed"
        indent = "  " * card.depth + (g.nest + " " if card.depth else "")
        row = Text(indent)
        row.append(f"{g.tool} ", style=pal.muted)
        row.append(clip(clean(title).replace("\n", " ")), style="bold")
        row.append(f"  {g.ok if ok else g.fail}", style=pal.ok if ok else pal.bad)
        row.append(f"  {time.monotonic() - card.started:.1f}s", style=pal.muted)
        content = update.get("content")
        items = [i for i in content if isinstance(i, dict)] if isinstance(content, list) else []
        diffs = [i for i in items if i.get("type") == "diff"]
        for item in diffs:
            path = clean(item.get("path", "?")).replace("\n", " ")
            rows = diff_rows(str(item.get("oldText") or ""), str(item.get("newText") or ""))
            adds, dels = sum(r[0] == "add" for r in rows), sum(r[0] == "del" for r in rows)
            self.stats.files.add(path)
            self.stats.added += adds
            self.stats.removed += dels
            row.append(f"  {clip(path, 60)} ", style=pal.muted)
            row.append(f"+{adds}", style=pal.ok)
            row.append(" ")
            row.append(f"-{dels}", style=pal.bad)
        self.console.print(row)
        for item in diffs:
            rows = diff_rows(str(item.get("oldText") or ""), str(item.get("newText") or ""))
            for line in diff_text(rows, self.look, self.look.diff_max_lines):
                self.console.print(line)
        texts = [
            str(i["content"]["text"])
            for i in items
            if i.get("type") == "content" and isinstance(i.get("content"), dict) and isinstance(i["content"].get("text"), str)
        ]
        if texts:
            self.print_output("\n".join(texts))

    def print_output(self, text: str, *, force_full: bool = False) -> None:
        lines = clean(text).rstrip("\n").splitlines()
        full = force_full or self.verbose
        limit = MAX_VERBOSE_LINES if full else self.look.tool_output_lines
        pal = self.look.palette
        for ln in lines[:limit]:
            self.console.print(Text(f"    {clip(ln)}", style=pal.muted))
        if len(lines) > limit:
            self.last_folded = text
            self.console.print(Text(f"    ... {len(lines) - limit} more lines (Ctrl-O shows all)", style=pal.muted))

    # -- todo --------------------------------------------------------------------------

    def todo_text(self) -> str:
        """The todo list as lines, or a note when there is none."""
        if not self.todo:
            return "(no todo list)"
        marks = self.look.glyphs.todo
        return "\n".join(f"  {marks.get(str(e.get('status')), marks['pending'])} {clip(clean(e.get('content', '')))}" for e in self.todo)

    def todo_panel(self) -> Panel:
        """The todo list as a rounded panel."""
        pal = self.look.palette
        body = Text(self.todo_text().replace("  ", "", 1) if self.todo else "(no todo list)", style="")
        return Panel(body, title="Todo", title_align="left", box=self.look.glyphs.box, border_style=pal.muted, expand=False)


def _text(update: Update) -> str:
    content = update.get("content")
    text = content.get("text") if isinstance(content, dict) else None
    return text if isinstance(text, str) else ""
