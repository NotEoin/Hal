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
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll

from . import tokens
from .render import clean
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
    TurnView > .turn-end { color: $hal-faint; }
    """

    def __init__(self, message: UserMessage, *, ascii: bool) -> None:
        super().__init__(classes="queued ascii" if ascii else "queued")
        self.message = message
        self.tail = SafeStatic("", classes="tail")

    def compose(self) -> ComposeResult:
        """The message, then the streaming tail (kept last)."""
        yield self.message
        yield self.tail

    def add(self, widget: object) -> None:
        """Mounts `widget` above this turn's streaming tail."""
        self.mount(widget, before=self.tail)  # type: ignore[arg-type]  # a Widget


class Transcript(VerticalScroll):
    """The conversation, one `TurnView` per message; follows new output while the view is at the bottom."""

    DEFAULT_CSS = """
    Transcript { height: 1fr; padding: 0 1 0 1; }
    Transcript > .notice { color: $hal-muted; }
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

    def add(self, widget: object) -> None:
        """Mounts `widget` in the active turn (or above the tail) and keeps the view at the bottom when following."""
        if self.active is not None and self.active.is_attached:
            self.active.add(widget)
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


class TextualSink:
    """The `render.Sink` of the full-screen UI: mounts what the renderer built into the transcript."""

    def __init__(self, app: AgentApp) -> None:
        self.app = app
        self.cards: dict[str, ToolCard] = {}
        self.pending: list[Callable[[], None]] = []  # output before the app has mounted

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

    def write(self, renderable: RenderableType) -> None:
        """One finished item."""
        self._run(lambda: self.app.transcript.add(SafeStatic(renderable)))

    def tail(self, renderable: RenderableType | None) -> None:
        """The streaming markdown block."""
        self._run(lambda: self.app.transcript.tail.update(renderable if renderable is not None else ""))

    def tool_started(self, cid: str, title: Text, depth: int) -> None:
        """A card with a spinner and its own timer."""

        def start() -> None:
            card = ToolCard(cid, title, depth)
            self.cards[cid] = card
            self.app.transcript.add(card)

        self._run(start)

    def tool_finished(self, cid: str, header: Text, body: list[RenderableType], full: list[RenderableType]) -> None:
        """The card shows its result; a call that never started gets a finished card."""

        def finish() -> None:
            card = self.cards.pop(cid, None)
            if card is None:
                card = ToolCard(cid, Text(""))
                self.app.transcript.add(card)
                card.call_after_refresh(card.finish, header, body, full)
            else:
                card.finish(header, body, full)
            self.app.status_soon()

        self._run(finish)

    def thought(self, summary: Text, body: Text, shown: bool) -> None:
        """A fold that opens in place."""
        self._run(lambda: self.app.transcript.add(ThinkingFold(summary, body, shown)))

    def turn_started(self) -> None:
        """Nothing: `TextualUi.turn_started` shows the working line."""

    def turn_ended(self) -> None:
        """Running cards that never finished stop their timers."""
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


