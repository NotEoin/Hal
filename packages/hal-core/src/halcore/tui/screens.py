"""Shared modal screens of the full-screen UIs: a filterable picker, a confirm, a choice, help, a diff view,
history search, the Ctrl+P palette and the effort and context popovers.

Every modal answers Esc with its safe result (no choice, No, the focused safe button), puts focus
on the safe answer when it opens, and draws text from callers as `Text` (via `SafeStatic` or
markup-free option lists), never as Textual markup.

`ask(app, screen)` awaits a modal from any task on the app's loop (the controller's turn loop is a
plain task, not a Textual worker, so it cannot use `push_screen_wait`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, LoadingIndicator, OptionList, TabbedContent, TabPane
from textual import events
from textual.widgets.option_list import Option

from . import fuzzy, palette
from .chrome import role_colours
from .render import clean
from .slash import Breakdown, PickItem
from .status import context_bar, short_tokens
from .theme import Glyphs
from .widgets import SafeStatic, Segment, Segmented

T = TypeVar("T")
# `$panel` is the theme's panel role: the `$hal-*` names exist only in Hal's themes, and tests run modals on a plain App
MODAL_CSS = """
ModalScreen { background: $background 70%; }
.modal-box {
    width: 80%; max-width: 100; height: auto; max-height: 90%;
    border: round $panel; background: $surface; padding: 0 1;
}
.modal-title { text-style: bold; color: $primary; height: 1; }
.modal-buttons { height: auto; align-horizontal: right; margin-top: 1; }
.modal-box .modal-buttons Button, .modal-box .modal-buttons Button.-primary, .modal-box .modal-buttons Button.-error,
.modal-box .modal-buttons Button.-warning, .modal-box .modal-buttons Button.-success {
    height: 1; min-width: 0; padding: 0; line-pad: 1; margin: 0 0 0 1; border: none; background: transparent;
    color: $foreground; text-style: none;
}
.modal-box .modal-buttons Button:hover { background: $boost; }
.modal-box .modal-buttons Button:focus { background: $primary; color: $background; text-style: bold; }
.modal-box .modal-buttons Button:disabled { color: $text-disabled; }
.muted { color: $text-muted; }
"""


async def ask(app: App, screen: ModalScreen[T]) -> T:
    """Pushes `screen` and waits for its result; works from any task on the app's loop."""
    future: asyncio.Future[T] = asyncio.get_running_loop().create_future()

    def done(result: T | None) -> None:
        if not future.done():
            future.set_result(result)  # type: ignore[arg-type]  # a modal may dismiss with None

    app.push_screen(screen, done)
    return await future


