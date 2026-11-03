"""The full-screen `hal code` and `hal chat`: a transcript, a composer, chips, a status bar and a side panel.

The turn loop is `halcore.tui.controller.Controller`, the same one the line UI runs; this module
is its `Ui` (`TextualUi`) and its output sink (`TextualSink`). Everything the agent or a tool says
arrives as renderables `Renderer` built from cleaned text and is mounted with `SafeStatic`, never
as Textual markup (see docs/dev/tui.md, "Rendering safety").

Layout: a header line with clickable chips (model, mode, effort, lockdown), the transcript (tool
calls appear as cards with their own timer while they run), a "working" line during a turn, the
composer (Enter sends, Alt+Enter or Ctrl+J adds a line, `/` and `@` open completions), the status
bar and the key hints. Ctrl+B shows a side panel with the todo list, the changes of the copy and
the context use. `hal chat` leaves out what needs a project copy.

Design decisions:

- Messages typed while a turn runs wait in `AgentApp.inbox` and are sent after it ("queued").
- Questions (confirm, plan, accept, approvals) are modals whose focus starts on the safe answer.
- A bare listing command (`/model`) opens a picker; the choice runs the same command again with
  the chosen value, so the picker adds no new path into the session.
- Ctrl+C clears the composer, else stops the turn and denies an open approval, else asks for a
  second press within `QUIT_WINDOW_S` before quitting; one stray Ctrl+C never ends a session.
- The header chips always fit the width: on a narrow terminal the model name is shortened, the
  read-only lockdown chip is left to the status bar, then the `model:` style prefixes go
  (`fit_header`); a chip is never pushed off screen where it cannot be clicked.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from halsandbox import isolated
from halsandbox.errors import SandboxError
from rich.console import RenderableType
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, OptionList, TabbedContent, TabPane, TextArea
from textual.widgets.option_list import Option

from .. import __version__
from . import mentions, screens
from . import fallback
from .approvalmodal import ModalAsker
from .controller import PLAN_CHOICES, Controller, Sender, history_path, refresh_status, stop_words, turn_facts
from .prompt import COMMAND_ARGUMENTS, PASTE_COLLAPSE_LINES, private_history
from .render import TurnStats, clean
from .slash import KEY_HELP, PickItem, PickRequest, Result, TuiState, command_descriptions, commands_for
from .status import context_bar, percent, segments, to_rich
from .theme import Look, textual_themes
from .widgets import SafeStatic, ThinkingFold, ToolCard, toast

log = logging.getLogger(__name__)

QUIT_WINDOW_S = 2.0  # a second Ctrl+C within this many seconds quits
WORKING_HZ = 4  # redraws per second of the "working" line
STATUS_EVERY_S = 2.0  # how often the slow status fields refresh while a turn runs
NOTIFY_MIN_S = 10.0  # a turn this long rings the bell when the window is not focused
POPUP_ROWS = 8
WIDE = 110  # at this width or more the side panel opens by default
SPIN = ("|", "/", "-", "\\")
CHIPS = ("model", "mode", "effort", "lockdown")
CHIP_CHROME = 3  # a chip's one-cell padding on each side plus its one-cell left margin
TITLE_MIN = 16  # "Hal 0.1.0  code" and the start of the project path
MODEL_CHIP_MAX = 32  # the longest model label a chip shows in full width
MODEL_CHIP_NARROW = 16
HEADER_LEVELS = 4  # 0 full, 1 shorter model, 2 no lockdown chip, 3 no prefixes
QUIT = object()  # the inbox item that ends `read_message`


# --- transcript ------------------------------------------------------------------------------


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

    def compose(self) -> ComposeResult:
        """The streaming tail, kept last."""
        yield self.tail

    def add(self, widget: object) -> None:
        """Mounts `widget` above the streaming tail and keeps the view at the bottom when following."""
        self.mount(widget, before=self.tail)  # type: ignore[arg-type]  # a Widget
        if self.follow:
            self.call_after_refresh(self.scroll_end, animate=False)

    def watch_scroll_y(self, old: float, new: float) -> None:
        """Following stops when the user scrolls up and starts again at the bottom."""
        super().watch_scroll_y(old, new)
        self.follow = new >= self.max_scroll_y - 1


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
        if isinstance(app, AgentApp):
            app.run_worker(app.message_actions(self), group="message")


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


# --- composer --------------------------------------------------------------------------------


class Composer(TextArea):
    """The message box: Enter sends, Alt+Enter or Ctrl+J adds a line, pastes of several lines become chips."""

    DEFAULT_CSS = """
    Composer { height: auto; min-height: 3; max-height: 10; border: round $panel; padding: 0 1; }
    Composer:focus { border: round $primary; }
    """
    NEWLINE_KEYS = ("alt+enter", "ctrl+j", "shift+enter")

    def __init__(self, app_: AgentApp, history: list[str]) -> None:
        super().__init__("", soft_wrap=True, show_line_numbers=False, highlight_cursor_line=False,
                         placeholder="Message Hal   (/ commands, Enter sends, Alt+Enter new line)", id="composer")  # fmt: skip
        self.owner = app_
        self.sent = history
        self.sent_at = len(history)
        self.pastes: dict[str, str] = {}
        self.recall: dict[str, dict[str, str]] = {}  # a sent text with chips -> the pastes behind them

    def load(self, text: str) -> None:
        """Puts an earlier message in the box, with the pasted text behind any chips it has."""
        self.load_text(text)
        self.pastes = dict(self.recall.get(text, {}))
        self.move_cursor(self.document.end)

    async def _on_key(self, event: events.Key) -> None:
        """Enter sends (or takes the highlighted completion); the newline keys add a line."""
        popup = self.owner.popup
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if popup.display and popup.highlighted is not None and self.owner.completion_changes_text():
                self.owner.take_completion()
            else:
                self.owner.submit()
            return
        if event.key in self.NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "tab" and popup.display:
            event.stop()
            event.prevent_default()
            self.owner.take_completion()
            return
        if event.key == "escape" and popup.display:
            event.stop()
            event.prevent_default()
            self.owner.hide_popup()
            return
        await super()._on_key(event)

    async def _on_paste(self, event: events.Paste) -> None:
        """A paste of `PASTE_COLLAPSE_LINES` lines or more becomes one `[pasted N lines #K]` chip."""
        data = event.text.replace("\r\n", "\n").replace("\r", "\n")
        lines = data.count("\n") + 1
        if lines < PASTE_COLLAPSE_LINES:
            await super()._on_paste(event)
            return
        event.stop()
        event.prevent_default()
        chip = f"[pasted {lines} lines #{len(self.pastes) + 1}]"
        self.pastes[chip] = data
        self.insert(chip)

    def action_delete_left(self) -> None:
        """Backspace right after a chip deletes the whole chip."""
        row, col = self.cursor_location
        before = self.document.get_line(row)[:col]
        for chip in self.pastes:
            if before.endswith(chip):
                self.delete((row, col - len(chip)), (row, col))
                return
        super().action_delete_left()

    def action_cursor_up(self, select: bool = False) -> None:
        """Up moves in the completion list, walks history on the first line, else moves the cursor."""
        if self.owner.popup.display:
            self.owner.popup.action_cursor_up()
        elif self.cursor_location[0] == 0 and not select and self.sent:
            self.sent_at = max(0, self.sent_at - 1)
            self.load(self.sent[self.sent_at])
        else:
            super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        """Down moves in the completion list, walks history on the last line, else moves the cursor."""
        if self.owner.popup.display:
            self.owner.popup.action_cursor_down()
        elif self.cursor_location[0] == self.document.line_count - 1 and not select and self.sent_at < len(self.sent):
            self.sent_at += 1
            self.load(self.sent[self.sent_at] if self.sent_at < len(self.sent) else "")
        else:
            super().action_cursor_down(select)

    def expand(self, text: str) -> str:
        """`text` with every chip replaced by the pasted text; the chips are cleared (and kept for recall)."""
        if self.pastes:
            self.recall[text] = dict(self.pastes)
        for chip, data in self.pastes.items():
            text = text.replace(chip, data)
        self.pastes.clear()
        return text


# --- modals ----------------------------------------------------------------------------------


class AcceptModal(ModalScreen[tuple[bool, bool]]):
    """Accept the copy: the report, the exact patch that will be applied, Cancel focused."""

    DEFAULT_CSS = screens.MODAL_CSS + """
    AcceptModal { align: center middle; }
    AcceptModal .modal-box { width: 95%; max-width: 140; }
    AcceptModal #patch { height: auto; max-height: 24; border: round $panel; }
    AcceptModal #report { height: auto; max-height: 8; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, report: str, patch: str, flagged: bool) -> None:
        super().__init__()
        self.report, self.patch, self.flagged = report, patch, flagged

    def compose(self) -> ComposeResult:
        """Report, patch, the high-risk checkbox when flagged, and the buttons."""
        with Vertical(classes="modal-box"):
            yield SafeStatic(Text("Apply the copy to your project?"), classes="modal-title")
            with VerticalScroll(id="report"):
                yield SafeStatic(Text(clean(self.report)))
            with VerticalScroll(id="patch"):
                for line in clean(self.patch).splitlines():
                    style = "green" if line.startswith("+") else ("red" if line.startswith("-") else "dim")
                    yield SafeStatic(Text(line, style=style))
            if self.flagged:
                yield Checkbox("also apply the high-risk changes listed above", id="allow-flags")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", id="cancel", variant="primary")
                yield Button("Apply exactly this patch", id="apply")

    def on_mount(self) -> None:
        """Cancel has focus."""
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Apply or Cancel."""
        event.stop()
        if event.button.id == "apply":
            ticked = bool(self.query("#allow-flags")) and self.query_one("#allow-flags", Checkbox).value
            self.dismiss((True, ticked))
        else:
            self.dismiss((False, False))

    def action_cancel(self) -> None:
        """Esc: nothing applied."""
        self.dismiss((False, False))


# --- the Ui ----------------------------------------------------------------------------------


class TextualUi:
    """The controller's `Ui` in the full-screen app."""

    def __init__(self, app: AgentApp) -> None:
        self.app = app
        self.asker: ModalAsker | None = None
        self.turn_started_at = 0.0

    async def read_message(self) -> str:
        """The next message from the composer (or a picker); `EOFError` once the app is quitting."""
        item = await self.app.inbox.get()
        if item is QUIT:
            raise EOFError
        self.app.queued_count = self.app.inbox.qsize()
        self.app.status_soon()
        return str(item)

    async def confirm(self, question: str) -> bool:
        """A yes/no modal with No focused."""
        return await screens.confirm(self.app, clean(question))

    async def choose_plan(self, state: TuiState) -> str | None:
        """The plan card: Keep planning has focus; only a button leaves plan mode."""
        body = Text(state.renderer.todo_text())
        choice = await screens.ask(
            self.app,
            screens.ChoiceModal("Run this plan?", body, [
                screens.Choice("Keep planning", "keep", "primary"), screens.Choice("Run in default mode", "default"),
                screens.Choice("Run in auto-edit", "auto-edit"),
            ], "keep"),
        )  # fmt: skip
        return choice if choice in PLAN_CHOICES else None

    async def review_accept(self, report: str, patch: str, flagged: bool) -> tuple[bool, bool]:
        """The accept dialog; any way of closing it other than Apply applies nothing."""
        result = await screens.ask(self.app, AcceptModal(report, patch, flagged))
        return result if isinstance(result, tuple) else (False, False)

    def approval_sender(self, bridge, on_note: Callable[[str], None]) -> Sender:  # noqa: ANN001 - a PermissionBridge
        """Approvals in the modal, one at a time."""
        self.asker = ModalAsker(self.app, bridge, on_note, ring=self.app.look.notify != "off")
        return self.asker.send

    def show(self, result: Result) -> None:
        """A picker for a listing command, a theme switch, else the text in the transcript."""
        if result.pick is not None:
            self.app.run_worker(self.app.open_pick(result.pick), group="pick")
            return
        if result.theme:
            self.app.theme = self.app.look_for(result.theme).textual_theme
        renderer = self.app.controller.renderer
        if result.renderable is not None:
            renderer.write(result.renderable)
        if result.message:
            renderer.info(result.message)

    def turn_started(self) -> None:
        """Shows the working line and Stop; remembers the checkpoint for Rewind."""
        self.turn_started_at = time.monotonic()
        self.app.set_busy(True)

    def turn_ended(self, stats: TurnStats, reason: str) -> None:
        """The end-of-turn line in plain words; a bell after a long turn when the window is not focused."""
        sep = f" {self.app.look.glyphs.sep} "
        parts = turn_facts(stats)
        words = stop_words(reason)
        if words:
            parts.append(words)
        line = Text(sep.join(parts), style="dim")
        if self.app.controller.session.backend == "none":
            line.append(f"{sep}UNSANDBOXED", style="bold reverse")
        self.app.controller.renderer.sink.write(line)
        self.app.set_busy(False)
        if stats.elapsed >= NOTIFY_MIN_S and not self.app.window_focused and self.app.look.notify != "off":
            self.app.bell()

    def status_changed(self) -> None:
        """Redraws the status bar."""
        self.app.refresh_status()


