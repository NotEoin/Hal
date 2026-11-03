"""The full-screen `hal code` and `hal chat`: a transcript, a composer, chips, a status bar and a side panel.

The turn loop is `halcore.tui.controller.Controller`, the same one the line UI runs; this module
is its `Ui` (`TextualUi`) and its output sink (`TextualSink`). Everything the agent or a tool says
arrives as renderables `Renderer` built from cleaned text and is mounted with `SafeStatic`, never
as Textual markup (see docs/dev/tui.md, "Rendering safety").

Layout: a one-row top bar (the HAL eye, project, then clickable model, mode, effort and context
gauge), the transcript (tool
calls appear as cards with their own timer while they run), a "working" line during a turn, the
composer (Enter sends, Alt+Enter or Ctrl+J adds a line, `/` and `@` open completions), the status
bar and the key hints. Ctrl+B (or a click on the handle right of the transcript) shows a side
panel, hidden on start at every width, with PLAN, CHANGES of the copy and CONTEXT sections; a drag
on the handle resizes it. `hal chat` leaves out what needs a project copy.

Design decisions:

- Messages typed while a turn runs wait in `AgentApp.inbox` and are sent after it ("queued").
- Scrolled up, the transcript stays put while output arrives and a "N new below" line under it
  counts what came in; a click on it, or scrolling to the bottom, follows the output again.
- Questions (confirm, plan, accept, approvals) are modals whose focus starts on the safe answer.
- A bare listing command (`/model`) opens a picker; the choice runs the same command again with
  the chosen value, so the picker adds no new path into the session.
- Ctrl+C clears the composer, else stops the turn and denies an open approval, else asks for a
  second press within `QUIT_WINDOW_S` before quitting; one stray Ctrl+C never ends a session.
- Typing always reaches the message box: a press on a chip or button gives the focus back to it,
  and a printable key that arrives while a card has focus moves to it.
- The top bar always fits the width (`chrome.fit_top`): on a narrow terminal the project becomes
  its basename, the model name is clipped, the gauge shrinks; model, mode, effort and the gauge
  are never pushed off screen where they cannot be clicked. Lockdown is in the status line.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

from halsandbox import isolated
from halsandbox.errors import SandboxError
from rich.console import RenderableType
from rich.syntax import Syntax
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Button, Checkbox, OptionList, TextArea
from textual.widgets.option_list import Option

from . import fuzzy, mentions, motion, palette, screens, tokens
from . import fallback
from .approval import ApprovalView
from .approvalmodal import ModalAsker, record_text
from .chrome import ChangesList, HalEye, role_colours, SideHandle, StatusLine, TopBar, TopInfo
from .controller import (
    PLAN_CHOICES, Controller, Sender, checkpoint_label, history_path, refresh_status, stop_words, turn_facts,
)
from .prompt import COMMAND_ARGUMENTS, PASTE_COLLAPSE_LINES, private_history
from .render import TurnStats, clean, clip, patch_text, split_patch
from .slash import KEY_HELP, PickItem, context_breakdown, effort_levels, effort_support, PickRequest, Result, TuiState, command_descriptions, commands_for
from .status import StatusInfo, context_bar, model_label, percent, segments
from .theme import Look, textual_themes
from .welcome import WelcomeView
from .transcript import TextualSink, ToolGroup, Transcript, UserMessage, plan_items
from .widgets import SafeStatic, ThinkingFold, ToolCard, toast

log = logging.getLogger(__name__)

QUIT_WINDOW_S = 2.0  # a second Ctrl+C within this many seconds quits
STATUS_EVERY_S = 2.0  # how often the slow status fields refresh while a turn runs
NOTIFY_MIN_S = 10.0  # a turn this long rings the bell when the window is not focused
NOTIFY_LONG_S = 60.0  # a turn this long notifies even when the terminal never reports focus (as the line UI)
OSC9 = "\x1b]9;Hal: turn finished\x07"  # fixed text: nothing from the model reaches the terminal title bar
POPUP_ROWS = 8
PREVIEW_MIN_W = 100  # below this width the completion list has the whole row and no preview
PREVIEW_TIMEOUT_S = 1.0  # a preview read that takes longer (a slow mount) shows a note instead
PALETTE_TIMEOUT_S = 5.0  # listing sessions and files on a slow disk; the palette then shows "could not list"
COMPACT_WIDTH = 90  # below this width or `COMPACT_HEIGHT` rows, blocks in a turn lose their blank row
COMPACT_HEIGHT = 30
SPARK_SAMPLES = 24  # seconds of tok/s kept for the sparkline
CONTEXT_BAR_CELLS = 14  # fits the narrowest side panel (`tokens.SIDE_MIN`) with the token counts
SIDE_PATHS_SHOWN = 12  # changed paths listed in the side panel before "+N more"
EFFORT_SETTLE_S = 0.6  # wheel notches closer together than this are one change: one /effort, not one per notch
HISTORY_ROW_CHARS = 120  # one row of the Ctrl+R list; the full message is what goes back in the box
TOAST_SHORT_S = 2.0  # a confirmation of something the user just did
TOAST_WAIT_S = 3.0  # "listing...": long enough to see while the picker loads
TOAST_LONG_S = 6.0  # a warning the user should have time to read
QUIT = object()  # the inbox item that ends `read_message`


# --- composer --------------------------------------------------------------------------------


class Composer(TextArea):
    """The message box: Enter sends, Alt+Enter or Ctrl+J adds a line, pastes of several lines become chips."""

    DEFAULT_CSS = """
    Composer { height: auto; min-height: 1; max-height: 10; border: none; padding: 0; background: $background; }
    Composer:focus { border: none; }
    Composer .text-area--placeholder { color: $hal-faint; }
    Composer .text-area--cursor-line { background: $background; }
    """
    NEWLINE_KEYS = ("alt+enter", "ctrl+j", "shift+enter")
    PLACEHOLDER_CODE = "Message Hal   / commands  @ files  Enter sends  Alt+Enter new line"
    PLACEHOLDER_CHAT = "Message Hal   / commands  Enter sends  Alt+Enter new line"

    def __init__(self, app_: AgentApp, history: list[str]) -> None:
        placeholder = self.PLACEHOLDER_CODE if app_.code else self.PLACEHOLDER_CHAT
        super().__init__("", soft_wrap=True, show_line_numbers=False, highlight_cursor_line=False,
                         placeholder=placeholder, id="composer")  # fmt: skip
        self.cursor_blink = app_.look.animations
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
            return  # Textual calls `TextArea._on_paste` next (it walks the MRO); calling it here too pasted twice
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


class PromptMark(SafeStatic):
    """The prompt glyph left of the composer: accent while the composer has focus, faint otherwise."""

    DEFAULT_CSS = """
    PromptMark { width: 2; height: 1; color: $hal-faint; text-style: bold; }
    PromptMark.focused { color: $primary; }
    """


class AttachmentChips(Widget):
    """The row under the composer: one chip per collapsed paste and per pending attachment; `x` removes it."""

    DEFAULT_CSS = """
    AttachmentChips { height: 1; padding: 0 0 0 2; display: none; }
    AttachmentChips.shown { display: block; }
    """

    def __init__(self, owner: AgentApp, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.owner = owner
        self.spans: list[tuple[int, int, str, int]] = []  # (start, end, kind, index) of each `x`

    def chips(self) -> list[tuple[str, str, int]]:
        """`(label, kind, index)` for each paste chip and attachment; labels are fixed words and counts."""
        out: list[tuple[str, str, int]] = []
        for n, chip in enumerate(self.owner.query_one(Composer).pastes):
            lines = chip.split(" ")[1] if chip.count(" ") >= 2 else "?"
            out.append((f"paste {lines} lines", "paste", n))
        kinds: dict[str, int] = {}
        for n, block in enumerate(self.owner.state.attachments):
            kind = "image" if block.get("type") == "image" else "text"
            kinds[kind] = kinds.get(kind, 0) + 1
            out.append((f"{kind} {kinds[kind]}", "attachment", n))
        return out

    def sync(self) -> None:
        """Shows the row only when there is a chip."""
        self.set_class(bool(self.chips()), "shown")
        self.refresh()

    def render(self) -> RenderableType:
        """`[ paste 42 lines x ] [ image 1 x ]`."""
        roles = tokens.ROLES.get(self.owner.look.theme, {}) if self.owner.look.color else {}
        chip = f"{roles['muted']} on {roles['surface']}" if roles else "reverse"
        text = Text(no_wrap=True, overflow="ellipsis")
        self.spans = []
        for label, kind, index in self.chips():
            if text:
                text.append(" ")
            text.append(f" {label} ", style=chip)
            start = text.cell_len
            text.append("x ", style=f"bold {chip}")
            self.spans.append((start, start + 1, kind, index))
        return text

    def on_click(self, event: events.Click) -> None:
        """A click on a chip's `x` removes that paste or attachment."""
        offset = event.get_content_offset(self)
        for start, end, kind, index in self.spans:
            if offset is not None and start <= offset.x < end:
                self.owner.remove_chip(kind, index)
                break
        self.owner.query_one(Composer).focus()


