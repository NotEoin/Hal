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
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll

from .render import clean
from .theme import Look
from .widgets import SafeStatic, ThinkingFold, ToolCard

if TYPE_CHECKING:
    from .agentapp import AgentApp


class Transcript(VerticalScroll):
    """The conversation; follows new output while the view is at the bottom."""

    DEFAULT_CSS = """
    Transcript { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    Transcript > .you { margin-top: 1; color: $primary; text-style: bold; }
    Transcript > .you:hover { background: $boost; }
    Transcript > .turn-end { color: $text-muted; margin-bottom: 1; }
    Transcript > .notice { color: $text-muted; }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.follow = True
        self.tail = SafeStatic("", id="tail")
        self.unseen = 0  # items added while the user was reading further up
        self.below = SafeStatic("", id="below")  # the "new output below" line, mounted by the app

    def compose(self) -> ComposeResult:
        """The streaming tail, kept last."""
        yield self.tail

    def add(self, widget: object) -> None:
        """Mounts `widget` above the streaming tail and keeps the view at the bottom when following."""
        self.mount(widget, before=self.tail)  # type: ignore[arg-type]  # a Widget
        if self.follow:
            self.call_after_refresh(self.scroll_end, animate=False)
        else:
            self.unseen += 1
            self.below.update(Text(f"v {self.unseen} new below: click here or scroll down to follow", style="bold"))
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
    """A message the human sent; a click offers Copy and (in code) Rewind to before it."""

    def __init__(self, text: str, look: Look) -> None:
        shown = Text(f"{look.glyphs.prompt} ", style="bold")
        shown.append(clean(text))
        super().__init__(shown, classes="you")
        self.text = text
        self.checkpoint: int | None = None  # the checkpoint taken just before this message ran

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


