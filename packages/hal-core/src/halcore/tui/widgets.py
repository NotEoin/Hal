"""Shared Textual widgets of the full-screen `hal code`, `hal chat` and `hal tune`.

Every widget here takes keys and the mouse: a slider you drag or step, one-of-N segment buttons,
a VRAM gauge, a tool-call card with its own timer, a diff view that folds, and `SafeStatic`.

Rendering safety (REQ-SEC-25): Textual markup can carry `[@click=...]` actions, so text that came
from the model, a tool or a config file never reaches Textual as markup. `SafeStatic` turns a
`str` into `rich.text.Text` from `clean()` output and has markup off; the other widgets draw
`Text` objects they build themselves. Labels that this program writes are fixed strings.

Design decisions:

- `Slider` maps positions to values with pure functions (`value_at`, `position_of`) so the maths
  is tested without a terminal. A log scale suits context lengths (8K to 128K); a linear one
  suits temperature. The knob follows a drag even outside the widget (the mouse is captured),
  and `Changed.final` is true only when the button is released or a key step lands, so costly
  listeners act once per change.
- A click on a slider's value, or Enter, opens `ValueEntry` to type an exact value (`parse` reads
  it, `32k` for a context); a value outside the range is refused with the range, never clamped
  silently.
- The wheel moves a slider only while it has focus; otherwise it scrolls the view around it, so
  wheeling down a page of sliders never changes a value the pointer happens to pass over.
- `Segmented` is one focusable widget, not a row of buttons, so Tab moves past it in one step
  and Left/Right move inside it, like a radio group. When its width is less than it asks for,
  it drops the space around each label so every option stays visible and clickable.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from rich.console import RenderableType
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.app import App
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Static

from . import tokens
from .render import clean

VALUE_WIDTH = 9  # columns kept for the value text right of a slider track
PAGE_CELLS = 10  # PgUp / PgDn move the knob this many cells
MIN_TRACK = 4
TIMER_S = 1.0  # a running tool card redraws its elapsed time this often
ASCII_TRACK = tokens.TRACK_ASCII
UNICODE_TRACK = tokens.TRACK_UNICODE
UNICODE_BAR = tokens.GAUGE_UNICODE
SPINNER = ("|", "/", "-", "\\")


def safe_text(content: RenderableType | str, style: str = "") -> RenderableType:
    """`content` ready for a widget: a `str` becomes cleaned `Text`; renderables pass unchanged."""
    if isinstance(content, str):
        return Text(clean(content), style=style)
    return content


def toast(widget: Widget | App, message: object, *, title: str = "", **kwargs: object) -> None:
    """A notification with markup off: `message` and `title` are cleaned text, never Textual markup."""
    plain = message.plain if isinstance(message, Text) else str(message)
    widget.notify(clean(plain), title=clean(title), markup=False, **kwargs)  # type: ignore[arg-type]  # notify's own keywords


class SafeStatic(Static):
    """A `Static` that never parses markup: strings are cleaned into `Text`, renderables pass as they are."""

    def __init__(self, content: RenderableType | str = "", **kwargs: object) -> None:
        super().__init__(safe_text(content), markup=False, **kwargs)  # type: ignore[arg-type]  # Static's own keywords

    def update(self, content: RenderableType | str = "", *, layout: bool = True) -> None:
        """Replaces the content; a `str` is cleaned first."""
        super().update(safe_text(content), layout=layout)


# --- slider ----------------------------------------------------------------------------------


class ValueEntry(ModalScreen[str | None]):
    """A one-line box to type a slider's value; Enter answers the text, Esc answers None."""

    DEFAULT_CSS = """
    ValueEntry { align: center middle; }
    ValueEntry > Vertical { width: 44; height: auto; border: round $primary; background: $surface; padding: 0 1; }
    ValueEntry .entry-title { text-style: bold; color: $primary; }
    ValueEntry .entry-hint { color: $text-muted; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, value: str, hint: str) -> None:
        super().__init__()
        self.title_text, self.value, self.hint = title, value, hint

    def compose(self) -> ComposeResult:
        """Title, the line and the range."""
        with Vertical():
            yield SafeStatic(Text(self.title_text), classes="entry-title")
            yield Input(self.value, id="entry")
            yield SafeStatic(Text(self.hint), classes="entry-hint")

    def on_mount(self) -> None:
        """The line has focus with its text selected, so typing replaces it."""
        entry = self.query_one("#entry", Input)
        entry.focus()
        entry.select_all()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter answers the typed text."""
        event.stop()
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        """Esc: no value."""
        self.dismiss(None)