class PickerModal(ModalScreen[str | None]):
    """A list to choose from: type to filter, Up/Down and Enter or a click choose, Esc cancels.

    Disabled rows show why and cannot be chosen. `items` may be a coroutine factory: a loading
    row shows until it returns, so a slow listing never blocks the screen. Every row is one line,
    cut with an ellipsis, and the list has no border even when focused: Textual's `OptionList:focus`
    border appeared between mouse down and click, moving every row down one line, so a click
    chose the row above the one under the pointer.
    """

    DEFAULT_CSS = MODAL_CSS + """
    PickerModal { align: center middle; }
    PickerModal OptionList, PickerModal OptionList:focus { height: auto; max-height: 50vh; border: none; text-wrap: nowrap; text-overflow: ellipsis; }
    PickerModal Input { margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]
    label_w = 0  # labels are padded to this width so the notes line up; 0 leaves them unpadded

    def __init__(
        self, title: str, items: Sequence[PickItem] | Callable[[], Awaitable[Sequence[PickItem]]], current: str = "",
        *, filterable: bool = True,
    ) -> None:  # fmt: skip
        super().__init__()
        self.title_text = title
        self.items: list[PickItem] = [] if callable(items) else list(items)
        self.loader = items if callable(items) else None
        self.current = current
        self.filterable = filterable
        self.shown: list[PickItem] = []

    def compose(self) -> ComposeResult:
        """Title, filter, list (or loading row)."""
        with Vertical(classes="modal-box"):
            yield SafeStatic(Text(self.title_text), classes="modal-title")
            if self.filterable:
                yield Input(placeholder="type to filter", id="filter")
            if self.loader is not None:
                yield LoadingIndicator(id="loading")
            yield OptionList(markup=False, id="options")
            yield SafeStatic(Text("Enter choose  Esc cancel", style="dim"), classes="muted")

    async def on_mount(self) -> None:
        """Fills the list, or starts the loader."""
        self.refill()
        if self.loader is not None:
            self.run_worker(self._load(), exclusive=True)
        self.query_one("#filter" if self.filterable else "#options").focus()

    async def _load(self) -> None:
        assert self.loader is not None
        try:
            self.items = list(await self.loader())
        except Exception as exc:  # noqa: BLE001 - a failed listing is shown in the picker, never a crash
            self.items = [PickItem(f"could not list: {clean(exc)}", "", disabled=True)]
        self.query("#loading").remove()
        self.refill()

    def row(self, item: PickItem, query: str = "") -> Text:
        """One option line: a mark for the current value, the label with the letters `query` matched, then the note dimmed."""
        text = Text("* " if item.value == self.current and item.value else "  ")
        label = clean(item.label)
        text.append(label.ljust(self.label_w), style="dim" if item.disabled else "")
        hit = fuzzy.score(query, f"{label} {clean(item.note)}") if query else None
        accent = role_colours(getattr(self.app, "look", None)).get("accent", "")
        for i in hit[1] if hit else ():
            if i < len(label):
                text.stylize(f"bold {accent}".strip(), 2 + i, 3 + i)
        if item.note:
            text.append(f"  {clean(item.note)}", style="dim italic")
        return text

    def refill(self, query: str = "") -> None:
        """Shows the items that match `query`, best match first, the current one highlighted."""
        options = self.query_one("#options", OptionList)
        self.shown = fuzzy.rank(query, self.items, key=lambda i: f"{clean(i.label)} {clean(i.note)}")
        options.set_options([Option(self.row(i, query), disabled=i.disabled) for i in self.shown])
        here = next((n for n, i in enumerate(self.shown) if i.value == self.current and not i.disabled), None)
        if here is None:
            here = next((n for n, i in enumerate(self.shown) if not i.disabled), None)
        options.highlighted = here

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filters as you type."""
        self.refill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter chooses the highlighted row."""
        options = self.query_one("#options", OptionList)
        if options.highlighted is not None:
            self.choose(options.highlighted)

    def on_key(self, event) -> None:  # noqa: ANN001 - Textual's Key event
        """Up and Down in the filter move the list."""
        if self.filterable and event.key in ("up", "down"):
            options = self.query_one("#options", OptionList)
            (options.action_cursor_up if event.key == "up" else options.action_cursor_down)()
            event.stop()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter or a click on a row chooses it."""
        self.choose(event.option_index)

    def choose(self, index: int) -> None:
        """Dismisses with the value of row `index`, unless it is disabled."""
        if 0 <= index < len(self.shown) and not self.shown[index].disabled:
            self.dismiss(self.shown[index].value)

    def action_cancel(self) -> None:
        """Esc: no choice."""
        self.dismiss(None)


