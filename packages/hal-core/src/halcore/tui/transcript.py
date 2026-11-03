"""The transcript of the full-screen `hal code` and `hal chat` and the sink that fills it.

`Transcript` is the scrolling conversation; `TextualSink` is the `render.Sink` that mounts what
`Renderer` built (renderables made from cleaned text, never Textual markup) into it. Updates
arrive on the ACP reader task and run on the app's loop through `App.call_later`, in order.
`UserMessage` is a message the human sent. The app (`agentapp.AgentApp`) owns these widgets.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound
from rich.console import RenderableType
from rich.markdown import Markdown
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical, VerticalScroll
from textual.strip import Strip
from textual.widget import Widget

from . import motion, tokens
from .render import ToolRow, clean
from .theme import Look
from .widgets import SPINNER_HZ, TIMER_S, SafeStatic, ThinkingFold, ToolCard, toast

if TYPE_CHECKING:
    from .agentapp import AgentApp


class TurnView(Vertical):
    """One turn: the human's message and everything the agent did for it, behind a left rail.

    The rail is the turn's only frame: `panel` grey once finished, `accent` while it runs, `warn`
    while an approval waits, `faint` while queued. Each turn keeps its own streaming tail last.
    """

    DEFAULT_CSS = """
    TurnView { height: auto; border-left: solid $hal-panel; padding-left: 1; margin-top: 1; }
    TurnView.ascii { border-left: ascii $hal-panel; }
    TurnView.queued { border-left: solid $hal-faint; }
    TurnView.queued.ascii { border-left: ascii $hal-faint; }
    TurnView.running { border-left: solid $primary; }
    TurnView.running.ascii { border-left: ascii $primary; }
    TurnView.waiting { border-left: solid $warning; }
    TurnView.waiting.ascii { border-left: ascii $warning; }
    TurnView > .tail { height: auto; }
    TurnView > .gap { margin-top: 1; }
    TurnView > .turn-end { color: $hal-faint; }
    """

    def __init__(self, message: UserMessage, *, ascii: bool) -> None:
        super().__init__(classes="queued ascii" if ascii else "queued")
        self.message = message
        self.tail = SafeStatic("", classes="tail")
        self.last_kind = "you"  # the kind of block added last, for the gap between kinds

    def compose(self) -> ComposeResult:
        """The message, then the streaming tail (kept last)."""
        yield self.message
        yield self.tail

    def add(self, widget: Widget, kind: str = "") -> None:
        """Mounts `widget` above this turn's streaming tail; a block of a new kind gets a blank row above it."""
        if kind:
            widget.set_class(kind != self.last_kind, "gap")
            self.last_kind = kind
        self.mount(widget, before=self.tail)