def value_at(x: int, track: int, low: float, high: float, step: float, *, log: bool = False) -> float:
    """The value at cell `x` of a track `track` cells wide, snapped to `step` and clamped."""
    if track <= 1 or high <= low:
        return low
    t = min(1.0, max(0.0, x / (track - 1)))
    raw = low * (high / low) ** t if log and low > 0 else low + (high - low) * t
    return snap(raw, low, high, step)


def position_of(value: float, track: int, low: float, high: float, *, log: bool = False) -> int:
    """The cell of a track `track` cells wide where `value` sits."""
    if track <= 1 or high <= low:
        return 0
    value = min(high, max(low, value))
    if log and low > 0:
        t = math.log(value / low) / math.log(high / low)
    else:
        t = (value - low) / (high - low)
    return round(t * (track - 1))


def snap(value: float, low: float, high: float, step: float) -> float:
    """`value` rounded to a multiple of `step` from `low`, kept inside `[low, high]`."""
    if step > 0:
        value = low + round((value - low) / step) * step
    value = min(high, max(low, value))
    return round(value, 6)


class Slider(Widget, can_focus=True):
    """A numeric value on a track: drag, click, wheel, arrows, PgUp/PgDn, Home/End; `a` asks for auto."""

    DEFAULT_CSS = """
    Slider { height: 1; width: 1fr; min-width: 16; }
    Slider:focus { background: $boost; }
    Slider:disabled { color: $text-disabled; }
    """
    BINDINGS = [
        Binding("left", "step(-1)", "less", show=False),
        Binding("right", "step(1)", "more", show=False),
        Binding("pageup", "step(-10)", show=False),
        Binding("pagedown", "step(10)", show=False),
        Binding("home", "jump(0)", show=False),
        Binding("end", "jump(1)", show=False),
        Binding("a", "auto", "auto", show=False),
        Binding("enter", "edit", "type a value", show=False),
    ]

    value: reactive[float] = reactive(0.0)
    auto: reactive[bool] = reactive(False)

    @dataclass
    class Changed(Message):
        """The value moved; `final` when the drag ended or a key step landed; `auto` when auto was asked for."""

        slider: Slider
        value: float
        final: bool
        auto: bool = False

        @property
        def control(self) -> Slider:
            """The slider that changed."""
            return self.slider

    def __init__(
        self, value: float, low: float, high: float, step: float, *, log: bool = False,
        fmt: Callable[[float], str] = lambda v: f"{v:g}", allow_auto: bool = False, auto: bool = False,
        auto_label: Callable[[float], str] = lambda v: "auto", reason: str = "", title: str = "Value",
        parse: Callable[[str], float] = float, **kwargs: object,
    ) -> None:  # fmt: skip
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.low, self.high, self.step_size, self.log_scale = low, high, step, log
        self.fmt, self.allow_auto, self.auto_label = fmt, allow_auto, auto_label
        self.title, self.parse = title, parse
        self.reason = reason  # shown instead of the track when disabled
        self.set_reactive(Slider.value, snap(value, low, high, step))
        self.set_reactive(Slider.auto, auto)
        self._dragging = False

    @property
    def track(self) -> int:
        """Cells of the track at the current width."""
        return max(MIN_TRACK, self.size.width - VALUE_WIDTH - 1)

    def render(self) -> RenderableType:
        """The track with its knob, then the value."""
        if self.disabled and self.reason:
            return Text(clean(self.reason), style="italic")
        line, fill, knob = UNICODE_TRACK if getattr(self.app, "unicode", False) else ASCII_TRACK
        at = position_of(self.value, self.track, self.low, self.high, log=self.log_scale)
        bar = Text()
        bar.append(fill * at, style="bold" if not self.auto else "dim")
        bar.append(knob, style="bold reverse" if self.has_focus else "bold")
        bar.append(line * (self.track - at - 1), style="dim")
        label = self.auto_label(self.value) if self.auto else self.fmt(self.value)
        bar.append(" " + label.rjust(VALUE_WIDTH)[-VALUE_WIDTH:])
        return bar

    def set_value(self, value: float, *, final: bool, auto: bool = False) -> None:
        """Moves the knob and posts `Changed` when the value or the auto state changed (or on a final step)."""
        new = snap(value, self.low, self.high, self.step_size)
        changed = new != self.value or self.auto != auto
        self.value, self.auto = new, auto
        if changed or final:
            self.post_message(self.Changed(self, new, final, auto))

    def action_step(self, cells: int) -> None:
        """A key step: one `step` on a linear track, one cell on a log track."""
        if self.log_scale:
            at = position_of(self.value, self.track, self.low, self.high, log=True)
            target = value_at(at + cells, self.track, self.low, self.high, self.step_size, log=True)
            if target == self.value:
                target = self.value + self.step_size * (1 if cells > 0 else -1)
        else:
            target = self.value + self.step_size * cells
        self.set_value(target, final=True)

    def action_jump(self, end: int) -> None:
        """Home or End."""
        self.set_value(self.high if end else self.low, final=True)

    def action_auto(self) -> None:
        """Asks for the automatic value (only when the slider offers it)."""
        if self.allow_auto:
            self.set_value(self.value, final=True, auto=True)

    def action_edit(self) -> None:
        """Enter, or a click on the value: type an exact value."""
        if self.disabled:
            return
        hint = f"{self.fmt(self.low)} to {self.fmt(self.high)}; Enter sets it, Esc cancels"
        self.app.push_screen(ValueEntry(self.title, f"{self.value:g}", hint), self.typed)

    def typed(self, text: str | None) -> None:
        """Sets the typed value, or says why not; an empty answer changes nothing."""
        if not text or not text.strip():
            return
        try:
            value = self.parse(text.strip())
        except ValueError as exc:
            toast(self, str(exc) or f"not a number: {text}", title=f"{self.title} not changed", severity="error")
            return
        if not self.low <= value <= self.high:
            toast(self, f"choose {self.fmt(self.low)} to {self.fmt(self.high)}", title=f"{self.title} not changed",
                  severity="error")  # fmt: skip
            return
        self.set_value(value, final=True)

    def _from_x(self, x: int) -> float:
        return value_at(x, self.track, self.low, self.high, self.step_size, log=self.log_scale)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Jumps to the clicked cell and starts a drag; a click on the value opens the entry box."""
        if self.disabled:
            return
        self.focus()
        if event.x > self.track:
            event.stop()
            self.action_edit()
            return
        self.capture_mouse()
        self._dragging = True
        self.set_value(self._from_x(event.x), final=False)
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """Follows the drag, also outside the widget."""
        if self._dragging:
            self.set_value(self._from_x(event.x), final=False)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """Ends the drag; the value is final now."""
        if self._dragging:
            self._dragging = False
            self.capture_mouse(False)
            self.set_value(self._from_x(event.x), final=True)
            event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        """The wheel steps up while the slider has focus; else the view around it scrolls."""
        if self.has_focus and not self.disabled:
            self.action_step(1)
            event.stop()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        """The wheel steps down while the slider has focus; else the view around it scrolls."""
        if self.has_focus and not self.disabled:
            self.action_step(-1)
            event.stop()


# --- segmented -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """One option of a `Segmented`; a disabled one says why."""

    label: str
    value: str
    reason: str = ""  # non-empty: the option cannot be chosen, and this says why


class Segmented(Widget, can_focus=True):
    """One of N options in a row; Left/Right or a click choose, disabled options are skipped."""

    DEFAULT_CSS = """
    Segmented { height: 1; width: auto; }
    Segmented:focus { background: $boost; }
    """
    BINDINGS = [Binding("left", "move(-1)", show=False), Binding("right", "move(1)", show=False)]

    selected: reactive[str] = reactive("")

    @dataclass
    class Changed(Message):
        """The chosen option changed."""

        segmented: Segmented
        value: str

        @property
        def control(self) -> Segmented:
            """The widget that changed."""
            return self.segmented

    def __init__(self, segments: Sequence[Segment], selected: str = "", **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.segments = list(segments)
        self.set_reactive(Segmented.selected, selected)

    def spans(self, pad: int | None = None) -> list[tuple[int, int, Segment]]:
        """`(start, end, segment)` column spans of each option as drawn (`pad` spaces around each label)."""
        pad = self.pad() if pad is None else pad
        out, x = [], 0
        for seg in self.segments:
            width = len(clean(seg.label)) + 2 * pad
            out.append((x, x + width, seg))
            x += width + 1
        return out

    def pad(self) -> int:
        """1 when the full labels fit the widget's width, else 0 (the compact form)."""
        full = self.spans(1)
        width = self.size.width
        return 1 if not full or width == 0 or width >= full[-1][1] else 0

    def render(self) -> RenderableType:
        """The options as `[ label ]` cells; the chosen one reversed, disabled ones struck through."""
        text = Text()
        space = " " * self.pad()
        for i, (_, _, seg) in enumerate(self.spans()):
            if i:
                text.append(" ")
            style = "reverse bold" if seg.value == self.selected else ""
            if seg.reason:
                style = "dim strike"
            text.append(f"{space}{clean(seg.label)}{space}", style=style)
        return text

    def get_content_width(self, container: object, viewport: object) -> int:  # noqa: D102 - Textual's sizing hook
        spans = self.spans(1)
        return spans[-1][1] if spans else 1

    def choose(self, value: str) -> bool:
        """Selects `value` unless it is disabled or unknown; True when it was chosen."""
        seg = next((s for s in self.segments if s.value == value), None)
        if seg is None or seg.reason:
            if seg is not None:
                toast(self, seg.reason, title=seg.label, severity="warning")
            return False
        if value != self.selected:
            self.selected = value
            self.post_message(self.Changed(self, value))
        return True

    def action_move(self, step: int) -> None:
        """Moves to the next enabled option."""
        enabled = [s.value for s in self.segments if not s.reason]
        if not enabled:
            return
        here = enabled.index(self.selected) if self.selected in enabled else -1
        self.choose(enabled[(here + step) % len(enabled)])

    def on_click(self, event: events.Click) -> None:
        """Chooses the clicked option."""
        for start, end, seg in self.spans():
            if start <= event.x < end:
                self.choose(seg.value)
                break