# --- modals ----------------------------------------------------------------------------------


class AcceptModal(ModalScreen[tuple[bool, bool]]):
    """Accept the copy: the report, the exact patch that will be applied, Cancel focused."""

    DEFAULT_CSS = screens.MODAL_CSS + """
    AcceptModal { align: center middle; }
    AcceptModal .modal-box { width: 95%; max-width: 140; height: 90%; }
    AcceptModal #patch { height: 1fr; min-height: 3; border: round $panel; }
    AcceptModal #report { height: auto; max-height: 25%; }
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
        self.app.transcript.activate_next()
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
            ], "keep", attention=True),
        )  # fmt: skip
        return choice if choice in PLAN_CHOICES else None

    async def review_accept(self, report: str, patch: str, flagged: bool) -> tuple[bool, bool]:
        """The accept dialog; any way of closing it other than Apply applies nothing."""
        result = await screens.ask(self.app, AcceptModal(report, patch, flagged))
        return result if isinstance(result, tuple) else (False, False)

    def approval_sender(self, bridge, on_note: Callable[[str], None]) -> Sender:  # noqa: ANN001 - a PermissionBridge
        """Approvals in the modal, one at a time."""
        self.asker = ModalAsker(
            self.app, bridge, on_note, ring=self.app.look.notify != "off", on_change=self.app.approval_changed,
            on_answer=self.approval_answered,
        )  # fmt: skip
        return self.asker.send

    def approval_answered(self, letter: str, view: ApprovalView) -> None:
        """One row in the turn saying what was answered; the answer itself is already with the bridge."""
        self.app.transcript.add(SafeStatic(record_text(letter, view, self.app.look), classes="approval-record"), kind="approval")

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
        faint = tokens.ROLES[self.app.look.theme]["faint"] if self.app.look.theme in tokens.ROLES else "dim"
        line = Text(sep.join(parts), style=faint)
        if self.app.controller.session.backend == "none":
            line.append(f"{sep}UNSANDBOXED", style="bold reverse")
        sink = self.app.controller.renderer.sink
        (getattr(sink, "end_line", None) or sink.write)(line)
        self.app.set_busy(False)
        if stats.elapsed >= NOTIFY_LONG_S or (stats.elapsed >= NOTIFY_MIN_S and not self.app.window_focused):
            self.app.notify_turn()

    def status_changed(self) -> None:
        """Redraws the status bar."""
        self.app.refresh_status()

    def welcome(self, state: TuiState, session: object) -> None:
        """The welcome view as the first transcript item, in place of the line UI's panel."""
        self.app.transcript.add(WelcomeView(state, session, self.app.look))