class Transcript(VerticalScroll):
    """The conversation, one `TurnView` per message; follows new output while the view is at the bottom."""

    DEFAULT_CSS = """
    Transcript { height: 1fr; padding: 0 0 0 1; }  /* the side handle is the right-hand column */
    Transcript > .notice { color: $hal-muted; }
    Transcript.compact TurnView > .gap { margin-top: 0; }  /* here: a TurnView rule is scoped under TurnView */
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.follow = True
        self.root_tail = SafeStatic("", id="tail")
        self.active: TurnView | None = None  # the turn output goes to
        self.pending: list[TurnView] = []  # sent, not yet read by the controller (in inbox order)
        self.unseen = 0  # items added while the user was reading further up
        self.below = SafeStatic("", id="below")  # the "new output below" line, mounted by the app

    @property
    def tail(self) -> SafeStatic:
        """The streaming tail of the active turn (or of the transcript before the first turn)."""
        return self.active.tail if self.active is not None and self.active.is_attached else self.root_tail

    def compose(self) -> ComposeResult:
        """The transcript's own streaming tail, kept last."""
        yield self.root_tail

    def add(self, widget: Widget, kind: str = "") -> None:
        """Mounts `widget` in the active turn (or above the tail) and keeps the view at the bottom when following.

        `kind` (`prose`, `code`, `tools`, `thought`, `plan`, `end`) spaces blocks of different kinds apart.
        """
        if self.active is not None and self.active.is_attached:
            self.active.add(widget, kind)
        else:
            self.mount(widget, before=self.root_tail)  # type: ignore[arg-type]  # a Widget
        self.followed()

    def new_turn(self, message: UserMessage, *, ascii: bool) -> TurnView:
        """A queued turn for a message just sent; `activate_next` makes it the active one."""
        turn = TurnView(message, ascii=ascii)
        self.mount(turn, before=self.root_tail)
        self.pending.append(turn)
        self.followed()
        return turn

    def activate_next(self) -> None:
        """The controller read the next message: its turn becomes the one output goes to."""
        if self.active is not None:
            self.active.remove_class("running", "waiting")
        if not self.pending:
            return
        turn = self.pending.pop(0)
        turn.remove_class("queued")
        turn.message.set_queued(False)
        self.active = turn

    def set_running(self, running: bool) -> None:
        """The active turn's rail turns accent while it runs."""
        if self.active is not None:
            self.active.set_class(running, "running")
            if not running:
                self.active.remove_class("waiting")

    def set_waiting(self, waiting: bool) -> None:
        """The active turn's rail turns amber while an approval waits."""
        if self.active is not None:
            self.active.set_class(waiting, "waiting")

    def clear(self) -> None:
        """Removes every item but the transcript's own tail."""
        for child in list(self.children):
            if child is not self.root_tail:
                child.remove()
        self.active, self.pending = None, []

    def followed(self) -> None:
        """Keeps the view at the bottom when following, else counts what arrived below."""
        if self.follow:
            self.call_after_refresh(self.scroll_end, animate=False)
        else:
            self.unseen += 1
            self.below.update(Text(f"v {self.unseen} new below  (click or End to follow)"))
            self.below.display = True

    def watch_scroll_y(self, old: float, new: float) -> None:
        """Following stops when the user scrolls up and starts again at the bottom."""
        super().watch_scroll_y(old, new)
        self.follow = new >= self.max_scroll_y - 1
        if self.follow:
            self.unseen = 0
            self.below.display = False

    def jump(self) -> None:
        """Back to the newest output, following it again."""
        self.follow = True
        self.scroll_end(animate=False)


HOVER_GAP = 2  # cells between two hover action words
HOVER_MARGIN = 1  # cells kept free right of the last action word
STAMP_FORMAT = "%H:%M"  # the local time a chat block was shown, on hover


def stamp_now() -> str:
    """The local `HH:MM` now, for a block's hover timestamp."""
    return time.strftime(STAMP_FORMAT, time.localtime())