# --- gauge -----------------------------------------------------------------------------------


GAUGE_STYLE = {"gpu": "success", "tight": "warning", "hybrid": "warning", "cpu": "secondary", "does-not-fit": "error"}


class FitGauge(Widget):
    """A memory bar: used against budget, coloured by fit status (never by parsing words)."""

    DEFAULT_CSS = "FitGauge { height: 1; width: 1fr; }"

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.used = self.budget = 0
        self.status = ""
        self.caption = ""

    def show(self, used: int, budget: int, status: str, caption: str) -> None:
        """Sets the bar; `status` picks the colour (`gpu`, `tight`, `hybrid`, `cpu`, `does-not-fit`)."""
        self.used, self.budget, self.status, self.caption = used, budget, status, caption
        self.refresh()

    def colour(self) -> str:
        """The theme colour of the current status."""
        name = GAUGE_STYLE.get(self.status, "secondary")
        variables = self.app.theme_variables if self.is_attached else {}
        return variables.get(name, "")

    def render(self) -> RenderableType:
        """`[#######----]  caption`."""
        caption = clean(self.caption)
        width = max(MIN_TRACK, self.size.width - len(caption) - 3)
        filled = 0 if self.budget <= 0 else min(width, round(width * self.used / self.budget))
        if self.status == "does-not-fit" and self.budget:
            filled = width
        full, empty = UNICODE_BAR if getattr(self.app, "unicode", False) else tokens.GAUGE_ASCII
        bar = Text("[")
        bar.append(full * filled, style=self.colour())
        bar.append(empty * (width - filled), style="dim")
        bar.append("] ")
        bar.append(caption, style=self.colour())
        return bar