# --- the app ---------------------------------------------------------------------------------


class AgentApp(App[int]):
    """`hal code` or `hal chat` in full screen; `run_async` returns the exit status."""

    ENABLE_COMMAND_PALETTE = False  # Ctrl+P opens Hal's own command list
    CSS = """
    Screen { layout: vertical; }
    #header { height: 1; padding: 0 1; }
    #header .chip { height: 1; min-width: 6; border: none; margin-left: 1; background: $panel; }
    #header .chip:hover { background: $primary 40%; }
    #title { width: 1fr; }
    #main { height: 1fr; }
    #side { width: 38; border-left: solid $panel; display: none; }
    #side.shown { display: block; }
    #working { height: 1; padding: 0 1; color: $primary; display: none; }
    #working.busy { display: block; }
    #popup { height: auto; max-height: 10; display: none; border: round $panel; margin: 0 1; }
    #popup.shown { display: block; }
    #status { height: 1; padding: 0 1; background: $panel; }
    #unsandboxed { height: 1; background: $error; color: $text; text-style: bold; padding: 0 1; display: none; }
    #unsandboxed.shown { display: block; }
    #changes-actions { height: 1; }
    #changes-actions Button { height: 1; border: none; min-width: 8; margin-right: 1; }
    """
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Stop / clear", priority=True),
        Binding("ctrl+d", "quit_or_delete", "Quit", priority=True),
        Binding("escape", "stop", "Stop", show=False),
        Binding("shift+tab", "cycle_mode", "Mode", priority=True),
        Binding("ctrl+o", "expand_all", "Expand", show=False),
        Binding("ctrl+b", "toggle_side", "Panel"),
        Binding("ctrl+t", "todo", "Todo", show=False),
        Binding("ctrl+r", "history", "History", show=False),
        Binding("ctrl+l", "clear_view", "Clear view", show=False),
        Binding("ctrl+p", "palette", "Commands"),
        Binding("f1", "help", "Help"),
        Binding("pageup", "scroll(-1)", show=False, priority=True),
        Binding("pagedown", "scroll(1)", show=False, priority=True),
    ]

    def __init__(self, controller: Controller, look: Look) -> None:
        super().__init__()
        assert controller.state is not None
        self.controller = controller
        self.state: TuiState = controller.state
        self.look = look
        self.unicode = look.unicode
        self.code = self.state.surface == "code"
        self.ui = TextualUi(self)
        self.sink = TextualSink(self)
        self.inbox: asyncio.Queue[object] = asyncio.Queue()
        self.queued_count = 0
        self.waiting_messages: list[UserMessage] = []
        self.mounted = False
        self.busy = False
        self.window_focused = True
        self.last_ctrl_c = 0.0
        self.history_store = private_history(history_path())
        self.transcript = Transcript(id="transcript")
        self.popup = OptionList(markup=False, id="popup")
        self._tick = 0
        self._status_due = 0.0
        self.exit_status = 0
        self.state.review_accept = self.ui.review_accept
        if not look.animations:
            self.animation_level = "none"

    def look_for(self, theme: str) -> Look:
        """This look with another theme."""
        from dataclasses import replace  # noqa: PLC0415 - only theme switches need it

        from .theme import PALETTES  # noqa: PLC0415

        return replace(self.look, theme=theme, palette=PALETTES[theme])

    # -- layout ----------------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Header, transcript and side panel, working line, completions, composer, status and keys."""
        with Horizontal(id="header"):
            yield SafeStatic(self.title_text(), id="title")
            yield Button(self.chip_text("model"), id="chip-model", classes="chip")
            if self.code:
                yield Button(self.chip_text("mode"), id="chip-mode", classes="chip")
            yield Button(self.chip_text("effort"), id="chip-effort", classes="chip")
            yield Button(self.chip_text("lockdown"), id="chip-lockdown", classes="chip")
        yield SafeStatic(Text("UNSANDBOXED: this session runs without a box"), id="unsandboxed")
        with Horizontal(id="main"):
            yield self.transcript
            with Vertical(id="side"), TabbedContent(id="side-tabs"):
                with TabPane("Todo", id="side-todo"):
                    yield SafeStatic("", id="todo")
                if self.code:
                    with TabPane("Changes", id="side-changes"), Vertical():
                        yield SafeStatic("", id="changes")
                        with Horizontal(id="changes-actions"):
                            yield Button("Diff", id="act-diff")
                            yield Button("Accept...", id="act-accept")
                            yield Button("Export", id="act-export")
                with TabPane("Context", id="side-context"):
                    yield SafeStatic("", id="context")
        yield SafeStatic("", id="working")
        yield self.popup
        history = list(reversed(list(self.history_store.load_history_strings())))
        yield Composer(self, history)
        yield SafeStatic("", id="status")
        yield Footer()

    def title_text(self) -> Text:
        """`Hal 0.1.0 | code | ~/proj (isolated copy)`."""
        session = self.controller.session
        where = str(getattr(session, "project", None) or self.state.cwd)
        home = str(Path.home())
        where = "~" + where[len(home):] if where.startswith(home) else where
        copy = " (isolated copy)" if getattr(session, "iso", None) is not None else ""
        text = Text("Hal", style="bold")
        text.append(f" {__version__}  {self.state.surface}  {clean(where)}{copy}", style="dim")
        return text

    def chip_text(self, which: str, level: int = 0) -> Text:
        """The label of a header chip, built as `Text` so a model name is never markup; `level` compacts it."""
        info = self.state.status
        prefix = "" if level >= 3 else f"{which}: "
        if which == "model":
            text = Text(prefix + (clean(info.model) or "default"))
            text.truncate(len(prefix) + (MODEL_CHIP_NARROW if level >= 1 else MODEL_CHIP_MAX), overflow="ellipsis")
            return text
        if which == "mode":
            return Text(f"{prefix}{info.mode}")
        if which == "effort":
            return Text(f"{prefix}{clean(info.effort) or 'default'}")
        if not info.lockdown:
            return Text("lockdown")
        return Text(f"{prefix}{info.lockdown}" + (f"/{info.paranoia}" if info.paranoia else ""))

    def fit_header(self) -> None:
        """Picks the least compact chip labels that leave the title `TITLE_MIN` columns."""
        chips = {which: found.first(Button) for which in CHIPS if (found := self.query(f"#chip-{which}"))}
        level = 0
        for level in range(HEADER_LEVELS):
            shown = [w for w in chips if not (w == "lockdown" and level >= 2)]
            need = sum(self.chip_text(w, level).cell_len + CHIP_CHROME for w in shown)
            if need + TITLE_MIN <= self.size.width:
                break
        for which, chip in chips.items():
            chip.label = self.chip_text(which, level)
            if which == "lockdown":
                chip.display = level < 2

    async def on_mount(self) -> None:
        """Theme, the side panel on wide terminals, then the controller loop."""
        self.mounted = True
        for theme in textual_themes():
            self.register_theme(theme)
        self.theme = self.look.textual_theme
        if self.size.width >= WIDE:
            self.query_one("#side").add_class("shown")
        if self.controller.session.backend == "none":
            self.query_one("#unsandboxed").add_class("shown")
        self.sink.flush()
        self.refresh_status()
        self.set_interval(1 / WORKING_HZ, self.tick)
        self.query_one(Composer).focus()
        self.run_worker(self.loop(), group="controller", exit_on_error=False)

    async def loop(self) -> None:
        """Runs the controller until the human quits, then leaves the app."""
        try:
            self.exit_status = await self.controller.run(self.ui)
        finally:
            if self.ui.asker is not None:
                self.ui.asker.close()
            self.exit(self.exit_status)

    # -- composer and completion -----------------------------------------------------------------

    def submit(self) -> None:
        """Sends the composer's text: help and quit act here, everything else goes to the controller."""
        composer = self.query_one(Composer)
        raw = composer.text
        if not raw.strip():
            return
        text = composer.expand(raw)
        composer.clear()
        composer.sent.append(raw)
        composer.sent_at = len(composer.sent)
        self.history_store.store_string(text)  # the file keeps the real text: chips mean nothing next session
        self.hide_popup()
        line = text.strip()
        if line in ("/help", "?"):
            self.action_help()
            return
        if line in ("/quit", "/exit"):
            self.action_leave()
            return
        if line == "/clear":
            self.action_clear_view()
        self.send(line)

    def send(self, line: str) -> None:
        """Shows the message in the transcript and queues it for the controller."""
        message = UserMessage(line, self.look)
        self.transcript.follow = True
        self.transcript.add(message)
        self.waiting_messages.append(message)
        self.inbox.put_nowait(line)
        self.queued_count = self.inbox.qsize() if self.busy else 0
        if self.busy:
            toast(self, "queued: it is sent when this turn ends", timeout=2)
        elif line.split(" ", 1)[0] in ("/model", "/effort") and self.state.choices is None and self.state.load_choices:
            toast(self, "listing the models of your sources...", timeout=3)
        self.refresh_status()

    @on(TextArea.Changed, "#composer")
    def composer_changed(self, event: TextArea.Changed) -> None:
        """Completions for `/commands`, their fixed arguments, and `@files` (code only)."""
        composer = self.query_one(Composer)
        row, col = composer.cursor_location
        before = composer.document.get_line(row)[:col]
        items: list[tuple[str, str]] = []  # (label, replacement of the current token)
        if row == 0 and before.startswith("/") and " " not in before:
            for name, desc in command_descriptions(self.state.user_commands, self.state.surface).items():
                if name.startswith(before[1:]):
                    items.append((f"/{name}  {desc}", f"/{name} "))
        elif row == 0 and before.startswith("/") and before.count(" ") == 1:
            name, _, partial = before[1:].partition(" ")
            items = [(w, w) for w in COMMAND_ARGUMENTS.get(name, ()) if w.startswith(partial)]
        elif self.code and self.controller.workdir is not None:
            token = before.rsplit(" ", 1)[-1]
            if token.startswith("@"):
                items = [(f"@{p}", f"@{p} ") for p in mentions.match_files(self.controller.workdir, token[1:])]
        self.show_popup(items[:POPUP_ROWS * 3])

    def show_popup(self, items: list[tuple[str, str]]) -> None:
        """Shows completions, or hides the list when there are none."""
        self.completions = [r for _, r in items]
        if not items:
            self.hide_popup()
            return
        self.popup.set_options([Option(Text(clean(label))) for label, _ in items])
        self.popup.highlighted = 0
        self.popup.add_class("shown")
        self.popup.display = True

    def hide_popup(self) -> None:
        """Hides the completion list."""
        self.popup.remove_class("shown")
        self.popup.display = False

    def completion_target(self) -> tuple[str, int, int, int] | None:
        """`(replacement, row, start, end)` for the highlighted completion, or None."""
        index = self.popup.highlighted
        if index is None or index >= len(getattr(self, "completions", [])):
            return None
        replacement = self.completions[index]
        composer = self.query_one(Composer)
        row, col = composer.cursor_location
        before = composer.document.get_line(row)[:col]
        if before.startswith("/") and before.count(" ") == 1 and not replacement.startswith("/"):
            start = len(before) - len(before.split(" ", 1)[1])
        elif replacement.startswith("@"):
            start = len(before) - len(before.rsplit(" ", 1)[-1])
        else:
            start = 0
        return replacement, row, start, col

    def completion_changes_text(self) -> bool:
        """False when the typed token already is the highlighted completion (Enter then sends)."""
        target = self.completion_target()
        if target is None:
            return False
        replacement, row, start, end = target
        typed = self.query_one(Composer).document.get_line(row)[start:end]
        return typed.strip() != replacement.strip()

    def take_completion(self) -> None:
        """Replaces the token before the cursor with the highlighted completion."""
        target = self.completion_target()
        if target is None:
            return
        replacement, row, start, end = target
        self.query_one(Composer).replace(replacement, (row, start), (row, end))
        self.hide_popup()

    @on(OptionList.OptionSelected, "#popup")
    def popup_clicked(self, event: OptionList.OptionSelected) -> None:
        """A click on a completion takes it."""
        self.popup.highlighted = event.option_index
        self.take_completion()
        self.query_one(Composer).focus()

    # -- status ----------------------------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        """Shows or hides the working line."""
        self.busy = busy
        self.query_one("#working").set_class(busy, "busy")
        if busy:
            self.link_checkpoint()
        self.refresh_status()

    def link_checkpoint(self) -> None:
        """Gives the message this turn runs the checkpoint taken just before it, for Rewind."""
        checkpoints = getattr(self.controller.session, "checkpoints", None)
        if checkpoints is None or not self.waiting_messages:
            return
        try:
            listed = checkpoints.list()
        except (SandboxError, OSError) as exc:
            log.warning("checkpoints unavailable: %s", exc)
            return
        if not listed:
            return
        label = str(listed[-1].label)
        for message in self.waiting_messages:
            if label == f"before: {message.text[:40]}":
                message.checkpoint = listed[-1].n
                self.waiting_messages.remove(message)
                return

    def tick(self) -> None:
        """The working line's spinner and numbers; the slow status fields every `STATUS_EVERY_S`."""
        if not self.busy:
            return
        self._tick += 1
        stats = self.controller.renderer.stats
        sep = f" {self.look.glyphs.sep} "
        text = Text(f"{SPIN[self._tick % len(SPIN)]} working {stats.elapsed:.0f}s", style="bold")
        text.append(f"{sep}~{stats.tokens} tok{sep}{stats.tok_s:.0f} tok/s{sep}Esc stops", style="dim")
        if self.queued_count:
            text.append(f"{sep}{self.queued_count} queued", style="bold")
        self.query_one("#working", SafeStatic).update(text)
        self.refresh_status()
        if time.monotonic() >= self._status_due:
            self._status_due = time.monotonic() + STATUS_EVERY_S
            self.status_soon()

    def status_soon(self) -> None:
        """Refreshes the slow status fields (branch, changes, jobs) off the event loop."""
        if not self.is_running:
            return

        async def go() -> None:
            await refresh_status(self.state, self.controller.session)
            self.refresh_status()

        self.run_worker(go, group="status", exclusive=True)

    def refresh_status(self) -> None:
        """Redraws the status bar, chips and side panel from the state."""
        if not self.mounted:
            return
        info = self.controller.live_info()
        line = to_rich(segments(info, self.look), self.look)
        if self.ui.asker is not None and self.ui.asker.current is not None:
            line.append(f" {self.look.glyphs.sep} ", style="dim")
            line.append("approval waiting", style="bold reverse")
        self.query_one("#status", SafeStatic).update(line)
        self.fit_header()
        self.query_one("#todo", SafeStatic).update(Text(self.state.renderer.todo_text()))
        if info.size:
            bar = context_bar(info.used, info.size, self.look.glyphs, 24)
            context = Text(f"{bar} {percent(info.used, info.size)}%\n{info.used} of {info.size} tokens\n\n/compact frees room")
        else:
            context = Text("context size not reported yet", style="dim")
        self.query_one("#context", SafeStatic).update(context)
        if self.code:
            n = info.changes
            self.query_one("#changes", SafeStatic).update(
                Text(f"{n} changed path{'s' if n != 1 else ''} in the copy" if n else "no changes in the copy yet", style="" if n else "dim")
            )

    # -- actions ---------------------------------------------------------------------------------

    SCREEN_ONLY = frozenset({"scroll", "cycle_mode", "quit_or_delete", "expand_all", "toggle_side", "todo", "history",
                             "clear_view", "palette", "help", "stop"})  # fmt: skip

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """While a dialog is open its own keys win: PgDn scrolls its text, Shift+Tab moves its focus."""
        if action in self.SCREEN_ONLY and isinstance(self.screen, ModalScreen):
            return False
        return True

    def action_interrupt(self) -> None:
        """Ctrl+C: clear the composer, else stop the turn (denying an open approval), else quit on a second press."""
        composer = self.query_one(Composer)
        if isinstance(self.screen, ModalScreen):
            if self.ui.asker is not None and self.ui.asker.deny_open():
                return
            self.screen.dismiss(None)
            return
        if composer.text:
            composer.clear()
            return
        if self.busy:
            if self.ui.asker is not None:
                self.ui.asker.deny_open()
            self.run_worker(self.controller.cancel(), group="cancel")
            toast(self, "stopping the turn", timeout=2)
            return
        now = time.monotonic()
        if now - self.last_ctrl_c <= QUIT_WINDOW_S:
            self.action_leave()
            return
        self.last_ctrl_c = now
        toast(self, "press Ctrl+C again to quit (Ctrl+D quits too)", timeout=QUIT_WINDOW_S)

    def action_stop(self) -> None:
        """Esc: stops a running turn."""
        if self.busy:
            self.run_worker(self.controller.cancel(), group="cancel")
            toast(self, "stopping the turn", timeout=2)

    def action_cycle_mode(self) -> None:
        """Shift+Tab: the next permission mode (code only)."""
        if not self.code:
            return
        mode = self.controller.cycle_mode()
        toast(self, f"mode: {mode}", timeout=2)
        self.refresh_status()

    def action_expand_all(self) -> None:
        """Ctrl+O: expands every tool card and thinking block when any is folded, else folds them all."""
        cards = [c for c in self.query(ToolCard) if c.body is not None and c.body.foldable]
        folds = list(self.query(ThinkingFold))
        expand = any(not c.body.expanded for c in cards) or any(not f.open for f in folds)  # type: ignore[union-attr]  # filtered above
        for card in cards:
            if card.body.expanded != expand:  # type: ignore[union-attr]  # filtered above
                card.action_toggle()
        for fold in folds:
            if fold.open != expand:
                fold.action_toggle()

    def action_toggle_side(self) -> None:
        """Ctrl+B: the side panel."""
        self.query_one("#side").toggle_class("shown")

    def action_todo(self) -> None:
        """Ctrl+T: the todo tab of the side panel."""
        self.query_one("#side").add_class("shown")
        self.query_one("#side-tabs", TabbedContent).active = "side-todo"

    def action_clear_view(self) -> None:
        """Ctrl+L: clears the transcript view; the session keeps its history."""
        for child in list(self.transcript.children):
            if child is not self.transcript.tail:
                child.remove()

    def action_scroll(self, pages: int) -> None:
        """PgUp / PgDn scroll the transcript."""
        (self.transcript.scroll_page_up if pages < 0 else self.transcript.scroll_page_down)()

    def action_help(self) -> None:
        """F1: keys, commands and prefixes of this surface, generated from the same tables."""
        keys = [(b.key_display or b.key, b.description) for b in self.BINDINGS if b.description]
        keys = [("Enter / Alt+Enter", "send / new line"), ("Up / Down", "history on the first / last line"), *keys]
        commands = [(usage, text) for usage, text in commands_for(self.state.surface).values()]
        commands += [(f"/{clean(n)}", "your command from .hal/commands") for n in sorted(self.state.user_commands)]
        sections = [screens.HelpSection("Keys", keys), screens.HelpSection("Commands", commands)]
        if self.code:
            prefixes = [("@path", "attach a workspace file"), ("!cmd", "run a command in the box"), ("# note", "add a note to AGENTS.md")]
            sections.append(screens.HelpSection("Prefixes", prefixes))
        mouse = [("click", "chips, cards, buttons, messages"), ("wheel", "scroll"), ("Shift+drag", "select text")]
        sections.append(screens.HelpSection("Mouse", mouse))
        sections.append(screens.HelpSection("Line UI keys", [(k.strip(), "") for k in KEY_HELP[self.state.surface].split("|")]))
        self.push_screen(screens.HelpScreen(f"hal {self.state.surface}", sections))

    def action_palette(self) -> None:
        """Ctrl+P: every command of this surface in a filterable list."""
        items = [PickItem(usage, f"/{name}", text) for name, (usage, text) in commands_for(self.state.surface).items()]
        self.run_worker(self.palette(items), group="pick")

    async def palette(self, items: list[PickItem]) -> None:
        """Runs the chosen command (commands that need an argument go to the composer instead)."""
        chosen = await screens.ask(self, screens.PickerModal("Commands", items))
        if not chosen:
            return
        if chosen in ("/help", "/quit"):
            self.action_help() if chosen == "/help" else self.action_leave()
            return
        if chosen in ("/image",):
            composer = self.query_one(Composer)
            composer.load_text(chosen + " ")
            composer.move_cursor(composer.document.end)
            composer.focus()
            return
        self.send(chosen)

    def action_history(self) -> None:
        """Ctrl+R: earlier messages in a filterable list; the choice goes into the composer."""
        composer = self.query_one(Composer)
        seen: list[str] = []
        for text in reversed(composer.sent):
            if text not in seen:
                seen.append(text)
        items = [PickItem(t.replace("\n", " ")[:120], t) for t in seen]
        self.run_worker(self.history_pick(items), group="pick")

    async def history_pick(self, items: list[PickItem]) -> None:
        """Puts the chosen message in the composer."""
        chosen = await screens.ask(self, screens.PickerModal("History", items))
        if chosen:
            self.query_one(Composer).load(chosen)

    async def open_pick(self, pick: PickRequest) -> None:
        """A picker for a bare listing command; the choice runs the same command with the value."""
        chosen = await screens.ask(self, screens.PickerModal(pick.title, list(pick.items), pick.current))
        if chosen:
            self.send(pick.command_for(chosen))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Header chips and side panel buttons."""
        which = str(event.button.id)
        commands = {"chip-model": "/model", "chip-mode": "/mode", "chip-effort": "/effort",
                    "act-diff": "/diff", "act-accept": "/accept", "act-export": "/export"}  # fmt: skip
        if which in commands:
            self.send(commands[which])
        elif which == "chip-lockdown":
            toast(self, "Lockdown and paranoia are read only here: change them on the host with `hal config set`.")

    async def message_actions(self, message: UserMessage) -> None:
        """Copy, or (in code, with a checkpoint) Rewind to before the message."""
        choices = [screens.Choice("Cancel", "cancel"), screens.Choice("Copy", "copy")]
        if self.code and message.checkpoint is not None:
            choices.append(screens.Choice("Rewind files to before this", "rewind", "warning"))
        choice = await screens.ask(self, screens.ChoiceModal("Message", Text(clean(message.text)), choices, "cancel"))
        if choice == "copy":
            self.copy_to_clipboard(message.text)
            toast(self, "copied", timeout=2)
        elif choice == "rewind":
            self.send(f"/rewind {message.checkpoint}")

    def action_quit_or_delete(self) -> None:
        """Ctrl+D: deletes forward in a non-empty composer, else quits (like a shell)."""
        composer = self.query_one(Composer)
        if composer.has_focus and composer.text:
            composer.action_delete_right()
        elif not isinstance(self.screen, ModalScreen):
            self.action_leave()

    def action_leave(self) -> None:
        """Ctrl+D, /quit: quits, asking first when the copy has unaccepted changes."""
        self.run_worker(self.leave_flow(), group="leave", exclusive=True)

    async def leave_flow(self) -> None:
        """Keep for later (focused), Review and accept, Discard, or Cancel."""
        count = await asyncio.to_thread(self.controller.unaccepted) if self.code else 0
        if count:
            iso = self.controller.session.iso
            body = Text(f"{count} changed path{'s' if count != 1 else ''} in the copy are not in your project yet.\n"
                        f"Keep for later: `hal code accept {iso.sid}` applies them.")  # fmt: skip
            choice = await screens.ask(self, screens.ChoiceModal("Leave hal code?", body, [
                screens.Choice("Keep for later", "keep", "primary"), screens.Choice("Review and accept...", "review"),
                screens.Choice("Discard the copy...", "discard", "error"), screens.Choice("Cancel", "cancel"),
            ], "keep", cancel="cancel"))  # fmt: skip
            if choice in ("cancel", None):  # Esc, or Ctrl+C closing the dialog: stay
                return
            if choice == "review":
                self.send("/accept")
                return
            if choice == "discard":
                if not await screens.confirm(self, "Discard every change in the copy? This cannot be undone.", yes="Discard"):
                    return
                try:
                    await asyncio.to_thread(isolated.reject, iso)
                except (SandboxError, OSError) as exc:
                    toast(self, f"could not discard: {exc}", severity="error")
                    return
        self.inbox.put_nowait(QUIT)

    def on_resize(self, event: events.Resize) -> None:
        """A new terminal size: the chips are fitted again."""
        if self.mounted:
            self.fit_header()

    def on_app_focus(self, event: events.AppFocus) -> None:
        """The terminal window has focus."""
        self.window_focused = True

    def on_app_blur(self, event: events.AppBlur) -> None:
        """The terminal window lost focus."""
        self.window_focused = False


class NotifyHandler(logging.Handler):
    """Shows library warnings as toasts while the full screen owns the terminal (stderr would garble it)."""

    def __init__(self, app: AgentApp) -> None:
        super().__init__(logging.WARNING)
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        """One toast per warning."""
        with contextlib.suppress(Exception):  # noqa: BLE001 - logging must never raise into the app
            if self.app.mounted:
                toast(self.app, self.format(record), severity="warning", timeout=6)


async def run_full(controller: Controller, look: Look) -> tuple[int, AgentApp]:
    """Runs the full-screen app on a started controller; returns the exit status and the app."""
    app = AgentApp(controller, look)
    controller.renderer.sink = app.sink
    root = logging.getLogger()
    saved = list(root.handlers)
    handler = NotifyHandler(app)
    root.handlers = [handler]
    try:
        status = await app.run_async(mouse=look.mouse)
    except Exception as exc:
        exc.hal_app_mounted = app.mounted  # type: ignore[attr-defined]  # read by `app._run_full` to pick the fallback
        raise
    finally:
        root.handlers = saved
    # Textual catches an error inside the app, prints it on exit and returns None with `return_code` 1
    if app.return_code and not app.mounted:
        raise fallback.StartFailed(fallback.reason_of(app))
    if app.return_code:
        return app.return_code, app
    return (status if isinstance(status, int) else app.exit_status), app