class HoverActions:
    """Mixin for a `SafeStatic`: fixed action words drawn over the right end of its first row while hovered.

    The words are this program's own strings, never model text; a click on one runs it through
    `run_hover_action`. Drawn over the row (not beside it) so hovering never reflows the text.
    """

    hover_x = -1  # the pointer's column, for the word under it
    stamp = ""  # in `hal chat`: the local `HH:MM` the block was shown, drawn after the actions as text

    def hover_actions(self) -> list[str]:
        """The action words this block offers."""
        return ["copy"]

    def on_mount(self) -> None:
        """In `hal chat`, notes the time the block was shown."""
        if not getattr(self.app, "code", True):  # type: ignore[attr-defined]  # a Widget
            self.stamp = stamp_now()

    def hover_words(self) -> list[str]:
        """What hovering draws: the actions, then the timestamp (text, not a button) when there is one."""
        return [*self.hover_actions(), *([self.stamp] if self.stamp else [])]

    def run_hover_action(self, name: str) -> None:
        """Runs action `name`."""

    def hover_spans(self, width: int) -> list[tuple[str, int, int]]:
        """Each hover word with its start and end column on the first row; empty when the row is too narrow."""
        words = self.hover_words()
        total = sum(len(w) for w in words) + HOVER_GAP * (len(words) - 1) + HOVER_MARGIN
        x, spans = width - total, []
        if not words or x <= 0:
            return []
        for word in words:
            spans.append((word, x, x + len(word)))
            x += len(word) + HOVER_GAP
        return spans

    def action_at(self, x: int, y: int) -> str | None:
        """The action word at column `x` of row `y`, if any."""
        if y != 0:
            return None
        width = self.content_size.width  # type: ignore[attr-defined]  # a Widget
        actions = self.hover_actions()
        return next((word for word, a, b in self.hover_spans(width) if a <= x < b and word in actions), None)

    def watch_mouse_hover(self, hovered: bool) -> None:
        """The words show and hide with the pointer."""
        self.refresh()  # type: ignore[attr-defined]  # a Widget

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """The word under the pointer lights up."""
        if event.y == 0 and event.x != self.hover_x:
            self.hover_x = event.x
            self.refresh()  # type: ignore[attr-defined]  # a Widget
        elif event.y != 0 and self.hover_x != -1:
            self.hover_x = -1
            self.refresh()  # type: ignore[attr-defined]  # a Widget

    def render_line(self, y: int) -> Strip:
        """The widget's own row, with the action words over its right end on row 0 while hovered."""
        strip: Strip = super().render_line(y)  # type: ignore[misc]  # a Widget
        if y != 0 or not self.mouse_hover:  # type: ignore[attr-defined]  # a Widget
            return strip
        width = self.content_size.width  # type: ignore[attr-defined]  # a Widget
        spans = self.hover_spans(width)
        if not spans:
            return strip
        bg = next((seg.style.bgcolor for seg in strip if seg.style and seg.style.bgcolor), None)
        strip = strip.extend_cell_length(width, Style(bgcolor=bg))  # the cached row is only as wide as its text
        start = spans[0][1]
        look = getattr(self.app, "look", None)  # type: ignore[attr-defined]  # a Widget
        roles = tokens.ROLES.get(look.theme, {}) if look is not None and look.color else {}
        faint = Style(color=roles.get("faint"), dim=not roles, bgcolor=bg)
        lit = Style(color=roles.get("accent"), bold=not roles, underline=True, bgcolor=bg)
        segments, pos = [], start
        for word, a, b in spans:
            if a > pos:
                segments.append(Segment(" " * (a - pos), faint))
            segments.append(Segment(word, lit if a <= self.hover_x < b and word != self.stamp else faint))
            pos = b
        segments.append(Segment(" " * (width - pos), faint))
        return Strip.join([strip.crop(0, start), Strip(segments)])


class UserMessage(HoverActions, SafeStatic):
    """A message the human sent, behind the turn's rail: prompt mark, then the text bold (hanging indent).

    Hovering shows `copy edit again` (and `rewind` in code when the message has a checkpoint, the
    time it was sent in chat); a click elsewhere on it, or Enter, offers the same actions in a dialog.
    """

    DEFAULT_CSS = """
    UserMessage { height: auto; }
    UserMessage:hover { background: $hal-surface; }
    UserMessage:focus { background: $hal-surface; }
    """
    can_focus = True
    BINDINGS = [Binding("enter", "actions", "actions", show=False)]

    def __init__(self, text: str, look: Look) -> None:
        self.text = text
        self.look = look
        self.queued = False
        super().__init__(self.shown(), classes="you")
        self.checkpoint: int | None = None  # the checkpoint taken just before this message ran

    def shown(self) -> RenderableType:
        """Prompt mark in the accent, the text bold, a faint `queued` tag while it waits."""
        roles = tokens.ROLES.get(self.look.theme, {}) if self.look.color else {}
        grid = Table.grid(padding=0)
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(ratio=1)
        body = Text(clean(self.text), style="bold")
        if self.queued:
            body.append("  queued", style=roles.get("faint", "dim"))
        grid.add_row(Text(self.look.glyphs.prompt, style=f"bold {roles.get('accent', '')}".strip()), body)
        return grid

    def set_queued(self, queued: bool) -> None:
        """Shows or drops the `queued` tag."""
        if queued != self.queued:
            self.queued = queued
            self.update(self.shown())

    def hover_actions(self) -> list[str]:
        """`copy edit again`, and `rewind` in code once the message has a checkpoint."""
        words = ["copy", "edit", "again"]
        if getattr(self.app, "code", False) and self.checkpoint is not None:
            words.append("rewind")
        return words

    def run_hover_action(self, name: str) -> None:
        """Hands the action to the app (`AgentApp.message_action`)."""
        act = getattr(self.app, "message_action", None)
        if act is not None:
            act(self, name)

    def action_actions(self) -> None:
        """Enter: the message actions in a dialog."""
        actions = getattr(self.app, "message_actions", None)
        if actions is not None:
            self.app.run_worker(actions(self), group="message")

    async def on_click(self, event: events.Click) -> None:
        """A click on an action word runs it; elsewhere it offers the actions in a dialog."""
        name = self.action_at(event.x, event.y)
        if name is not None:
            event.stop()
            self.run_hover_action(name)
        else:
            self.action_actions()


