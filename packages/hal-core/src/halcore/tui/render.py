"""Terminal rendering of what the boxed agent sends (06-core-cli "TUI", REQ-SEC-25 display rules).

Every string that came from the agent or a tool is cleaned by `clean` (terminal controls stripped,
bidirectional and zero-width characters shown as `\\uXXXX`) before it reaches the terminal, and
it is printed through a rich console with markup off, so a hostile model cannot move the cursor,
retitle the window, paint a fake approval prompt or restyle text. Markdown links are drawn as
visible `text (url)`, never as OSC 8 hyperlinks, so the label cannot hide the target.

Streaming: completed markdown blocks are committed to scrollback as they arrive; the unfinished
tail and a spinner line live in a rich `Live` region (terminals only), so scrollback stays plain
text a user can select and search. Only the fixed labels this module writes are trusted.

Split: `Renderer` reads the updates, keeps the turn stats and builds rich renderables from
cleaned text; a `Sink` only places them. `ConsoleSink` prints to the terminal (the line UI);
the full-screen UI has its own sink that mounts the same renderables as widgets. Cleaning
happens here, before any sink, so one set of display-safety tests covers both UIs.
"""

from __future__ import annotations

import contextlib
import difflib
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, TextIO
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
    server_tok_s: float = 0.0  # `_meta.hal.tokensPerSecond` of the latest reply, 0 when unreported
    compactions: int = 0

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
        """Output tokens per second: the agent's measured rate when reported, else an estimate."""
        if self.server_tok_s > 0:
            return self.server_tok_s
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


def _hal_meta(update: Update) -> dict[str, object]:
    """The `_meta.hal` object of an update, empty when absent or malformed."""
    meta = update.get("_meta")
    hal = meta.get("hal") if isinstance(meta, dict) else None
    return hal if isinstance(hal, dict) else {}


class Sink(Protocol):
    """Where the renderer's output goes; every renderable it receives is already safe to show."""

    def write(self, renderable: RenderableType) -> None:
        """Adds one finished item to the transcript."""

    def tail(self, renderable: RenderableType | None) -> None:
        """Replaces the streaming tail (the unfinished markdown block); None clears it."""

    def tool_started(self, cid: str, title: Text, depth: int) -> None:
        """A tool call began; `title` is its cleaned one-line title."""

    def tool_finished(self, cid: str, header: Text, body: list[RenderableType], full: list[RenderableType]) -> None:
        """A tool call ended: its result row, the folded body and the body in full."""

    def thought(self, summary: Text, body: Text, shown: bool) -> None:
        """A reasoning block ended: the summary line and its text, open when `shown`."""

    def turn_started(self) -> None:
        """A turn began."""

    def turn_ended(self) -> None:
        """A turn ended."""

    def suspended(self) -> contextlib.AbstractContextManager[None]:
        """Pauses live drawing while something else owns the terminal."""


class ConsoleSink:
    """Prints to a rich console; the tail and a spinner line live in a `Live` region on a terminal."""

    def __init__(self, console: Console, live_view: Callable[[], RenderableType], refresh_hz: int) -> None:
        self.console = console
        self.live_view = live_view
        self.refresh_hz = refresh_hz
        self._live: Live | None = None

    def write(self, renderable: RenderableType) -> None:
        """Prints one item to scrollback."""
        self.console.print(renderable)

    def tail(self, renderable: RenderableType | None) -> None:
        """Nothing to do: the live region reads the tail when it redraws."""

    def tool_started(self, cid: str, title: Text, depth: int) -> None:
        """Nothing to print yet: the spinner line shows that work goes on."""

    def tool_finished(self, cid: str, header: Text, body: list[RenderableType], full: list[RenderableType]) -> None:
        """Prints the result row and the folded body."""
        self.console.print(header)
        for item in body:
            self.console.print(item)

    def thought(self, summary: Text, body: Text, shown: bool) -> None:
        """Prints the reasoning when shown, then the summary line."""
        if shown:
            self.console.print(body)
        self.console.print(summary)

    def _start(self) -> None:
        self._live = Live(get_renderable=self.live_view, console=self.console, refresh_per_second=self.refresh_hz, transient=True)
        self._live.start()

    def turn_started(self) -> None:
        """Starts the live region on a terminal."""
        if self.console.is_terminal and self._live is None:
            self._start()

    def turn_ended(self) -> None:
        """Stops the live region."""
        if self._live is not None:
            self._live.stop()
            self._live = None

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
                self._start()