class HistorySearch(PickerModal):
    """Ctrl+R: earlier messages, newest first, fuzzy filtered; Enter answers the message, Ctrl+R moves to the next match.

    It sits above the message box, and the caller loads the answer there without sending it.
    """

    DEFAULT_CSS = MODAL_CSS + """
    HistorySearch { align: center bottom; }
    HistorySearch .modal-box { margin-bottom: 3; }
    HistorySearch OptionList, HistorySearch OptionList:focus { height: auto; max-height: 12; border: none; text-wrap: nowrap; text-overflow: ellipsis; }
    HistorySearch #history-input { height: 1; }
    HistorySearch #history-input SafeStatic { width: 2; }
    HistorySearch Input, HistorySearch Input:focus { border: none; height: 1; padding: 0; width: 1fr; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel"), Binding("ctrl+r", "next", "next match", show=False)]

    def __init__(self, items: Sequence[PickItem], prompt: str = "> ") -> None:
        super().__init__("history", items)
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        """Title, the prompt-marked filter, the matches."""
        with Vertical(classes="modal-box"):
            yield SafeStatic(Text("history"), classes="modal-title")
            with Horizontal(id="history-input"):
                yield SafeStatic(Text(self.prompt))
                yield Input(placeholder="type to search earlier messages", id="filter")
            yield OptionList(markup=False, id="options")
            yield SafeStatic(Text("Enter puts it in the message box  Ctrl+R next  Esc cancel", style="dim"), classes="muted")

    def action_next(self) -> None:
        """The next match down the list, wrapping at the end."""
        options = self.query_one("#options", OptionList)
        if options.option_count:
            options.highlighted = ((options.highlighted or 0) + 1) % options.option_count


class CommandPalette(PickerModal):
    """Ctrl+P: commands, stored sessions, files, models and settings in one fuzzy list at the top.

    `load` returns the `PaletteItem`s (it lists sessions, so the caller runs it in a thread); the
    answer is the chosen item's index as a string, mapped back by the caller through `entries`.
    """

    DEFAULT_CSS = MODAL_CSS + """
    CommandPalette { align: center top; }
    CommandPalette .modal-box { margin-top: 2; }
    CommandPalette OptionList, CommandPalette OptionList:focus { height: auto; max-height: 14; border: none; text-wrap: nowrap; text-overflow: ellipsis; }
    CommandPalette #palette-input { height: 1; }
    CommandPalette #palette-input SafeStatic { width: 2; }
    CommandPalette Input, CommandPalette Input:focus { border: none; height: 1; padding: 0; width: 1fr; }
    """
    KIND_W = 9  # "command" and "setting" plus a gap
    LABEL_MAX_W = 24  # the note column starts no further right than this, so long labels do not push it off

    def __init__(self, load: Callable[[], Awaitable[Sequence[palette.PaletteItem]]], prompt: str = "> ") -> None:
        super().__init__("palette", self._load_entries)
        self.load = load
        self.prompt = prompt
        self.entries: list[palette.PaletteItem] = []

    async def _load_entries(self) -> list[PickItem]:
        self.entries = list(await self.load())
        return [PickItem(e.label, str(n), e.note) for n, e in enumerate(self.entries)]

    def compose(self) -> ComposeResult:
        """The prompt-marked search line, then the matches."""
        with Vertical(classes="modal-box"):
            with Horizontal(id="palette-input"):
                yield SafeStatic(Text(self.prompt))
                yield Input(placeholder="commands, sessions, files, models, settings", id="filter")
            yield LoadingIndicator(id="loading")
            yield OptionList(markup=False, id="options")
            yield SafeStatic(Text("Enter choose  Esc cancel", style="dim"), classes="muted")

    def row(self, item: PickItem, query: str = "") -> Text:
        """The kind column faint, then the row as a picker draws it."""
        kind = self.entries[int(item.value)].kind if item.value.isdigit() and int(item.value) < len(self.entries) else ""
        text = Text(kind.ljust(self.KIND_W), style="dim")
        return text.append_text(super().row(item, query))

    def refill(self, query: str = "") -> None:
        """At most `PALETTE_ROWS` matches, best first."""
        options = self.query_one("#options", OptionList)
        self.shown = fuzzy.rank(query, self.items, key=lambda i: f"{clean(i.label)} {clean(i.note)}", limit=palette.PALETTE_ROWS)
        self.label_w = min(self.LABEL_MAX_W, max((len(clean(i.label)) for i in self.shown if i.note), default=0))
        options.set_options([Option(self.row(i, query), disabled=i.disabled) for i in self.shown])
        options.highlighted = next((n for n, i in enumerate(self.shown) if not i.disabled), None)


@dataclass(frozen=True)
class Choice:
    """One button of a `ChoiceModal`."""

    label: str
    value: str
    variant: str = "default"  # Textual button variant: default, primary, error, warning, success


class ChoiceModal(ModalScreen[str]):
    """A question with buttons; focus starts on `safe`, and Esc answers `cancel` (`safe` when not given).

    The two differ when the focused default acts, as "Keep for later" on leaving does: Esc then
    stays put instead of leaving. `attention` draws the heavy accent border the plan card keeps.
    """

    DEFAULT_CSS = MODAL_CSS + """
    ChoiceModal { align: center middle; }
    ChoiceModal .modal-box { width: auto; min-width: 50; }
    ChoiceModal .modal-box.attention { border: heavy $primary; }
    """
    BINDINGS = [Binding("escape", "safe", "cancel")]

    def __init__(
        self, title: str, body: Text | str, choices: Sequence[Choice], safe: str, *, cancel: str = "", attention: bool = False,
    ) -> None:  # fmt: skip
        super().__init__()
        self.title_text, self.body, self.choices, self.safe = title, body, list(choices), safe
        self.cancel = cancel or safe
        self.attention = attention

    def compose(self) -> ComposeResult:
        """Title, body and the buttons."""
        with Vertical(classes="modal-box attention" if self.attention else "modal-box"):
            yield SafeStatic(Text(self.title_text), classes="modal-title")
            with VerticalScroll(id="body"):
                yield SafeStatic(self.body)
            with Horizontal(classes="modal-buttons"):
                for c in self.choices:
                    yield Button(c.label, id=f"choice-{c.value}", variant=c.variant)  # type: ignore[arg-type]  # a fixed variant name

    def on_mount(self) -> None:
        """Focus on the safe answer."""
        self.query_one(f"#choice-{self.safe}", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """A button answers with its value."""
        event.stop()
        self.dismiss(str(event.button.id).removeprefix("choice-"))

    def action_safe(self) -> None:
        """Esc: the cancel answer."""
        self.dismiss(self.cancel)


class ConfirmModal(ChoiceModal):
    """A yes/no question: No has focus, Enter on it and Esc answer No."""

    def __init__(self, question: str, *, yes: str = "Yes", no: str = "No", title: str = "Confirm") -> None:
        super().__init__(title, Text(question), [Choice(no, "no"), Choice(yes, "yes", "primary")], "no")


async def confirm(app: App, question: str, **kwargs: str) -> bool:
    """True only when the human pressed the yes button."""
    return await ask(app, ConfirmModal(question, **kwargs)) == "yes"


@dataclass(frozen=True)
class HelpSection:
    """One tab of the help screen: rows of `(key or command, what it does)`."""

    title: str
    rows: Sequence[tuple[str, str]]


class HelpScreen(ModalScreen[None]):
    """Help in tabs, generated from the caller's own bindings and command table; Esc closes it."""

    DEFAULT_CSS = MODAL_CSS + "HelpScreen { align: center middle; } HelpScreen TabbedContent { height: auto; max-height: 30; }"
    BINDINGS = [Binding("escape", "close", "close"), Binding("f1", "close", show=False), Binding("q", "close", show=False)]

    def __init__(self, title: str, sections: Sequence[HelpSection]) -> None:
        super().__init__()
        self.title_text, self.sections = title, list(sections)

    def compose(self) -> ComposeResult:
        """One tab per section."""
        with Vertical(classes="modal-box"):
            yield SafeStatic(Text(self.title_text), classes="modal-title")
            with TabbedContent():
                for section in self.sections:
                    with TabPane(section.title), VerticalScroll():
                        width = max((len(k) for k, _ in section.rows), default=0) + 2
                        for key, what in section.rows:
                            row = Text(key.ljust(width), style="bold")
                            row.append(what)
                            yield SafeStatic(row)
            yield SafeStatic(Text("Esc closes", style="dim"), classes="muted")

    def action_close(self) -> None:
        """Closes help."""
        self.dismiss(None)