class AssistantBlock(HoverActions, SafeStatic):
    """One committed block of the assistant's prose; hovering offers `copy` of its markdown source (and the time in chat)."""

    def __init__(self, renderable: Markdown, **kwargs: object) -> None:
        super().__init__(renderable, **kwargs)
        self.source = renderable.markup  # cleaned by the renderer

    def run_hover_action(self, name: str) -> None:
        """`copy`: the block's markdown source to the clipboard."""
        if name == "copy":
            self.app.copy_to_clipboard(self.source)
            toast(self.app, "copied", timeout=COPY_TOAST_S)

    def on_click(self, event: events.Click) -> None:
        """A click on `copy` copies the block."""
        name = self.action_at(event.x, event.y)
        if name is not None:
            event.stop()
            self.run_hover_action(name)


COPY_TOAST_S = 2.0
LANG_MAX = 16  # cells of a code block's language label
LANG_CHARS = re.compile(r"[^A-Za-z0-9+#._-]")  # a label keeps only what language names use


def language_label(lang: str) -> str:
    """A fence's language word made safe to show: cleaned, only `[A-Za-z0-9+#._-]`, at most `LANG_MAX` cells."""
    return LANG_CHARS.sub("", clean(lang).split(" ", 1)[0] if lang.strip() else "")[:LANG_MAX]


def lexer_for(label: str) -> str:
    """The label when pygments has a lexer by that name, else `text` (pygments comes with rich)."""
    if not label:
        return "text"
    try:
        get_lexer_by_name(label)
    except ClassNotFound:
        return "text"
    return label


