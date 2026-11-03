"""The full-screen welcome: the HAL eye, the session facts, recent sessions and a tip of the day.

`WelcomeView` is the first item of the transcript in `hal code` and `hal chat` and scrolls away
like any other; the line UI keeps `controller.welcome()`. Design decisions:

- The eye art is fixed ASCII strings in both looks (the Unicode look only swaps the pupil for the
  eye glyph) and never animates. Below `WELCOME_ART_MIN_ROWS` screen rows a one-row eye replaces it.
- Recent sessions come from `palette.sessions` (stored ids checked, titles `clean()`ed) in a
  thread at mount; a row only posts the fixed command `/resume ID`. The stored title is the
  preview, because the meta holds no message text (spec section 6).
- The tip is `TIPS[day_of_year % len(TIPS)]`, read through the module-level `clock`, so it is
  stable within a day and tests pin it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import datetime

from halkit import timeutil
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget

from .. import __version__
from . import palette
from .chrome import role_colours
from .render import clean
from .slash import TuiState
from .theme import Look
from .widgets import SafeStatic

clock: Callable[[], datetime] = timeutil.utc_now
WELCOME_ART_MIN_ROWS = 30  # below this the 7-row eye leaves too little room for the transcript
RECENT_SHORT, RECENT_LONG = 3, 5  # recent sessions shown below and from `WELCOME_ART_MIN_ROWS`
RECENT_TIMEOUT_S = 5.0  # listing stored sessions on a slow disk; the list is then left out
INDENT = 4  # columns before the art, the recent rows and the tip (the transcript adds one more)
FACTS_GAP = 9  # columns between the art and the facts beside it
DATE_W = 16  # `YYYY-MM-DD HH:MM`
RECENT_MAX_W = 72  # on a wide screen the date stays near its title instead of at the far edge
ART = (
    "   .-----.",
    " .'  ___  '.",
    r"/   /   \   \ ",
    "|  |  o  |  |",
    r"\   \___/   /",
    " '.       .'",
    "   '-----'",
)
PUPIL_ROW, PUPIL_COL = 3, 6
FACT_ROWS = (1, 2, 3)  # art rows the three fact lines sit beside
TIPS = (
    "Ctrl+P finds commands, files, sessions and settings in one search",
    "Ctrl+R searches the messages you sent before",
    "a click on the context gauge shows what fills the window",
    "Ctrl+B shows the plan, the changes and the context",
    "@ before a file name attaches that file to the message",
)


def tip(now: datetime | None = None) -> str:
    """The tip of the day."""
    day = (now or clock()).timetuple().tm_yday
    return TIPS[day % len(TIPS)]


def short_path(path: str, home: str | None = None) -> str:
    """`path` with the home folder written as `~`."""
    home = home if home is not None else os.path.expanduser("~")
    if home and home != "/" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home) :]
    return path


def facts(state: TuiState, session: object, look: Look, version: str = __version__) -> list[Text]:
    """The three fact lines: surface and version, model and box, workspace and copy."""
    roles = role_colours(look)
    muted = roles.get("muted", "dim")
    sep = f"  {look.glyphs.sep}  "
    info = state.status
    head = Text.assemble((f"hal {state.surface}", f"bold {roles.get('fg', '')}".strip()), (f" {version}", muted))
    parts = [clean(info.model) or "model: default"]
    if info.size:
        parts.append(f"{info.size // 1024}k context")
    if info.lockdown:
        parts.append(f"lockdown {clean(info.lockdown)}")
    if info.paranoia:
        parts.append(f"paranoia {clean(info.paranoia)}")
    runtime = getattr(session, "runtime", None) or getattr(session, "backend", "")
    if runtime:
        parts.append(f"box {clean(runtime)}")
    where = Text(short_path(clean(str(getattr(session, "project", None) or state.cwd))), style=muted)
    if getattr(session, "iso", None) is not None:
        where.append("   isolated copy; /accept applies changes", style=muted)
    return [head, Text(sep.join(parts), style=muted), where]


def art(look: Look) -> list[Text]:
    """The seven eye rows: ring `faint`, pupil `accent` (the eye glyph in the Unicode look)."""
    roles = role_colours(look)
    ring, pupil = roles.get("faint", "dim"), f"bold {roles.get('accent', '')}".strip()
    rows = []
    for n, line in enumerate(ART):
        text = Text(line, style=ring)
        if n == PUPIL_ROW:
            text = Text.assemble((line[:PUPIL_COL], ring), (look.glyphs.eye, pupil), (line[PUPIL_COL + 1 :], ring))
        rows.append(text)
    return rows


def header(state: TuiState, session: object, look: Look, rows: int) -> Text:
    """The eye with the facts beside it, or the one-row eye above them on a short screen."""
    lines = facts(state, session, look)
    if rows < WELCOME_ART_MIN_ROWS:
        accent = f"bold {role_colours(look).get('accent', '')}".strip()
        out = Text(" " * 2).append(look.glyphs.eye, style=accent).append("  ").append_text(lines[0])
        for line in lines[1:]:
            out.append("\n" + " " * 5).append_text(line)
        return out
    width = max(len(line) for line in ART)
    out = Text()
    for n, row in enumerate(art(look)):
        out.append("\n" if n else "").append(" " * INDENT).append_text(row)
        if n in FACT_ROWS:
            out.append(" " * (width - len(ART[n]) + FACTS_GAP)).append_text(lines[FACT_ROWS.index(n)])
    return out


class EyeHeader(Widget):
    """The eye and facts, redrawn for the screen height so a resize swaps the art in or out."""

    DEFAULT_CSS = "EyeHeader { height: auto; margin: 1 0; }"

    def __init__(self, state: TuiState, session: object, look: Look) -> None:
        super().__init__()
        self.state, self.session, self.look = state, session, look

    def render(self) -> Text:
        """The header for the current screen height."""
        return header(self.state, self.session, self.look, self.app.size.height)

    def on_resize(self, event: events.Resize) -> None:
        """A new height may cross `WELCOME_ART_MIN_ROWS`."""
        self.refresh(layout=True)


class RecentRow(Widget, can_focus=True):
    """One stored session: its title clipped to fit and the date; a click or Enter resumes it."""

    DEFAULT_CSS = """
    RecentRow { height: 1; width: 1fr; padding: 0 0 0 4; }
    RecentRow:hover, RecentRow:focus { background: $surface; }
    """
    BINDINGS = [Binding("enter", "resume", "resume", show=False)]

    def __init__(self, item: palette.PaletteItem, look: Look) -> None:
        super().__init__()
        self.item, self.look = item, look

    def render(self) -> Text:
        """`> title` then the date at the right, faint."""
        roles = role_colours(self.look)
        width = max(min(self.size.width - 1, RECENT_MAX_W), DATE_W + 4)  # one column free before the edge
        room = width - DATE_W - 4
        title = self.item.label
        if len(title) > room:
            cut = self.look.glyphs.ellipsis
            title = title[: max(room - len(cut), 0)] + cut
        text = Text("> ", style=roles.get("faint", "dim")).append(title, style=roles.get("fg", ""))
        text.append(" " * (width - len(text) - DATE_W)).append(self.item.note.rjust(DATE_W), style=roles.get("faint", "dim"))
        return text

    def on_click(self, event: events.Click) -> None:
        """A click resumes this session."""
        event.stop()
        self.action_resume()

    def action_resume(self) -> None:
        """Posts the fixed `/resume ID` built from a checked id."""
        self.post_message(WelcomeView.Resume(self.item.value))


class WelcomeView(Vertical):
    """The first transcript item of the full screen: eye, facts, recent sessions and a tip."""

    DEFAULT_CSS = """
    WelcomeView { height: auto; }
    WelcomeView .welcome-heading { height: 1; margin-top: 1; padding: 0 0 0 4; color: $text-muted; }
    WelcomeView .welcome-tip { height: auto; margin: 1 0; padding: 0 0 0 4; }
    """

    class Resume(Message):
        """A recent session was chosen; `command` is `/resume ID`."""

        def __init__(self, command: str) -> None:
            super().__init__()
            self.command = command

    def __init__(self, state: TuiState, session: object, look: Look) -> None:
        super().__init__()
        self.state, self.session, self.look = state, session, look

    def compose(self) -> ComposeResult:
        """The header and the tip; recent rows are mounted between them once listed."""
        yield EyeHeader(self.state, self.session, self.look)
        roles = role_colours(self.look)
        line = Text("tip", style=roles.get("faint", "dim")).append(f"  {tip()}", style=roles.get("muted", "dim"))
        yield SafeStatic(line, classes="welcome-tip")

    def on_mount(self) -> None:
        """Lists the recent sessions in the background."""
        self.run_worker(self.load_recent(), group="welcome")

    async def load_recent(self) -> None:
        """Mounts the `recent` heading and rows; with no stored sessions both are left out."""
        count = RECENT_LONG if self.app.size.height >= WELCOME_ART_MIN_ROWS else RECENT_SHORT
        try:
            rows = await asyncio.wait_for(asyncio.to_thread(palette.sessions, self.state, count + 1), RECENT_TIMEOUT_S)
        except (OSError, TimeoutError):
            return  # the welcome works without the list; /resume still lists them
        rows = rows[:count]
        if not rows:
            return
        tip_line = self.query_one(".welcome-tip")
        await self.mount(SafeStatic(Text("recent"), classes="welcome-heading"), before=tip_line)
        await self.mount_all([RecentRow(r, self.look) for r in rows], before=tip_line)