class DiffScreen(ModalScreen[None]):
    """A read-only view of one file's diff; the caller passes cleaned, coloured `Text`. Esc closes it."""

    DEFAULT_CSS = MODAL_CSS + "DiffScreen { align: center middle; } DiffScreen VerticalScroll { height: auto; max-height: 30; }"
    BINDINGS = [Binding("escape", "close", "close"), Binding("q", "close", show=False)]

    def __init__(self, path: str, body: Text) -> None:
        super().__init__()
        self.path, self.body = path, body

    def compose(self) -> ComposeResult:
        """The path, the hunks in a scroll and the key hint."""
        with Vertical(classes="modal-box"):
            yield SafeStatic(Text(self.path), classes="modal-title")
            with VerticalScroll():
                yield SafeStatic(self.body, id="diff-body")
            yield SafeStatic(Text("read only; Esc closes, /accept applies the copy", style="dim"), classes="muted")

    def action_close(self) -> None:
        """Closes the view."""
        self.dismiss(None)


class EffortPopover(ModalScreen[str | None]):
    """The effort levels the model supports, as a segmented row anchored at `(x, y)`; answers the chosen level.

    Left/Right move, Enter or a click chooses, Esc or a click outside answers None. Without levels
    it shows the reason instead. Levels are discrete, so there is no slider, and there is no
    temperature control: the session has no setter for it.
    """

    DEFAULT_CSS = MODAL_CSS + """
    EffortPopover { align: left top; background: $background 30%; }
    EffortPopover .modal-box { width: auto; min-width: 24; max-width: 60; }
    """
    BINDINGS = [Binding("escape", "close", "close"), Binding("enter", "choose", "choose")]

    def __init__(self, levels: Sequence[str], current: str, reason: str, x: int, y: int) -> None:
        super().__init__()
        self.levels, self.current, self.reason, self.at = list(levels), current or "default", reason, (x, y)

    def compose(self) -> ComposeResult:
        """A title, then the levels or the reason they are missing."""
        with Vertical(classes="modal-box"):
            yield SafeStatic(Text("effort"), classes="modal-title")
            if self.levels:
                options = [Segment(level, level) for level in ("default", *self.levels)]
                yield Segmented(options, self.current, id="effort-levels")
            else:
                yield SafeStatic(Text(f"not available: {clean(self.reason)}"), classes="muted")

    def on_mount(self) -> None:
        """Opens under the effort item, with the focus on the levels."""
        self.query_one(".modal-box").styles.offset = self.at
        for seg in self.query(Segmented):
            seg.focus()

    def on_click(self, event: events.Click) -> None:
        """A click on a level chooses it; a click outside the box closes."""
        if isinstance(event.widget, Segmented):
            if any(start <= event.x < end for start, end, _ in event.widget.spans()):
                self.action_choose()
        elif event.widget is self:
            self.dismiss(None)

    def action_choose(self) -> None:
        """Answers the selected level, or None when it is the current one."""
        chosen = next((seg.selected for seg in self.query(Segmented)), self.current)
        self.dismiss(chosen if chosen != self.current else None)

    def action_close(self) -> None:
        """Closes without a change."""
        self.dismiss(None)


