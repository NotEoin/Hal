"""The chrome around the transcript of `hal code` and `hal chat`: the HAL eye (and, later, the bars).

The HAL eye is one cell in HAL red that says what Hal is doing: dim when idle, a slow pulse while
a turn runs, an amber blink while an approval waits. The state is also said in words in the status
line, so colour is never the only signal; in the ASCII look the letter changes too (`o`, `O`,
`!`). With `[ui].animations = false` the eye is a solid colour and subscribes to nothing.

Widgets here read the app's `look` and `motion` attributes when present, so they also run in a
bare test app.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

from . import motion, tokens
from .theme import ASCII, Look

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
