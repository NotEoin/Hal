"""The transcript of the full-screen `hal code` and `hal chat` and the sink that fills it.

`Transcript` is the scrolling conversation; `TextualSink` is the `render.Sink` that mounts what
`Renderer` built (renderables made from cleaned text, never Textual markup) into it. Updates
arrive on the ACP reader task and run on the app's loop through `App.call_later`, in order.
`UserMessage` is a message the human sent. The app (`agentapp.AgentApp`) owns these widgets.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from rich.console import RenderableType
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widget import Widget

from . import tokens
from .render import ToolRow, clean
from .theme import Look
from .widgets import SafeStatic, ThinkingFold, ToolCard

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
    Transcript { height: 1fr; padding: 0 1 0 1; }
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


class UserMessage(SafeStatic):
    """A message the human sent: the prompt mark, then the text with a hanging indent; a click offers actions."""

    DEFAULT_CSS = """
    UserMessage { height: auto; }
    UserMessage:hover { background: $hal-surface; }
    """

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

    async def on_click(self) -> None:
        """Offers the message actions."""
        app = self.app
        actions = getattr(app, "message_actions", None)
        if actions is not None:
            app.run_worker(actions(self), group="message")


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

    def __init__(self, cards: list[ToolCard], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.cards = cards
        self.open = False

    @property
    def rows(self) -> list[ToolRow]:
        """The folded calls' parts."""
        return [c.row for c in self.cards if c.row is not None]

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


class TextualSink:
    """The `render.Sink` of the full-screen UI: mounts what the renderer built into the transcript."""

    def __init__(self, app: AgentApp) -> None:
        self.app = app
        self.cards: dict[str, ToolCard] = {}
        self.rows: dict[str, ToolRow] = {}  # parts of a finished call, from `tool_row`, until `tool_finished`
        self.pending: list[Callable[[], None]] = []  # output before the app has mounted
        self.run: list[ToolCard] = []  # finished calls in a row that may fold into a `ToolGroup`

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
        cards = [c for c in run if c.is_attached]
        if len(cards) < FOLD_MIN or len({id(c.parent) for c in cards}) != 1:
            return
        group = ToolGroup(cards)
        group.set_class(cards[0].has_class("gap"), "gap")
        cards[0].parent.mount(group, before=cards[0])  # type: ignore[union-attr]  # attached: has a parent
        for card in cards:
            card.display = False

    def write(self, renderable: RenderableType) -> None:
        """One finished item."""

        def add() -> None:
            self.fold()
            self.app.transcript.add(SafeStatic(renderable), "prose")

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
                self.run.append(card)
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

    def turn_started(self) -> None:
        """Nothing: `TextualUi.turn_started` shows the working line."""

    def turn_ended(self) -> None:
        """Running cards that never finished stop their timers; the last run of calls folds."""
        self._run(self.fold)
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


