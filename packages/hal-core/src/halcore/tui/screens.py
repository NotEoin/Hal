"""Shared modal screens of the full-screen UIs: a filterable picker, a confirm, a choice and help.

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
from textual.widgets.option_list import Option

from .mentions import fuzzy
from .render import clean
from .slash import PickItem
from .widgets import SafeStatic

T = TypeVar("T")
MODAL_CSS = """
.modal-box {
    width: 80%; max-width: 100; height: auto; max-height: 90%;
    border: round $primary; background: $surface; padding: 0 1;
}
.modal-title { text-style: bold; color: $primary; height: 1; }
.modal-buttons { height: auto; align-horizontal: right; margin-top: 1; }
.modal-buttons Button { margin-left: 1; min-width: 10; }
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
    row shows until it returns, so a slow listing never blocks the screen.
    """

    DEFAULT_CSS = MODAL_CSS + """
    PickerModal { align: center middle; }
    PickerModal OptionList { height: auto; max-height: 20; border: none; }
    PickerModal Input { margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

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

    def row(self, item: PickItem) -> Text:
        """One option line: a mark for the current value, the label, then the note dimmed."""
        text = Text("* " if item.value == self.current and item.value else "  ")
        text.append(clean(item.label), style="dim" if item.disabled else "")
        if item.note:
            text.append(f"  {clean(item.note)}", style="dim italic")
        return text

    def refill(self, query: str = "") -> None:
        """Shows the items that match `query`, the current one highlighted."""
        options = self.query_one("#options", OptionList)
        self.shown = [i for i in self.items if fuzzy(query, f"{i.label} {i.note}")]
        options.set_options([Option(self.row(i), disabled=i.disabled) for i in self.shown])
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


@dataclass(frozen=True)
class Choice:
    """One button of a `ChoiceModal`."""

    label: str
    value: str
    variant: str = "default"  # Textual button variant: default, primary, error, warning, success


class ChoiceModal(ModalScreen[str]):
    """A question with buttons; focus starts on `safe`, and Esc answers `safe`."""

    DEFAULT_CSS = MODAL_CSS + "ChoiceModal { align: center middle; } ChoiceModal .modal-box { width: auto; min-width: 50; }"
    BINDINGS = [Binding("escape", "safe", "cancel")]

    def __init__(self, title: str, body: Text | str, choices: Sequence[Choice], safe: str) -> None:
        super().__init__()
        self.title_text, self.body, self.choices, self.safe = title, body, list(choices), safe

    def compose(self) -> ComposeResult:
        """Title, body and the buttons."""
        with Vertical(classes="modal-box"):
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
        """Esc: the safe answer."""
        self.dismiss(self.safe)


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