CONTEXT_POPOVER_BAR = 32  # gauge cells in the context popover
GAUGE_EASE_S = 0.3  # the gauge fills to its value when the popover opens


class ContextPopover(ModalScreen[str | None]):
    """The context split, lockdown, and a Compact button; answers `"compact"` when it is pressed.

    The numbers come from `slash.context_breakdown`, the same object `/context` prints, so the two
    always agree. Compact is disabled while a turn runs. Esc or a click outside closes.
    """

    DEFAULT_CSS = MODAL_CSS + """
    ContextPopover { align: right top; background: $background 30%; }
    ContextPopover .modal-box { width: 48; margin: 1 1 0 0; }
    ContextPopover #compact-row { height: 1; margin-top: 1; }
    ContextPopover #compact-row Button { height: 1; min-width: 0; border: none; padding: 0 1; margin: 0 1 0 0; }
    """
    BINDINGS = [Binding("escape", "close", "close")]

    def __init__(self, breakdown: Breakdown, glyphs: Glyphs, *, busy: bool) -> None:
        super().__init__()
        self.b, self.glyphs, self.busy = breakdown, glyphs, busy

    def gauge(self, used: float) -> Text:
        """The bar filled to `used` tokens and the percent of the real value."""
        return Text(f"{context_bar(round(used), self.b.size, self.glyphs, CONTEXT_POPOVER_BAR)}  {self.b.percent}%")

    def compose(self) -> ComposeResult:
        """Gauge, split, lockdown, then Compact; or one line while the size is unknown."""
        b = self.b
        with Vertical(classes="modal-box"):
            yield SafeStatic(Text("context"), classes="modal-title")
            if not b.size:
                yield SafeStatic(Text("context size not reported yet"), classes="muted")
            else:
                yield SafeStatic(self.gauge(0 if self._eases() else b.used), id="context-gauge")
                rows = (
                    ("input side (system, tools, files)", f"~{short_tokens(b.prompt_side)}"),
                    ("model output kept in history", f"~{short_tokens(b.written)}"),
                    ("free", f"~{short_tokens(b.free)}"),
                )
                yield SafeStatic(Text("\n".join(f"{label:<34}{value:>8}" for label, value in rows)), id="context-split")
                if b.lockdown:
                    lock = f"lockdown {clean(b.lockdown)}" + (f" / paranoia {clean(b.paranoia)}" if b.paranoia else "")
                    yield SafeStatic(Text(lock), classes="muted")
            with Horizontal(id="compact-row"):
                yield Button("Compact", id="compact", disabled=self.busy or not b.size)
                yield SafeStatic(Text("summarise history" + (" (after this turn)" if self.busy else "")), classes="muted")

    def _eases(self) -> bool:
        motion = getattr(self.app, "motion", None)
        return motion is not None and motion.enabled

    def on_mount(self) -> None:
        """Fills the gauge with an ease when motion is on."""
        motion = getattr(self.app, "motion", None)
        if self.b.size and self._eases():
            gauge = self.query_one("#context-gauge", SafeStatic)
            self._tween = motion.tween(0, self.b.used, GAUGE_EASE_S, lambda v: gauge.update(self.gauge(v)))

    def on_unmount(self) -> None:
        """Stops the ease if the popover closes first."""
        tween = getattr(self, "_tween", None)
        if tween is not None:
            tween.cancel()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Compact answers `compact`."""
        if event.button.id == "compact":
            self.dismiss("compact")

    def on_click(self, event: events.Click) -> None:
        """A click outside the box closes."""
        if event.widget is self:
            self.dismiss(None)

    def action_close(self) -> None:
        """Closes without a change."""
        self.dismiss(None)