class Renderer:
    """Turns ACP `session/update` objects into safe renderables for a `Sink`; keeps the todo list and turn stats."""

    def __init__(
        self, out: TextIO | None = None, look: Look | None = None, console: Console | None = None, sink: Sink | None = None
    ) -> None:
        self.out = out or sys.stdout
        self.look = look or load_look(stream=self.out)
        self.console = console or make_console(self.look, self.out)
        self.sink: Sink = sink or ConsoleSink(self.console, self._live_view, self.look.refresh_hz)
        self.todo: list[Update] = []
        self.verbose = False
        self.show_todo = True
        self.stats = TurnStats()
        self.last_folded = ""  # the full text of the last folded tool output, shown by Ctrl-O
        self.last_thought = ""  # the last folded thinking block, shown by Ctrl-O too
        self.status_provider: Callable[[], Text | None] | None = None
        self._buffer = ""
        self._thought = ""
        self._cards: dict[str, Card] = {}
        self._subagents: dict[str, tuple[str, float]] = {}  # subagent id -> (name, start time)

    # -- scrollback --------------------------------------------------------------------

    def write(self, renderable: RenderableType = "") -> None:
        """Adds a renderable this program built (a panel, a table) to the transcript."""
        self.sink.write(renderable)

    def line(self, text: str, style: str = "") -> None:
        """Prints one already trusted or cleaned line."""
        self.sink.write(Text(text, style=style))

    def info(self, text: str) -> None:
        """Prints cleaned text in the muted style (command output, notices)."""
        self.sink.write(Text(clean(text), style=self.look.palette.muted))

    def plain(self, text: str) -> None:
        """Prints cleaned text without styling."""
        self.sink.write(Text(clean(text)))

    def _markdown(self, block: str) -> Markdown:
        return Markdown(clean(block), hyperlinks=False, code_theme=self.look.palette.code)

    def _commit_blocks(self, final: bool = False) -> None:
        """Moves complete markdown blocks from the buffer to scrollback, keeping an open fence in the tail."""
        text = self._buffer
        if final:
            block, self._buffer = text, ""
            if block.strip():
                self.sink.write(self._markdown(block.rstrip()))
            self.sink.tail(None)
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
                self.sink.write(self._markdown(block.rstrip()))
        self.sink.tail(self._markdown(self._buffer) if self._buffer.strip() else None)

    def _end_thought(self) -> None:
        if not self._thought:
            return
        tokens = estimate_tokens(len(self._thought))
        pal, g = self.look.palette, self.look.glyphs
        shown = self.look.show_thinking or self.verbose
        if not shown:
            self.last_thought = self._thought
        hint = "" if shown else " (Ctrl-O shows it)"
        body = Text(clean(self._thought.strip()), style=f"dim {pal.muted}")
        self.sink.thought(Text(f"{g.think} thought for ~{tokens} tokens{hint}", style=f"dim {pal.muted}"), body, shown)
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
        self._subagents.clear()
        self.sink.turn_started()

    def end_turn(self) -> TurnStats:
        """Commits the unfinished tail, stops the live region and returns the turn stats."""
        self._end_thought()
        self._commit_blocks(final=True)
        self.sink.turn_ended()
        return self.stats

    def suspended(self) -> contextlib.AbstractContextManager[None]:
        """Hides the live region while something else (an approval prompt) draws, then restores it."""
        return self.sink.suspended()

    def toggle_verbose(self) -> bool:
        """Flips folded tool output on and off; turning it on shows the last folded output and thinking block in full."""
        self.verbose = not self.verbose
        if self.verbose and self.last_folded:
            self.print_output(self.last_folded, force_full=True)
            self.last_folded = ""
        if self.verbose and self.last_thought:
            self.sink.write(Text(clean(self.last_thought.strip()), style=f"dim {self.look.palette.muted}"))
            self.last_thought = ""
        return self.verbose

    def toggle_todo(self) -> bool:
        """Flips the todo panel; turning it on prints the current list."""
        self.show_todo = not self.show_todo
        if self.show_todo and self.todo:
            self.sink.write(self.todo_panel())
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
                self.sink.write(self.todo_panel())
        elif kind == "usage_update":
            used, size = update.get("used"), update.get("size")
            if isinstance(used, int) and isinstance(size, int):
                self.stats.used, self.stats.size = used, size
            rate = _hal_meta(update).get("tokensPerSecond")
            if isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate > 0:
                self.stats.server_tok_s = float(rate)
        elif kind == "hal_compaction":
            self._compaction(update)
        elif kind == "hal_subagent":
            self._subagent(update)

    def _compaction(self, update: Update) -> None:
        """Prints one muted line when the agent masks or summarises history."""
        self._end_thought()
        self._flush_for_card()
        before, after = update.get("beforeTokens"), update.get("afterTokens")
        self.stats.compactions += 1
        if isinstance(after, int) and not isinstance(after, bool) and self.stats.size:
            self.stats.used = after
        span = f" ~{before} -> ~{after} tokens" if isinstance(before, int) and isinstance(after, int) else ""
        what = clean(str(update.get("method", "")))
        why = clean(str(update.get("reason", "auto")))
        self.sink.write(Text(f"{self.look.glyphs.think} compacted context ({why}, {what}){span}", style=f"dim {self.look.palette.muted}"))

    def _subagent(self, update: Update) -> None:
        """Nests a child agent's start, tool calls and end under the parent's turn."""
        self._flush_for_card()
        pal, g = self.look.palette, self.look.glyphs
        sid, name = str(update.get("subagentId", "")), clean(str(update.get("agent") or "agent"))
        phase = update.get("phase")
        if phase == "start":
            self._subagents[sid] = (name, time.monotonic())
            task = clean(str(update.get("task") or "")).replace("\n", " ")
            row = Text(f"{g.tool} ", style=pal.muted)
            row.append(f"{name}", style="bold")
            row.append(f"  {clip(task)}" if task else "", style=pal.muted)
            self.sink.write(row)
        elif phase == "tool":
            tool = clip(clean(str(update.get("tool") or "tool")).replace("\n", " "))
            self.sink.write(Text(f"  {g.nest} {tool}", style=pal.muted))
        elif phase == "done":
            _, started = self._subagents.pop(sid, (name, time.monotonic()))
            row = Text(f"  {g.nest} ", style=pal.muted)
            row.append(f"{name} done {g.ok}", style=pal.ok)
            row.append(f"  {time.monotonic() - started:.1f}s", style=pal.muted)
            self.sink.write(row)

    def _flush_for_card(self) -> None:
        self._commit_blocks(final=True)

    def _depth(self, update: Update) -> int:
        meta = update.get("_meta")
        parent = update.get("parentToolCallId") or (meta.get("parentToolCallId") if isinstance(meta, dict) else None)
        card = self._cards.get(str(parent)) if parent else None
        return card.depth + 1 if card else 0

    def _tool_call(self, update: Update) -> None:
        cid = str(update.get("toolCallId", ""))
        card = Card(clip(str(update.get("title", "tool"))), time.monotonic(), self._depth(update))
        self._cards[cid] = card
        self.stats.tools += 1
        if str(update.get("status", "")) in ("completed", "failed"):
            self._tool_update(update)
            return
        self._flush_for_card()
        self.sink.tool_started(cid, Text(clip(clean(card.title).replace("\n", " "))), card.depth)

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
        body: list[RenderableType] = []
        full: list[RenderableType] = []
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
            body += diff_text(rows, self.look, self.look.diff_max_lines)
            full += diff_text(rows, self.look, MAX_VERBOSE_LINES)
        texts = [
            str(i["content"]["text"])
            for i in items
            if i.get("type") == "content" and isinstance(i.get("content"), dict) and isinstance(i["content"].get("text"), str)
        ]
        if texts:
            joined = "\n".join(texts)
            body += self._output_lines(joined, full=self.verbose, remember=True)
            full += self._output_lines(joined, full=True, remember=False)
        self.sink.tool_finished(cid, row, body, full)

    def _output_lines(self, text: str, *, full: bool, remember: bool) -> list[Text]:
        """Tool output as muted lines cut to the fold limit (or the verbose cap when `full`)."""
        lines = clean(text).rstrip("\n").splitlines()
        limit = MAX_VERBOSE_LINES if full else self.look.tool_output_lines
        pal = self.look.palette
        out = [Text(f"    {clip(ln)}", style=pal.muted) for ln in lines[:limit]]
        if len(lines) > limit:
            if remember:
                self.last_folded = text
            out.append(Text(f"    ... {len(lines) - limit} more lines (Ctrl-O shows all)", style=pal.muted))
        return out

    def print_output(self, text: str, *, force_full: bool = False) -> None:
        """Prints tool output cleaned of control characters, cut to the line limit unless `force_full`."""
        for line in self._output_lines(text, full=force_full or self.verbose, remember=True):
            self.sink.write(line)

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