# --- the app ---------------------------------------------------------------------------------


class AgentApp(App[int]):
    """`hal code` or `hal chat` in full screen; `run_async` returns the exit status."""

    ENABLE_COMMAND_PALETTE = False  # Ctrl+P opens Hal's own command list
    CSS = """
    Screen { layout: vertical; }
    ScrollableContainer, OptionList, TextArea {
        scrollbar-size-vertical: 1; scrollbar-color: $hal-panel; scrollbar-color-hover: $hal-muted;
        scrollbar-color-active: $primary; scrollbar-background: $background; scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
    }
    #topbar { height: 1; padding: 0 1; }
    #compose { height: auto; padding: 0 1; border-top: solid $hal-panel; }
    #compose.ascii { border-top: ascii $hal-panel; }
    #topbar TopBar { margin-left: 2; }
    #main { height: 1fr; }
    #side { width: 34; display: none; padding: 0 1; }
    #side.shown { display: block; }
    #side .side-head { color: $hal-muted; }
    #side .side-gap { margin-top: 1; }
    #changes-actions { height: 1; }
    #changes-actions Button { height: 1; width: auto; min-width: 0; border: none; padding: 0 0 0 1; margin: 0;
                              background: $background; color: $hal-muted; text-style: none; }
    #changes-actions Button:hover { color: $primary; }
    #changes-actions Button:focus { background: $hal-surface; }
    #below { height: 1; padding: 0 1; color: $hal-faint; display: none; }
    #below:hover { color: $primary; }
    #working { height: 1; padding: 0 1; color: $primary; display: none; }
    #working.busy { display: block; }
    #complete-box { height: auto; max-height: 14; display: none; border: round $hal-panel; margin: 0 1; }
    #complete-box.shown { display: block; }
    #popup { height: auto; max-height: 12; width: 1fr; display: none; border: none; padding: 0;
             text-wrap: nowrap; text-overflow: ellipsis; }
    #popup.shown { display: block; }
    #preview { width: 1fr; height: auto; max-height: 12; display: none; padding: 0 1; border-left: solid $hal-panel; }
    #preview.shown { display: block; }
    #unsandboxed { height: 1; background: $error; color: $text; text-style: bold; padding: 0 1; display: none; }
    #unsandboxed.shown { display: block; }
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
        self.title = "Hal code" if self.code else "Hal chat"  # the terminal tab, not the class name
        self.ui = TextualUi(self)
        self.sink = TextualSink(self)
        self.inbox: asyncio.Queue[object] = asyncio.Queue()
        self.queued_count = 0
        self.waiting_messages: list[UserMessage] = []
        self.mounted = False
        self.busy = False
        self.window_focused = True
        self.last_ctrl_c = 0.0
        self.side_width = tokens.SIDE_WIDTH  # a drag on the handle changes it for the session
        self.wheel_effort = ""  # the level the wheel points at until it settles
        self.wheel_timer: Timer | None = None
        self.history_store = private_history(history_path())
        self.transcript = Transcript(id="transcript")
        self.popup = OptionList(markup=False, id="popup")
        self._tick = 0
        self._status_due = 0.0
        self.motion: motion.Ticker | None = None  # created on mount: it needs the app's timers
        self._working: list[motion.Handle] = []
        self.tok_samples: list[float] = []  # tok/s once a second during turns, for the status sparkline
        self.exit_status = 0
        self.state.review_accept = self.ui.review_accept
        if not look.animations:
            self.animation_level = "none"
        for theme in textual_themes():
            self.register_theme(theme)
        self.theme = look.textual_theme

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """The `$hal-*` colour roles, so the CSS parses before the theme is applied."""
        look = getattr(self, "look", None)  # Textual asks while `App.__init__` runs, before `look` is set
        theme = look.theme if look is not None and look.theme in tokens.ROLES else "dark"
        return tokens.css_variables(theme)

    def look_for(self, theme: str) -> Look:
        """This look with another theme."""
        from dataclasses import replace  # noqa: PLC0415 - only theme switches need it

        from .theme import PALETTES  # noqa: PLC0415

        return replace(self.look, theme=theme, palette=PALETTES[theme])

    # -- layout ----------------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Header, transcript and side panel, working line, completions, composer, status and keys."""
        with Horizontal(id="topbar"):
            yield HalEye(id="eye")
            yield TopBar(self.top_info(), id="top")
        yield SafeStatic(Text("UNSANDBOXED: this session runs without a box"), id="unsandboxed")
        with Horizontal(id="main"):
            yield self.transcript
            yield SideHandle(id="side-handle")
            with VerticalScroll(id="side"):
                yield SafeStatic("", id="side-plan", classes="side-head")
                yield SafeStatic("", id="todo")
                if self.code:
                    yield SafeStatic("", id="side-changes", classes="side-head side-gap")
                    yield ChangesList(id="changes")
                    with Horizontal(id="changes-actions"):
                        yield Button("diff", id="act-diff")
                        yield Button("accept...", id="act-accept")
                        yield Button("export", id="act-export")
                yield SafeStatic("", id="side-context", classes="side-head side-gap")
                yield SafeStatic("", id="context")
        yield self.transcript.below
        yield SafeStatic("", id="working")
        with Horizontal(id="complete-box"):
            yield self.popup
            yield SafeStatic("", id="preview")
        history = list(reversed(list(self.history_store.load_history_strings())))
        with Horizontal(id="compose"):
            yield PromptMark(Text(self.look.glyphs.prompt))
            yield Composer(self, history)
        yield AttachmentChips(self, id="chips")
        yield StatusLine(id="status")

    def top_info(self) -> TopInfo:
        """The top bar's data: surface, project, model, mode, effort and context use."""
        session = self.controller.session
        info = self.controller.live_info() if self.mounted else self.state.status
        where = str(getattr(session, "project", None) or self.state.cwd)
        home = str(Path.home())
        where = "~" + where[len(home):] if where.startswith(home) else where
        return TopInfo(
            surface=self.state.surface, project=where, copy=getattr(session, "iso", None) is not None,
            model=model_label(info), mode=info.mode if self.code else None, effort=self.wheel_effort or clean(info.effort) or "default",
            used=info.used, size=info.size,
        )  # fmt: skip

    async def on_mount(self) -> None:
        """Theme and the (hidden) side panel, then the controller loop."""
        self.mounted = True
        self.motion = motion.Ticker(self.set_interval, enabled=self.look.animations)
        self.query_one("#side").styles.width = tokens.side_width(self.side_width, self.size.width)
        if self.controller.session.backend == "none":
            self.query_one("#unsandboxed").add_class("shown")
        self.sink.flush()
        self.refresh_status()
        self.query_one("#compose").set_class(not self.look.unicode, "ascii")
        self.transcript.set_class(self.size.width < COMPACT_WIDTH or self.size.height < COMPACT_HEIGHT, "compact")
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
        self.history_store.store_string(raw)  # pasted text stays out of the file, as in the line UI
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
        message.set_queued(self.busy)
        self.transcript.jump()
        self.transcript.new_turn(message, ascii=not self.look.unicode)
        self.waiting_messages.append(message)
        self.inbox.put_nowait(line)
        self.queued_count = self.inbox.qsize() if self.busy else 0
        if self.busy:
            toast(self, "queued: it is sent when this turn ends", timeout=TOAST_SHORT_S)
        elif line.split(" ", 1)[0] in ("/model", "/effort") and self.state.choices is None and self.state.load_choices:
            toast(self, "listing the models of your sources...", timeout=TOAST_WAIT_S)
        self.refresh_status()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """The prompt mark lights up while the composer has focus."""
        self.query_one(PromptMark).set_class(isinstance(event.widget, Composer), "focused")

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        """The prompt mark dims when the composer loses focus."""
        if isinstance(event.widget, Composer):
            self.query_one(PromptMark).remove_class("focused")

    def remove_chip(self, kind: str, index: int) -> None:
        """Removes a paste chip (its text goes from the box too) or a pending attachment."""
        composer = self.query_one(Composer)
        if kind == "paste":
            chips = list(composer.pastes)
            if 0 <= index < len(chips):
                chip = chips[index]
                composer.pastes.pop(chip)
                composer.load_text(composer.text.replace(chip, ""))
        elif 0 <= index < len(self.state.attachments):
            self.state.attachments.pop(index)
        self.query_one(AttachmentChips).sync()

    @on(TextArea.Changed, "#composer")
    def composer_changed(self, event: TextArea.Changed) -> None:
        """Completions for `/commands`, their fixed arguments, and `@files` (code only)."""
        self.query_one(AttachmentChips).sync()
        composer = self.query_one(Composer)
        row, col = composer.cursor_location
        before = composer.document.get_line(row)[:col]
        items: list[tuple[Text, str]] = []  # (row, replacement of the current token)
        typed = before.rsplit(" ", 1)[-1]
        if row == 0 and before.startswith("/") and " " not in before:
            described = command_descriptions(self.state.user_commands, self.state.surface)
            width = max((len(n) for n in described), default=0) + 3
            for name, desc in described.items():
                if name.startswith(before[1:]):
                    label = self.match_row(f"/{name}", before, prefix=True)
                    label.append(" " * (width - len(name) - 1))
                    label.append(clean(desc), style=self.role("muted"))
                    items.append((label, f"/{name} "))
        elif row == 0 and before.startswith("/") and before.count(" ") == 1:
            name, _, partial = before[1:].partition(" ")
            items = [(self.match_row(w, partial, prefix=True), w) for w in COMMAND_ARGUMENTS.get(name, ()) if w.startswith(partial)]
        elif self.code and self.controller.workdir is not None:
            token = before.rsplit(" ", 1)[-1]
            if token.startswith("@"):
                found = mentions.match_files(self.controller.workdir, token[1:])
                items = [(self.match_row(f"@{p}", token), f"@{p} ") for p in found]
        # an exact match goes first, so Enter on "/mode" sends /mode instead of completing to /model
        items.sort(key=lambda item: item[1].strip() != typed)
        self.show_popup(items[:POPUP_ROWS * 3])

    def role(self, name: str) -> str:
        """The colour of role `name` in the current look, or "" (mono, or no colour)."""
        return role_colours(self.look).get(name, "")

    def match_row(self, label: str, typed: str, *, prefix: bool = False) -> Text:
        """`label` cleaned, with the letters `typed` matched in the accent (a prefix, or fuzzily)."""
        text = Text(clean(label))
        style = f"bold {self.role('accent')}".strip()
        if prefix:
            text.stylize(style, 0, min(len(typed), len(text)))
        elif (hit := fuzzy.score(typed, text.plain)) is not None:
            for i in hit[1]:
                text.stylize(style, i, i + 1)
        return text

    def show_popup(self, items: list[tuple[Text, str]]) -> None:
        """Shows completions, or hides the list when there are none."""
        self.completions = [r for _, r in items]
        if not items:
            self.hide_popup()
            return
        self.popup.set_options([Option(label) for label, _ in items])
        self.popup.add_class("shown")
        self.popup.display = True
        self.query_one("#complete-box").add_class("shown")
        self.popup.highlighted = 0
        self.show_preview()

    def hide_popup(self) -> None:
        """Hides the completion list and its preview."""
        self.popup.remove_class("shown")
        self.popup.display = False
        self.query_one("#complete-box").remove_class("shown")
        self.query_one("#preview").remove_class("shown")

    @on(OptionList.OptionHighlighted, "#popup")
    def popup_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Moving in the list updates the preview."""
        self.show_preview()

    def show_preview(self) -> None:
        """The highlighted file's first lines, or a command's arguments, beside the list on a wide screen."""
        preview = self.query_one("#preview", SafeStatic)
        index = self.popup.highlighted
        chosen = self.completions[index] if index is not None and index < len(self.completions) else ""
        name = chosen.strip()[1:] if chosen.startswith("/") else ""
        if self.size.width < PREVIEW_MIN_W or not (chosen.startswith("@") or COMMAND_ARGUMENTS.get(name)):
            preview.remove_class("shown")
            return
        preview.add_class("shown")
        if name:
            preview.update(Text("usage: /" + name + " " + "|".join(COMMAND_ARGUMENTS[name]), style=self.role("faint")))
        else:
            self.run_worker(self.load_preview(chosen.strip()[1:]), group="preview", exclusive=True)

    async def load_preview(self, rel: str) -> None:
        """Reads the preview off the loop with a timeout, then shows it highlighted as code (no markup)."""
        root = self.controller.workdir
        if root is None:
            return
        try:
            body = await asyncio.wait_for(asyncio.to_thread(mentions.preview, root, rel), PREVIEW_TIMEOUT_S)
        except TimeoutError:
            body = "(the preview took too long)"
        preview = self.query_one("#preview", SafeStatic)
        if body in (mentions.BINARY, mentions.NOT_PREVIEWABLE) or body.startswith("(the preview"):
            preview.update(Text(body, style=self.role("faint")))
            return
        lexer = Syntax.guess_lexer(rel)
        theme = self.look.palette.code
        preview.update(Syntax(clean(body), lexer, theme=theme, background_color="default", word_wrap=False))

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
        for handle in self._working:
            handle.cancel()
        self._working = []
        if busy:
            self.link_checkpoint()
            if self.motion is not None:
                self._working = [self.motion.subscribe(self.spin, motion.SPINNER_HZ), self.motion.every(1.0, self.tick)]
            self.tick()
        self.transcript.set_running(busy)
        self.update_eye()
        self.refresh_status()

    def approval_changed(self) -> None:
        """An approval opened or closed: the eye, the turn rail and the status line follow."""
        if not self.query(HalEye):
            return  # the asker's cleanup can run during shutdown, after the widgets are removed
        self.transcript.set_waiting(self.ui.asker is not None and self.ui.asker.current is not None)
        self.update_eye()
        self.refresh_status()

    def update_eye(self) -> None:
        """The eye's state: approval waiting, else thinking during a turn, else idle."""
        waiting = self.ui.asker is not None and self.ui.asker.current is not None
        self.query_one(HalEye).state = "approval" if waiting else ("thinking" if self.busy else "idle")

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
            if label == checkpoint_label(message.text):
                message.checkpoint = listed[-1].n
                self.waiting_messages.remove(message)
                return

    def spin(self, now: float) -> bool:
        """One spinner frame of the working line (motion on only)."""
        self._tick += 1
        self.draw_working()
        return self.busy

    def tick(self, now: float = 0.0) -> bool:
        """The working line's numbers each second; the slow status fields every `STATUS_EVERY_S`."""
        if not self.busy:
            return False
        self.draw_working()
        self.tok_samples = [*self.tok_samples, self.controller.renderer.stats.tok_s][-SPARK_SAMPLES:]
        self.refresh_status()
        if time.monotonic() >= self._status_due:
            self._status_due = time.monotonic() + STATUS_EVERY_S
            self.status_soon()
        return True

    def draw_working(self) -> None:
        """Spinner, elapsed time, tokens, speed and the queue of the working line."""
        g = self.look.glyphs
        stats = self.controller.renderer.stats
        sep = f" {g.sep} "
        spin = tokens.spinner_frame(g.spinner_frames, self._tick) if self.look.animations else g.running
        text = Text(f"{spin} working {stats.elapsed:.0f}s", style="bold")
        text.append(f"{sep}~{stats.tokens} tok{sep}{stats.tok_s:.0f} tok/s{sep}Esc stops", style="dim")
        if self.queued_count:
            text.append(f"{sep}{self.queued_count} queued", style="bold")
        self.query_one("#working", SafeStatic).update(text)

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
        parts = segments(info, self.look, where="status")
        if self.ui.asker is not None and self.ui.asker.current is not None:
            parts.append(("warn", f" {self.look.glyphs.sep} approval waiting"))
        self.query_one(StatusLine).show(parts, self.tok_samples, self.busy)
        self.query_one(TopBar).show(self.top_info())
        self.query_one(AttachmentChips).sync()
        self.refresh_side(info)

    def refresh_side(self, info: StatusInfo) -> None:
        """The side panel's sections: PLAN, CHANGES (code only) and CONTEXT, each a heading and its content."""
        todo = self.state.renderer.todo
        done = sum(1 for e in todo if e.get("status") == "completed")
        self.query_one("#side-plan", SafeStatic).update(Text(f"PLAN  {done} of {len(todo)}" if todo else "PLAN"))
        entries = self.state.renderer.plan_entries()
        plan = plan_items(entries, self.look.glyphs, role_colours(self.look)) if entries else Text("(no todo list)")
        self.query_one("#todo", SafeStatic).update(plan)
        self.query_one("#side-context", SafeStatic).update(Text(f"CONTEXT  {percent(info.used, info.size)}%" if info.size else "CONTEXT"))
        if info.size:
            bar = context_bar(info.used, info.size, self.look.glyphs, CONTEXT_BAR_CELLS)
            context = Text(f"  {bar}  {info.used} of {info.size}\n  /compact frees room")
        else:
            context = Text("  context size not reported yet", style="dim")
        self.query_one("#context", SafeStatic).update(context)
        if self.code:
            n = info.changes
            self.query_one("#side-changes", SafeStatic).update(Text(f"CHANGES  {n}" if n else "CHANGES"))
            listed = info.changed[:SIDE_PATHS_SHOWN]
            paths = Text("\n".join(f"  {clean(status)} {clip(clean(path))}" for status, path in listed))
            if len(info.changed) > SIDE_PATHS_SHOWN:
                paths.append(f"\n  +{len(info.changed) - SIDE_PATHS_SHOWN} more")
            if not n:
                paths = Text("  no changes in the copy yet", style="dim")
            elif not info.changed:
                paths = Text(f"  {n} changed path{'s' if n != 1 else ''} in the copy")
            self.query_one(ChangesList).show(paths, [path for _, path in listed] if n else [])

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
            toast(self, "stopping the turn", timeout=TOAST_SHORT_S)
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
            toast(self, "stopping the turn", timeout=TOAST_SHORT_S)

    def action_cycle_mode(self) -> None:
        """Shift+Tab: the next permission mode (code only)."""
        if not self.code:
            return
        mode = self.controller.cycle_mode()
        toast(self, f"mode: {mode}", timeout=TOAST_SHORT_S)
        self.refresh_status()

    def action_expand_all(self) -> None:
        """Ctrl+O: expands every tool card and thinking block when any is folded, else folds them all."""
        cards = [c for c in self.query(ToolCard) if c.expandable]
        folds = list(self.query(ThinkingFold))
        groups = list(self.query(ToolGroup))
        expand = any(not c.expanded for c in cards) or any(not f.open for f in [*folds, *groups])
        for group in groups:
            if group.open != expand:
                group.action_toggle()
        for card in cards:
            if card.expanded != expand:
                card.action_toggle()
        for fold in folds:
            if fold.open != expand:
                fold.action_toggle()

    def action_toggle_side(self) -> None:
        """Ctrl+B or a click on the handle: shows or hides the side panel (hidden on start at every width)."""
        self.show_side(not self.query_one("#side").has_class("shown"))

    def show_side(self, shown: bool) -> None:
        """Shows or hides the side panel; the handle draws its rule while it is open."""
        self.query_one("#side").set_class(shown, "shown")
        self.query_one(SideHandle).set_open(shown)

    def action_todo(self) -> None:
        """Ctrl+T: the side panel, scrolled to PLAN."""
        self.show_side(True)
        self.query_one("#side", VerticalScroll).scroll_home(animate=False)

    def on_side_handle_toggled(self, event: SideHandle.Toggled) -> None:
        """A click on the handle toggles the side panel."""
        self.action_toggle_side()

    def on_side_handle_dragged(self, event: SideHandle.Dragged) -> None:
        """A drag on the open panel's handle resizes it, clamped; the width holds for the session."""
        if not self.query_one("#side").has_class("shown"):
            return
        self.side_width = tokens.side_width(self.size.width - event.x - 1, self.size.width)
        self.query_one("#side").styles.width = self.side_width

    def on_changes_list_open(self, event: ChangesList.Open) -> None:
        """A click on a changed path opens its read-only diff."""
        self.run_worker(self.show_change(event.path), group="pick")

    async def show_change(self, path: str) -> None:
        """One changed file's hunks, cut on the host from the copy's whole patch, in a read-only dialog."""
        try:
            patch = await asyncio.to_thread(self.state.session.diff, None)  # type: ignore[union-attr]  # CHANGES exists only in code, which has a session
        except (SandboxError, OSError) as exc:
            toast(self, str(exc), title="diff not available", severity="error")
            return
        part = split_patch(patch).get(path)
        if not part:
            toast(self, "this path has no diff in the copy any more", title="diff not available")
            return
        await screens.ask(self, screens.DiffScreen(clip(clean(path)), patch_text(part, self.look)))
        self.query_one(Composer).focus()

    def action_clear_view(self) -> None:
        """Ctrl+L: clears the transcript view; the session keeps its history."""
        self.transcript.clear()

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
        """Ctrl+P: commands, sessions, files, models and settings in one fuzzy search."""
        self.run_worker(self.palette(), group="pick")

    async def palette_items(self) -> list[palette.PaletteItem]:
        """Every palette row; sessions and files are read from disk in a thread, once per open."""
        workdir = self.controller.workdir if self.code else None
        animations = self.motion.enabled if self.motion is not None else self.look.animations

        def gather() -> list[palette.PaletteItem]:
            names = mentions.index_files(workdir) if workdir is not None else []
            return palette.gather(self.state, animations=animations, workdir=workdir, file_names=names)

        return await asyncio.wait_for(asyncio.to_thread(gather), PALETTE_TIMEOUT_S)

    async def palette(self) -> None:
        """Does what the chosen row names: a command, a mention, a command to finish, or a UI switch."""
        screen = screens.CommandPalette(self.palette_items, f"{self.look.glyphs.prompt} ")
        chosen = await screens.ask(self, screen)
        if not chosen or not chosen.isdigit() or int(chosen) >= len(screen.entries):
            return
        item = screen.entries[int(chosen)]
        if item.action == "ui":
            self.palette_ui(item.value)
        elif item.action in ("insert", "compose"):
            composer = self.query_one(Composer)
            composer.insert(item.value) if item.action == "insert" else composer.load_text(item.value + " ")
            composer.move_cursor(composer.document.end)
            composer.focus()
        elif item.value in ("/help", "/quit"):
            self.action_help() if item.value == "/help" else self.action_leave()
        else:
            self.send(item.value)

    def palette_ui(self, which: str) -> None:
        """The palette's UI-only switches; animations last for this session only."""
        if which in (palette.UI_ANIMATIONS_ON, palette.UI_ANIMATIONS_OFF):
            on = which == palette.UI_ANIMATIONS_ON
            if self.motion is not None:
                self.motion.enabled = on
            self.animation_level = "full" if on else "none"
        elif which == palette.UI_SIDE:
            self.action_toggle_side()
        elif which == palette.UI_EXPAND:
            self.action_expand_all()

    def action_history(self) -> None:
        """Ctrl+R: earlier messages, newest first, in a fuzzy search; the choice goes into the composer unsent."""
        composer = self.query_one(Composer)
        seen: list[str] = []
        for text in reversed(composer.sent):
            if text not in seen:
                seen.append(text)
        items = [PickItem(t.replace("\n", " ")[:HISTORY_ROW_CHARS], t) for t in seen]
        self.run_worker(self.history_pick(items), group="pick")

    async def history_pick(self, items: list[PickItem]) -> None:
        """Puts the chosen message in the composer."""
        chosen = await screens.ask(self, screens.HistorySearch(items, f"{self.look.glyphs.prompt} "))
        if chosen:
            self.query_one(Composer).load(chosen)

    async def open_pick(self, pick: PickRequest) -> None:
        """A picker for a bare listing command; the choice runs the same command with the value."""
        chosen = await screens.ask(self, screens.PickerModal(pick.title, list(pick.items), pick.current))
        if chosen:
            self.send(pick.command_for(chosen))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Side panel buttons."""
        which = str(event.button.id)
        commands = {"act-diff": "/diff", "act-accept": "/accept", "act-export": "/export"}
        if which in commands:
            self.send(commands[which])
        self.query_one(Composer).focus()  # the next thing typed is a message, not another press of the button

    @on(WelcomeView.Resume)
    def welcome_resume(self, event: WelcomeView.Resume) -> None:
        """A recent session row: `/resume ID`, then typing goes to the message box again."""
        self.send(event.command)
        self.query_one(Composer).focus()

    @on(TopBar.Clicked)
    def top_clicked(self, event: TopBar.Clicked) -> None:
        """Model and mode open their pickers, effort its levels, the gauge the context breakdown."""
        commands = {"model": "/model", "mode": "/mode"}
        if event.key == "effort":
            self.run_worker(self.effort_popover(), group="pick")
        elif event.key == "gauge":
            self.run_worker(self.context_popover(), group="pick")
        elif event.key in commands and not (event.key == "mode" and not self.code):
            self.send(commands[event.key])
        elif event.key == "copy":
            toast(self, "Edits go to an isolated copy; /accept applies them to your project.", timeout=TOAST_LONG_S)
        self.query_one(Composer).focus()  # the next thing typed is a message, not another click

    async def effort_popover(self) -> None:
        """The effort levels under the effort item; a choice runs `/effort LEVEL` like typing it."""
        levels, reason = await effort_levels(self.state)
        bar = self.query_one(TopBar)
        x = bar.region.x + bar.spans.get("effort", (0, 0))[0]
        chosen = await screens.ask(self, screens.EffortPopover(levels, self.state.effort, reason, x, bar.region.bottom))
        if chosen:
            self.send(f"/effort {chosen}")
        self.query_one(Composer).focus()

    async def context_popover(self) -> None:
        """The context breakdown `/context` prints, with a Compact button that runs `/compact`."""
        popover = screens.ContextPopover(context_breakdown(self.state), self.look.glyphs, busy=self.busy)
        if await screens.ask(self, popover) == "compact":
            self.send("/compact")
        self.query_one(Composer).focus()

    @on(TopBar.Wheeled)
    def top_wheeled(self, event: TopBar.Wheeled) -> None:
        """The wheel steps effort while idle, committed once it settles; model and mode never react.

        A wheel on the model would switch (maybe load) a model and on the mode could make edits
        pass without asking, so an accidental scroll there must change nothing.
        """
        if event.key != "effort" or self.busy:
            return
        levels, _ = effort_support(self.state)
        if not levels:
            return
        options = ("default", *levels)
        here = self.wheel_effort or self.state.effort or "default"
        at = options.index(here) if here in options else 0
        self.wheel_effort = options[max(0, min(len(options) - 1, at + event.step))]
        self.query_one(TopBar).show(self.top_info())
        if self.wheel_timer is not None:
            self.wheel_timer.stop()
        self.wheel_timer = self.set_timer(EFFORT_SETTLE_S, self.commit_wheel)

    def commit_wheel(self) -> None:
        """Sends the level the wheel settled on, unless it is the current one or a turn started."""
        level, self.wheel_effort, self.wheel_timer = self.wheel_effort, "", None
        if level and level != (self.state.effort or "default") and not self.busy:
            self.send(f"/effort {level}")
        self.query_one(TopBar).show(self.top_info())

    def on_click(self, event: events.Click) -> None:
        """A click on the "new below" line jumps to the newest output."""
        if event.widget is self.transcript.below:
            self.transcript.jump()

    def on_key(self, event: events.Key) -> None:
        """A printable key that reached the app (focus on a card or a chip) goes into the message box."""
        composer = self.query_one(Composer)
        if isinstance(self.screen, ModalScreen) or composer.has_focus or not event.is_printable or not event.character:
            return
        composer.focus()
        composer.insert(event.character)
        event.stop()

    async def message_actions(self, message: UserMessage) -> None:
        """Copy, Edit, Send again, or (in code, with a checkpoint) Rewind to before the message."""
        choices = [screens.Choice("Cancel", "cancel"), screens.Choice("Copy", "copy"), screens.Choice("Edit", "edit"),
                   screens.Choice("Send again", "again")]  # fmt: skip
        if self.code and message.checkpoint is not None:
            choices.append(screens.Choice("Rewind files to before this", "rewind", "warning"))
        choice = await screens.ask(self, screens.ChoiceModal("Message", Text(clean(message.text)), choices, "cancel"))
        if choice in ("copy", "edit", "again", "rewind"):
            self.message_action(message, choice)

    def message_action(self, message: UserMessage, name: str) -> None:
        """Runs one message action: `edit` and `again` leave the earlier turn in the agent's history."""
        if name == "copy":
            self.copy_to_clipboard(message.text)
            toast(self, "copied", timeout=TOAST_SHORT_S)
        elif name == "edit":
            composer = self.query_one(Composer)
            composer.load(message.text)
            composer.focus()
        elif name == "again":
            self.send(message.text)
        elif name == "rewind" and self.code and message.checkpoint is not None:
            self.send(f"/rewind {message.checkpoint}")

    def action_quit_or_delete(self) -> None:
        """Ctrl+D: deletes forward in a non-empty composer, else quits (like a shell)."""
        composer = self.query_one(Composer)
        if composer.has_focus and composer.text:
            composer.action_delete_right()
        elif not isinstance(self.screen, ModalScreen):
            self.action_leave()

    async def action_quit(self) -> None:
        """Textual's own Ctrl+Q leaves the same way as Ctrl+D: through the unaccepted-changes question."""
        if not isinstance(self.screen, ModalScreen):
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
        """A new terminal size: compact rhythm on small screens; the side panel keeps its share limit (the top bar fits itself)."""
        self.transcript.set_class(event.size.width < COMPACT_WIDTH or event.size.height < COMPACT_HEIGHT, "compact")
        if self.mounted:
            self.query_one("#side").styles.width = tokens.side_width(self.side_width, event.size.width)

    def notify_turn(self) -> None:
        """The end-of-turn signal `[ui].notify` asks for: nothing, the bell, or an OSC 9 notification."""
        if self.look.notify == "osc9":
            if not self.is_headless and self._driver is not None:
                self._driver.write(OSC9)  # noqa: SLF001 - Textual has no public raw write; `App.bell` does the same
        elif self.look.notify != "off":
            self.bell()

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
                toast(self.app, self.format(record), severity="warning", timeout=TOAST_LONG_S)


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
