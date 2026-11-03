"""The chrome around the transcript of `hal code` and `hal chat`: the HAL eye and the top bar.

The top bar is one row with no boxes: eye, surface, project, model, mode, effort, then the context
gauge at the right edge. It is one widget that renders one `Text` and maps clicks by column span,
so nothing is a filled button. `fit_top` (pure) picks the least compact layout that fits: the
project becomes its basename, `hal` and the word `effort` go, the model name is clipped with an
ellipsis, the gauge shrinks, then the project, the copy marker, the surface word and the
separators go. Model, mode, effort and the
gauge are never dropped, so every clickable item stays on screen; names are clipped, never wrapped.
Spans map clicks by index, never by text, so a model name cannot add an action.

The HAL eye is one cell in HAL red that says what Hal is doing: dim when idle, a slow pulse while
a turn runs, an amber blink while an approval waits. The state is also said in words in the status
line, so colour is never the only signal; in the ASCII look the letter changes too (`o`, `O`,
`!`). With `[ui].animations = false` the eye is a solid colour and subscribes to nothing.

Widgets here read the app's `look` and `motion` attributes when present, so they also run in a
bare test app.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import RenderableType
from rich.text import Text
from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from . import motion, tokens
from .render import clean
from .theme import ASCII, Glyphs, Look

EYE_STATES = ("idle", "thinking", "approval")


def role_colours(look: Look | None) -> dict[str, str]:
    """The colour roles of the look's theme; empty for `mono` (styles then fall back to bold and dim)."""
    if look is None or not look.color or look.theme not in tokens.ROLES:
        return {}
    return tokens.ROLES[look.theme]


def eye_style(state: str, now: float, look: Look | None) -> str:
    """The rich style of the eye in `state` at time `now`; static when motion is off."""
    roles = role_colours(look)
    animate = look is not None and look.animations
    if not roles:
        return {"idle": "dim", "thinking": "bold", "approval": "bold reverse"}.get(state, "dim")
    if state == "thinking":
        if not animate:
            return roles["accent"]
        return motion.blend(roles["accent_dim"], roles["accent"], motion.pulse(now, motion.EYE_PULSE_PERIOD_S))
    if state == "approval":
        if not animate or motion.blink(now, motion.EYE_BLINK_PERIOD_S):
            return f"bold {roles['warn']}"
        return roles["accent_dim"]
    return roles["accent_dim"]