# --- tool card and diff ----------------------------------------------------------------------


class DiffView(Vertical):
    """Diff or output lines folded to `limit`; a click on the last line (or Enter) shows the rest."""

    DEFAULT_CSS = """
    DiffView { height: auto; }
    DiffView > .more { color: $text-muted; }
    DiffView > .more:hover { color: $primary; text-style: underline; }
    """

    def __init__(self, folded: Sequence[RenderableType], full: Sequence[RenderableType], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.folded, self.full = list(folded), list(full)
        self.expanded = False

    def compose(self):  # noqa: ANN201 - Textual's ComposeResult
        """The folded lines."""
        for line in self.folded:
            yield SafeStatic(line)

    def toggle(self) -> None:
        """Swaps between the folded and the full lines."""
        self.expanded = not self.expanded
        self.remove_children()
        self.mount_all([SafeStatic(line) for line in (self.full if self.expanded else self.folded)])

    @property
    def foldable(self) -> bool:
        """True when the full view has more than the folded one."""
        return len(self.full) > len(self.folded)


class ToolCard(Vertical):
    """One tool call: a spinner and timer while it runs, then its result row; the body expands on click or Enter."""

    DEFAULT_CSS = """
    ToolCard { height: auto; margin: 0 0 0 2; }
    ToolCard > .header { height: 1; }
    ToolCard > .header:hover { background: $boost; }
    ToolCard:focus-within > .header { background: $boost; }
    ToolCard.running > .header { color: $primary; }
    """
    can_focus = True
    BINDINGS = [Binding("enter", "toggle", "expand", show=False)]

    def __init__(self, cid: str, title: Text, depth: int = 0, **kwargs: object) -> None:
        super().__init__(classes="running", **kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.cid, self.title, self.depth = cid, title, depth
        self.started = time.monotonic()
        self.finished = False
        self.body: DiffView | None = None
        self._tick = 0

    def compose(self):  # noqa: ANN201 - Textual's ComposeResult
        """The header row."""
        yield SafeStatic(self._running_header(), classes="header")

    def on_mount(self) -> None:
        """Starts the elapsed-time redraw."""
        self._timer = self.set_interval(TIMER_S, self._redraw)

    def _running_header(self) -> Text:
        spin = SPINNER[self._tick % len(SPINNER)]
        row = Text("  " * self.depth)
        row.append(f"{spin} ", style="bold")
        row.append_text(self.title)
        row.append(f"  running {time.monotonic() - self.started:.0f}s", style="dim")
        return row

    def _redraw(self) -> None:
        if not self.finished:
            self._tick += 1
            self.query_one(".header", SafeStatic).update(self._running_header())

    def finish(self, header: Text, folded: Sequence[RenderableType], full: Sequence[RenderableType]) -> None:
        """Shows the result row and the folded body."""
        self.finished = True
        self.remove_class("running")
        self._timer.stop()
        self.query_one(".header", SafeStatic).update(header)
        if folded or full:
            self.body = DiffView(folded, full)
            self.mount(self.body)

    def action_toggle(self) -> None:
        """Expands or folds the body."""
        if self.body is not None and self.body.foldable:
            self.body.toggle()

    def on_click(self, event: events.Click) -> None:
        """A click toggles the body."""
        self.action_toggle()


class ThinkingFold(Vertical):
    """`thought for ~N tokens`; a click or Enter shows the reasoning in place."""

    DEFAULT_CSS = """
    ThinkingFold { height: auto; color: $text-muted; }
    ThinkingFold > .summary:hover { text-style: underline; }
    """
    can_focus = True
    BINDINGS = [Binding("enter", "toggle", "expand", show=False)]

    def __init__(self, summary: Text, body: Text, shown: bool, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.summary, self.body, self.open = summary, body, shown

    def compose(self):  # noqa: ANN201 - Textual's ComposeResult
        """The summary, and the text when open."""
        yield SafeStatic(self.summary, classes="summary")
        if self.open:
            yield SafeStatic(self.body, classes="body")

    def action_toggle(self) -> None:
        """Shows or hides the reasoning."""
        self.open = not self.open
        if self.open:
            self.mount(SafeStatic(self.body, classes="body"))
        else:
            self.query(".body").remove()

    def on_click(self, event: events.Click) -> None:
        """A click toggles the text."""
        self.action_toggle()