class CodeHeader(Widget):
    """The row above a code block: the language label left, `copy` right; a click copies the source."""

    DEFAULT_CSS = """
    CodeHeader { height: 1; color: $hal-faint; }
    CodeHeader:hover { color: $primary; }
    """

    def __init__(self, label: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.label = label

    def render(self) -> RenderableType:
        """`python ...... copy`."""
        width = self.size.width or 40
        text = Text(self.label, no_wrap=True)
        text.append(" " * max(1, width - len(self.label) - len("copy")))
        text.append("copy")
        return text


class CodeBlock(Vertical):
    """A fenced code block: label and copy row, then the code on the surface colour; it scrolls sideways, never wraps."""

    DEFAULT_CSS = """
    CodeBlock { height: auto; padding-left: 2; }
    CodeBlock > .code-body { height: auto; overflow-x: auto; overflow-y: hidden; background: $hal-surface;
                             scrollbar-size-horizontal: 1; }
    CodeBlock > .code-body > SafeStatic { width: auto; }
    """

    def __init__(self, lang: str, source: str, look: Look, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.label = language_label(lang)
        self.source = source  # already cleaned by the renderer; copied as it is
        self.look = look

    def compose(self) -> ComposeResult:
        """The header row and the highlighted code."""
        yield CodeHeader(self.label, classes="code-header")
        roles = tokens.ROLES.get(self.look.theme, {}) if self.look.color else {}
        syntax = Syntax(clean(self.source), lexer_for(self.label), theme=self.look.palette.code,
                        background_color=roles.get("surface"), word_wrap=False)  # fmt: skip
        with ScrollableContainer(classes="code-body"):
            yield SafeStatic(syntax)

    def on_click(self, event: events.Click) -> None:
        """A click on the header copies the source."""
        if isinstance(event.widget, CodeHeader):
            event.stop()
            self.app.copy_to_clipboard(self.source)
            lines = self.source.count("\n") + 1
            toast(self.app, f"copied {lines} line{'s' if lines != 1 else ''}", timeout=COPY_TOAST_S)


FOLD_MIN = 3  # consecutive finished tool calls that fold into one "ran N tools" row
GROUP_TARGETS = 3  # targets named per verb in the summary before "+N"


def group_summary(rows: list[ToolRow]) -> list[str]:
    """One part per verb (`read calc.py, test_calc.py`, `grep add(`): verbs lowercased, paths as basenames."""
    by_verb: dict[str, list[str]] = {}
    for row in rows:
        head, _, rest = row.target.partition(" ")
        if "/" in head:
            head = head.rstrip("/").rsplit("/", 1)[-1]
        target = f"{head} {rest}".strip()
        by_verb.setdefault(row.verb.lower(), [])
        if target and target not in by_verb[row.verb.lower()]:
            by_verb[row.verb.lower()].append(target)
    parts = []
    for verb, targets in by_verb.items():
        shown = ", ".join(targets[:GROUP_TARGETS]) + (f" +{len(targets) - GROUP_TARGETS}" if len(targets) > GROUP_TARGETS else "")
        parts.append(f"{verb} {shown}".strip())
    return parts


class ToolGroup(Widget):
    """One row standing for 3 or more consecutive finished tool calls; a click shows or hides them.

    Only calls that succeeded and changed no file fold: an edit keeps its preview on screen and a
    failure stays open. A group forms only once its run has ended, so running calls are visible.
    """

    DEFAULT_CSS = """
    ToolGroup { height: 1; }
    ToolGroup:hover { background: $hal-surface; }
    """
    can_focus = True
    BINDINGS = [Binding("enter", "toggle", "expand", show=False)]

    def __init__(self, cards: list[ToolCard], rows: list[ToolRow], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.cards = cards
        self.rows = rows  # from the sink: a card that never started gets its own row only after a refresh
        self.open = False

    def render(self) -> RenderableType:
        """`* ran 3 tools  read a.py, b.py | grep x      0.3s  ok  >`."""
        look = getattr(self.app, "look", None)
        g = look.glyphs if look is not None else None
        roles = tokens.ROLES.get(look.theme, {}) if look is not None and look.color else {}
        faint, muted = roles.get("faint", "dim"), roles.get("muted", "dim")
        sep = f" {g.sep if g else '|'} "
        text = Text(no_wrap=True, overflow="crop")
        text.append(f"{g.tool if g else '*'} ", style=faint)
        text.append(f"ran {len(self.cards)} tools", style=f"bold {roles.get('fg', '')}".strip())
        right = Text(f"{sum(r.seconds for r in self.rows):.1f}s", style=faint)
        right.append(f"  {g.ok if g else 'ok'}", style=roles.get("ok", ""))
        fold = (g.fold_open if self.open else g.fold_closed) if g else ("v" if self.open else ">")
        right.append(f"  {fold}", style=faint)
        width = self.size.width or 80
        summary = sep.join(group_summary(self.rows))
        room = width - text.cell_len - right.cell_len - 4
        ellipsis = g.ellipsis if g else "..."
        if room > 0:
            text.append("  " + (summary if len(summary) <= room else summary[: max(0, room - len(ellipsis))] + ellipsis), style=muted)
        text.append(" " * max(2, width - text.cell_len - right.cell_len))
        text.append_text(right)
        return text

    def action_toggle(self) -> None:
        """Shows or hides the folded calls."""
        self.open = not self.open
        for card in self.cards:
            card.display = self.open
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        """A click toggles the folded calls."""
        self.action_toggle()


PLAN_BAR_CELLS = 8  # width of the plan's progress bar (spec 5.7)


class PlanBlock(Widget):
    """The turn's todo list, updated in place: `plan  ###-----  1 of 3`, then one row per item.

    Entries arrive cleaned and clipped from the renderer. Done items are faint with an `ok` mark,
    the active one `fg` with an accent mark, pending ones muted. The bar eases to a new value.
    """

    DEFAULT_CSS = """
    PlanBlock { height: auto; }
    """

    def __init__(self, entries: list[tuple[str, str]], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.entries = entries
        self.shown = self.fraction  # the bar's drawn fill; eases towards `fraction`
        self._tween: motion.Handle | None = None

    @property
    def fraction(self) -> float:
        """The share of items done, 0 for an empty list."""
        done = sum(1 for status, _ in self.entries if status == "completed")
        return done / len(self.entries) if self.entries else 0.0

    def set_entries(self, entries: list[tuple[str, str]]) -> None:
        """Shows a new list; the bar eases from its drawn fill to the new one."""
        start, self.entries = self.shown, entries
        if self._tween is not None:
            self._tween.cancel()
        ticker = getattr(self.app, "motion", None)
        if ticker is None:
            self._ease(self.fraction)
        else:
            self._tween = ticker.tween(start, self.fraction, motion.GAUGE_TWEEN_S, self._ease)
        self.refresh(layout=True)

    def _ease(self, value: float) -> None:
        self.shown = value
        self.refresh()

    def render(self) -> RenderableType:
        """The header row, then each item with its mark."""
        look = getattr(self.app, "look", None)
        g = look.glyphs if look is not None else None
        roles = tokens.ROLES.get(look.theme, {}) if look is not None and look.color else {}
        faint, muted, fg = roles.get("faint", "dim"), roles.get("muted", "dim"), roles.get("fg", "")
        marks = g.plan if g is not None else tokens.ASCII_PLAN
        done = sum(1 for status, _ in self.entries if status == "completed")
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("plan  ", style=muted)
        full, empty = (g.bar_full, g.bar_empty) if g is not None else ("#", "-")
        cells = round(self.shown * PLAN_BAR_CELLS)
        text.append(full * cells, style=roles.get("accent", ""))
        text.append(empty * (PLAN_BAR_CELLS - cells), style=roles.get("panel", "dim"))
        text.append(f"  {done} of {len(self.entries)}", style=faint)
        for status, item in self.entries:
            mark, style, mark_style = {
                "completed": (marks["completed"], faint, roles.get("ok", "")),
                "in_progress": (marks["in_progress"], fg, roles.get("accent", "")),
            }.get(status, (marks["pending"], muted, muted))
            text.append("\n  ")
            text.append(mark, style=mark_style)
            text.append(f" {item}", style=style)
        return text


SUBAGENT_TOOLS_SHOWN = 3  # latest tool rows a subagent tree shows before "+N more" (spec 5.8)


class SubagentTree(Widget):
    """One child agent, updated in place: name, task and time, then its latest tool calls.

    The header spins while it runs (a static mark with motion off) and shows `ok` and the total
    time when done. Only the last `SUBAGENT_TOOLS_SHOWN` tools are drawn, after a `+N more` row.
    """

    DEFAULT_CSS = """
    SubagentTree { height: auto; }
    """

    def __init__(self, name: str, task: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.agent, self.job = name, task  # cleaned by the renderer
        self.tools: list[str] = []
        self.done = False
        self.elapsed = 0.0  # seconds, as the renderer measured them at the last update
        self.began = time.monotonic()  # for the running time between updates
        self.tick = 0
        self._handles: list[motion.Handle] = []

    def on_mount(self) -> None:
        """Joins the app's motion clock: the time each second, spinner frames when motion is on."""
        ticker = getattr(self.app, "motion", None)
        if ticker is not None and not self.done:
            self._handles = [ticker.every(TIMER_S, self._redraw), ticker.subscribe(self._spin, SPINNER_HZ)]

    def on_unmount(self) -> None:
        """Leaves the motion clock."""
        self.stop()

    def stop(self) -> None:
        """Stops the spinner and the clock (a turn ended before the agent said it was done)."""
        for handle in self._handles:
            handle.cancel()
        self._handles = []

    def _spin(self, now: float) -> bool:
        self.tick += 1
        return self._redraw(now)

    def _redraw(self, now: float = 0.0) -> bool:
        if self.done or not self.is_attached:
            return False
        self.refresh()
        return True

    def add_tool(self, tool: str, elapsed: float) -> None:
        """One more tool call by this agent."""
        self.tools.append(tool)
        self.elapsed, self.began = elapsed, time.monotonic() - elapsed
        self.refresh(layout=True)

    def finish(self, elapsed: float) -> None:
        """The agent ended after `elapsed` seconds."""
        self.done, self.elapsed = True, elapsed
        self.stop()
        self.refresh()

    def render(self) -> RenderableType:
        """`* explorer  task      3s`, then `|- Grep  add(` rows."""
        look = getattr(self.app, "look", None)
        g = look.glyphs if look is not None else None
        roles = tokens.ROLES.get(look.theme, {}) if look is not None and look.color else {}
        faint, muted = roles.get("faint", "dim"), roles.get("muted", "dim")
        if self.done:
            mark, mark_style = (g.ok if g else "ok"), roles.get("ok", "")
            time_text = f"{self.elapsed:.1f}s"
        else:
            animated = look is not None and look.animations and g is not None
            mark = g.spinner_frames[self.tick % len(g.spinner_frames)] if animated else (g.tool if g else "*")
            mark_style = roles.get("accent", "")
            time_text = f"{max(self.elapsed, time.monotonic() - self.began):.0f}s"
        width = self.size.width or 80
        head = Text(no_wrap=True, overflow="ellipsis")
        head.append(f"{mark} ", style=mark_style)
        head.append(self.agent, style="bold")
        right = Text(time_text, style=faint)
        room = width - head.cell_len - right.cell_len - 4
        if self.job and room > 0:
            ellipsis = g.ellipsis if g else "..."
            task = self.job if len(self.job) <= room else self.job[: max(0, room - len(ellipsis))] + ellipsis
            head.append(f"  {task}", style=muted)
        head.append(" " * max(2, width - head.cell_len - right.cell_len))
        head.append_text(right)
        lines = [head]
        hidden = max(0, len(self.tools) - SUBAGENT_TOOLS_SHOWN)
        if hidden:
            lines.append(Text(f"  +{hidden} more", style=faint))
        shown = self.tools[hidden:]
        for i, tool in enumerate(shown):
            last = i == len(shown) - 1
            branch = (g.nest if last else g.branch) if g else ("`-" if last else "|-")
            verb, target = ToolRow.split(tool)
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(f"  {branch} ", style=faint)
            row.append(f"{verb:<6} ", style=muted)
            row.append(target, style=faint)
            lines.append(row)
        return Text("\n").join(lines)


class TextualSink:
    """The `render.Sink` of the full-screen UI: mounts what the renderer built into the transcript."""

    def __init__(self, app: AgentApp) -> None:
        self.app = app
        self.cards: dict[str, ToolCard] = {}
        self.rows: dict[str, ToolRow] = {}  # parts of a finished call, from `tool_row`, until `tool_finished`
        self.pending: list[Callable[[], None]] = []  # output before the app has mounted
        self.run: list[tuple[ToolCard, ToolRow]] = []  # finished calls in a row that may fold into a `ToolGroup`
        self.plan_block: PlanBlock | None = None  # this turn's plan, updated in place
        self.subagents: dict[str, SubagentTree] = {}  # one tree per child agent id, updated in place

    def _run(self, action: Callable[[], None]) -> None:
        # updates arrive on the ACP reader task; `call_later` runs them on the app's own loop, in order,
        # so widgets are mounted with the app's context (timers and queries need it)
        if self.app.mounted:
            self.app.call_later(self._guarded, action)
        else:
            self.pending.append(action)

    def _guarded(self, action: Callable[[], None]) -> None:
        """Runs `action` unless the app is already tearing the transcript down."""
        if self.app.transcript.is_attached and self.app.is_running:
            action()

    def flush(self) -> None:
        """Runs what arrived before the app mounted."""
        for action in self.pending:
            action()
        self.pending.clear()

    def fold(self) -> None:
        """Ends the current run of tool calls; 3 or more become one `ToolGroup` row (runs on the app's loop)."""
        run, self.run = self.run, []
        run = [(c, r) for c, r in run if c.is_attached]
        cards = [c for c, _ in run]
        if len(cards) < FOLD_MIN or len({id(c.parent) for c in cards}) != 1:
            return
        group = ToolGroup(cards, [r for _, r in run])
        group.set_class(cards[0].has_class("gap"), "gap")
        cards[0].parent.mount(group, before=cards[0])  # type: ignore[union-attr]  # attached: has a parent
        for card in cards:
            card.display = False

    def write(self, renderable: RenderableType) -> None:
        """One finished item."""

        def add() -> None:
            self.fold()
            block = AssistantBlock(renderable) if isinstance(renderable, Markdown) else SafeStatic(renderable)
            self.app.transcript.add(block, "prose")

        self._run(add)

    def tail(self, renderable: RenderableType | None) -> None:
        """The streaming markdown block."""

        def show() -> None:
            if renderable is not None:
                self.fold()
            self.app.transcript.tail.update(renderable if renderable is not None else "")

        self._run(show)

    def end_line(self, line: Text) -> None:
        """The end-of-turn facts, a blank row below the turn's last block."""

        def add() -> None:
            self.fold()
            self.app.transcript.add(SafeStatic(line, classes="turn-end"), "end")

        self._run(add)

    def code(self, lang: str, source: str) -> None:
        """A fenced code block, apart from the prose around it."""

        def add() -> None:
            self.fold()
            self.app.transcript.add(CodeBlock(lang, source, self.app.look), "code")

        self._run(add)

    def tool_started(self, cid: str, title: Text, depth: int) -> None:
        """A card with a spinner and its own timer."""

        def start() -> None:
            card = ToolCard(cid, title, depth)
            self.cards[cid] = card
            self.app.transcript.add(card, "tools")

        self._run(start)

    def tool_row(self, cid: str, row: ToolRow) -> None:
        """The parts of a finished call, for its one-row layout (comes just before `tool_finished`)."""
        self.rows[cid] = row

    def tool_finished(self, cid: str, header: Text, body: list[RenderableType], full: list[RenderableType]) -> None:
        """The card shows its result; a call that never started gets a finished card."""
        row = self.rows.pop(cid, None)

        def finish() -> None:
            card = self.cards.pop(cid, None)
            if card is None:
                card = ToolCard(cid, Text(""), row.depth if row else 0)
                self.app.transcript.add(card, "tools")
                card.call_after_refresh(card.finish, header, body, full, row)
            else:
                card.finish(header, body, full, row)
            if row is not None and row.ok and not row.diff and row.depth == 0:
                self.run.append((card, row))
            else:
                self.fold()  # an edit or a failure ends the run and stays on screen
            self.app.status_soon()

        self._run(finish)

    def thought(self, summary: Text, body: Text, shown: bool) -> None:
        """A fold that opens in place."""
        def add() -> None:
            self.fold()
            self.app.transcript.add(ThinkingFold(summary, body, shown), "thought")

        self._run(add)

    def thought_timed(self, seconds: float, tokens: int, body: Text, shown: bool) -> None:
        """A `thought for 4s` fold that opens in place."""

        def add() -> None:
            self.fold()
            self.app.transcript.add(ThinkingFold(None, body, shown, seconds, tokens), "thought")

        self._run(add)

    def plan(self, entries: list[tuple[str, str]]) -> None:
        """The turn's single `PlanBlock`: created on the first plan update of a turn, then updated in place."""

        def show() -> None:
            block = self.plan_block
            if block is not None and block.is_attached:
                block.set_entries(entries)
                return
            self.fold()
            self.plan_block = PlanBlock(entries)
            self.app.transcript.add(self.plan_block, "plan")

        self._run(show)

    def subagent(self, sid: str, phase: str, name: str, task: str, tool: str, elapsed: float) -> None:
        """One `SubagentTree` per child agent: mounted at its start, then updated in place."""

        def show() -> None:
            tree = self.subagents.get(sid)
            if tree is None or not tree.is_attached:
                self.fold()
                tree = SubagentTree(name, task)
                self.subagents[sid] = tree
                self.app.transcript.add(tree, "tools")
            if phase == "tool":
                tree.add_tool(tool, elapsed)
            elif phase == "done":
                tree.finish(elapsed)
                self.subagents.pop(sid, None)

        self._run(show)

    def _stop_subagents(self) -> None:
        for tree in self.subagents.values():
            tree.stop()
        self.subagents.clear()

    def turn_started(self) -> None:
        """A new turn gets its own plan block; `TextualUi.turn_started` shows the working line."""

        def reset() -> None:
            self.plan_block = None

        self._run(reset)

    def turn_ended(self) -> None:
        """Running cards and subagents that never finished stop their timers; the last run of calls folds."""
        self._run(self.fold)
        self._run(self._stop_subagents)
        if not self.app.is_running:
            self.cards.clear()
            return
        for card in self.cards.values():
            card.finish(Text(f"{card.title.plain}  (no result)", style="dim"), [], [])
        self.cards.clear()

    @contextlib.contextmanager
    def suspended(self) -> Iterator[None]:
        """Nothing to pause: modals draw over the screen."""
        yield