class HalEye(Widget):
    """One cell: the HAL eye in its state colour (`idle`, `thinking`, `approval`)."""

    DEFAULT_CSS = "HalEye { width: 1; height: 1; }"

    state: reactive[str] = reactive("idle")

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.now = 0.0
        self._handle: motion.Handle | None = None

    @property
    def look(self) -> Look | None:
        """The app's look, when it has one."""
        return getattr(self.app, "look", None)

    def render(self) -> RenderableType:
        """The eye glyph of the state in its colour."""
        look = self.look
        glyphs = look.glyphs if look is not None else ASCII
        return Text(glyphs.eye_states.get(self.state, glyphs.eye), style=eye_style(self.state, self.now, look))

    def watch_state(self, old: str, new: str) -> None:
        """Starts or stops the eye's motion when the state changes."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        ticker: motion.Ticker | None = getattr(self.app, "motion", None)
        if new != "idle" and ticker is not None:
            self._handle = ticker.subscribe(self._frame, motion.EYE_HZ)
        self.refresh()

    def _frame(self, now: float) -> bool:
        self.now = now
        self.refresh()
        return self.state != "idle"

    def on_unmount(self) -> None:
        """Leaves the motion clock."""
        if self._handle is not None:
            self._handle.cancel()


# --- top bar -----------------------------------------------------------------------------------

MODEL_CHIP_NARROW = 16  # level 3 clips the model name to this many cells
MODEL_MIN = 6  # the shortest model clip at any width (still recognisable, still clickable)
GAUGE_CELLS = 10
GAUGE_NARROW = 6
GAUGE_TINY = 3
TOP_LEVELS = 10  # 0 full .. 9 tiny gauge (see `fit_top`); then the model name shrinks to `MODEL_MIN`
GROUP_GAP = "  "
WARN_PERCENT = 85  # `status.WARN_PERCENT`: the gauge turns amber here
CLICKABLE = ("model", "mode", "effort", "gauge", "copy")


@dataclass(frozen=True)
class TopInfo:
    """What the top bar shows; plain data so `fit_top` is a pure function."""

    surface: str
    project: str
    copy: bool
    model: str
    mode: str | None  # None in `hal chat` (no permission modes there)
    effort: str
    used: int
    size: int
    effort_pending: bool = False


@dataclass
class TopLayout:
    """The fitted top bar: `(key, text, role)` items left and right of the gap, and the level used."""

    level: int
    left: list[tuple[str, str, str]]
    right: list[tuple[str, str, str]]

    @property
    def width(self) -> int:
        """Cells used, without the gap."""
        return sum(cell_len(t) for _, t, _ in self.left + self.right)

    def keys(self) -> set[str]:
        """The item keys shown."""
        return {k for k, _, _ in self.left + self.right}


def _basename(project: str) -> str:
    return project.rstrip("/").rsplit("/", 1)[-1] or project


def _clip(text: str, cells: int, ellipsis: str) -> str:
    if cell_len(text) <= cells:
        return text
    return text[: max(1, cells - cell_len(ellipsis))] + ellipsis


def _layout(info: TopInfo, level: int, glyphs: Glyphs, model_cells: int) -> TopLayout:
    g = glyphs
    sep = f" {g.sep} " if level < 8 else " "
    left: list[tuple[str, str, str]] = []
    if level < 2:
        left.append(("hal", "hal ", "fg-bold"))
    if level < 7:
        left += [("surface", info.surface, "muted"), ("", GROUP_GAP, "")]
    if level < 5 and info.project:
        project = info.project if level < 1 else _basename(info.project)
        left += [("project", project, "muted"), ("", GROUP_GAP, "")]
    if info.copy and level < 6:
        left += [("copy", "copy", "faint"), ("", GROUP_GAP, "")]
    if level < 3:
        left.append(("", GROUP_GAP, ""))  # the read-only facts and the controls read as two groups
    model = info.model
    if level >= 3:
        model = _clip(model, min(model_cells, MODEL_CHIP_NARROW), g.ellipsis)
    left += [("model", f"{model} {g.caret}", "fg")]
    if info.mode is not None:
        left += [("", sep, "faint"), ("mode", f"{info.mode} {g.caret}", "warn" if info.mode == "auto-edit" else "fg")]
    effort = (info.effort or "default") + ("*" if info.effort_pending else "")
    left += [("", sep, "faint"), ("effort", (f"{effort} " if level >= 2 else f"effort {effort} ") + g.caret, "fg")]
    cells = GAUGE_TINY if level >= 9 else GAUGE_NARROW if level >= 4 else GAUGE_CELLS
    pct = 0 if info.size <= 0 else min(100, round(100 * info.used / info.size))
    bar = tokens.bar(info.used / info.size if info.size > 0 else 0.0, cells, g.bar_full, g.bar_empty)
    right = [("gauge", f"{bar} {pct}%", "warn" if pct >= WARN_PERCENT else "muted")]
    return TopLayout(level, left, right)


def fit_top(info: TopInfo, width: int, glyphs: Glyphs) -> TopLayout:
    """The least compact layout that fits `width` cells; model, mode, effort and gauge are never dropped."""
    layout = _layout(info, 0, glyphs, MODEL_CHIP_NARROW)
    for level in range(TOP_LEVELS):
        layout = _layout(info, level, glyphs, MODEL_CHIP_NARROW)
        if layout.width + 1 <= width:
            return layout
    for cells in range(MODEL_CHIP_NARROW - 1, MODEL_MIN - 1, -1):
        layout = _layout(info, TOP_LEVELS - 1, glyphs, cells)
        if layout.width + 1 <= width:
            break
    return layout


ROLE_STYLES = {"fg-bold": ("fg", "bold"), "fg": ("fg", ""), "muted": ("muted", ""), "faint": ("faint", ""),
               "warn": ("warn", ""), "": ("", "")}  # fmt: skip


def role_style(role: str, look: Look | None) -> str:
    """A rich style for a role name of the top bar or status line."""
    roles = role_colours(look)
    name, extra = ROLE_STYLES.get(role, (role, ""))
    if not roles:
        mono = {"muted": "dim", "faint": "dim", "warn": "bold"}.get(name, "")
        return f"{mono} {extra}".strip()
    return f"{roles.get(name, '')} {extra}".strip()


class TopBar(Widget):
    """The one-row top bar: surface, project, model, mode, effort and the context gauge; items are clickable."""

    DEFAULT_CSS = "TopBar { width: 1fr; height: 1; overflow: hidden; }"

    @dataclass
    class Clicked(Message):
        """An item of the top bar was clicked (`model`, `mode`, `effort`, `gauge` or `copy`)."""

        bar: TopBar
        key: str

    @dataclass
    class Wheeled(Message):
        """The wheel turned over an item; `step` is +1 or -1."""

        bar: TopBar
        key: str
        step: int

    def __init__(self, info: TopInfo, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # Widget's own keywords
        self.info = info
        self.hovered = ""
        self.spans: dict[str, tuple[int, int]] = {}

    def show(self, info: TopInfo) -> None:
        """New data; redraws when it changed."""
        if info != self.info:
            self.info = info
            self.refresh()

    def render(self) -> RenderableType:
        """The fitted items, the gap, then the gauge at the right edge."""
        look: Look | None = getattr(self.app, "look", None)
        glyphs = look.glyphs if look is not None else ASCII
        width = self.size.width or 80
        layout = fit_top(self.info, width, glyphs)
        roles = role_colours(look)
        hover = f"on {roles['surface']}" if roles else "reverse"
        text = Text(no_wrap=True, overflow="crop")
        self.spans = {}
        gap = max(1, width - layout.width)
        for part, items in (("left", layout.left), ("right", layout.right)):
            if part == "right":
                text.append(" " * gap)
            for key, shown, role in items:
                start = text.cell_len
                style = role_style(role, look)
                if key and key == self.hovered and key in CLICKABLE:
                    style = f"{style} {hover}"
                if key == "effort" and shown.startswith("effort "):
                    text.append("effort ", style=role_style("faint", look) + (f" {hover}" if key == self.hovered else ""))
                    shown = shown[len("effort ") :]
                text.append(clean(shown) if key in ("model", "project") else shown, style=style)
                if key in CLICKABLE:
                    self.spans[key] = (start, text.cell_len)
        return text

    def key_at(self, x: int) -> str:
        """The clickable item under column `x`, or ""."""
        return next((k for k, (a, b) in self.spans.items() if a <= x < b), "")

    def on_click(self, event: events.Click) -> None:
        """A click on an item posts `Clicked`."""
        key = self.key_at(event.x)
        if key:
            event.stop()
            self.post_message(self.Clicked(self, key))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """Highlights the item under the pointer."""
        key = self.key_at(event.x)
        if key != self.hovered:
            self.hovered = key
            self.refresh()

    def on_leave(self, event: events.Leave) -> None:
        """No highlight once the pointer leaves."""
        if self.hovered:
            self.hovered = ""
            self.refresh()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        """The wheel over an item posts `Wheeled` (the app decides which items react)."""
        key = self.key_at(event.x)
        if key:
            event.stop()
            self.post_message(self.Wheeled(self, key, 1))

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        """The wheel over an item posts `Wheeled`."""
        key = self.key_at(event.x)
        if key:
            event.stop()
            self.post_message(self.Wheeled(self, key, -1))
