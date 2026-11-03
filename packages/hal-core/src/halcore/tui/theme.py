"""The look of the TUI: `[ui]` settings, glyph sets, colour tokens and the rich console factory.

One accent colour (HAL red) marks the prompt, the active spinner and focus; grey is for metadata;
green and red appear only on diff lines and pass/fail marks. Glyphs are Unicode when the terminal
encoding is UTF-8 and `[ui].unicode` is true, plain ASCII otherwise (this overrides the spec's
ASCII default, see docs/design/06-core-cli.md REQ-CLI-24). The console is built with markup,
emoji and highlighting off, so a string printed through it is never parsed as markup: model text
cannot restyle the screen (REQ-SEC-25).

One set of colour tokens (`tokens.py`) drives every surface: `PALETTES` (rich styles for the line UI),
`PT_COLORS` (prompt_toolkit) and `textual_themes()` (the full-screen UI), so a theme change looks
the same everywhere. `Look.theme` names the theme; nothing guesses it from a colour value.
`textual_themes` imports Textual only when called, so the headless paths never load it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TextIO

from typing import TYPE_CHECKING

from halkit import config as kitconfig
from rich import box as richbox
from rich.console import Console

from . import tokens

if TYPE_CHECKING:
    from textual.theme import Theme

ACCENT = tokens.ACCENT
THEMES = ("dark", "light", "mono")
NOTIFY_MODES = ("off", "bell", "osc9")
INTERFACES = ("auto", "full", "line")
DEFAULT_WIDTH = 100  # used when the output is not a terminal (tests, pipes)

UI_TABLE = kitconfig.register(
    kitconfig.Table(
        name="ui",
        owner="hal-core",
        keys=(
            kitconfig.Key("default_mode", str, "auto", choices=("auto", "code", "chat"), doc="What a bare `hal` runs"),
            kitconfig.Key("show_thinking", bool, False, doc="Show reasoning text, dimmed, while it streams"),
            kitconfig.Key("tool_output_lines", int, 8, doc="Lines of tool output shown before folding"),
            kitconfig.Key("diff_max_lines", int, 80, doc="Diff lines shown inline; /diff shows the rest"),
            kitconfig.Key("markdown_refresh_hz", int, 8, doc="Redraws per second of the streaming tail"),
            kitconfig.Key("mention_max_bytes", int, 32768, doc="Largest file an @path mention attaches"),
            kitconfig.Key("unicode", bool, True, doc="Use box drawing and glyphs when the terminal is UTF-8"),
            kitconfig.Key("theme", str, "dark", choices=THEMES, env="HAL_THEME", doc="Colour theme"),
            kitconfig.Key("notify", str, "bell", choices=NOTIFY_MODES, doc="Signal when a long turn ends unfocused"),
            kitconfig.Key(
                "interface", str, "auto", choices=INTERFACES, env="HAL_TUI",
                doc="auto: full screen on a capable terminal; full or line to force one",
            ),
            kitconfig.Key("animations", bool, True, doc="Short fades and slides in the full-screen UI"),
            kitconfig.Key("mouse", bool, True, doc="Mouse clicks and drags in the full-screen UI (Shift+drag selects text)"),
        ),
    )
)


@dataclass(frozen=True)
class Glyphs:
    """The drawing characters of one glyph set."""

    box: richbox.Box
    prompt: str
    tool: str
    ok: str
    fail: str
    running: str
    nest: str
    bullet: str
    think: str
    bar_full: str
    bar_empty: str
    sep: str
    spinner: str  # name of a rich spinner
    todo: Mapping[str, str]


UNICODE = Glyphs(box=richbox.ROUNDED, spinner=tokens.UNICODE_SPINNER, todo=tokens.UNICODE_TODO, **tokens.UNICODE_GLYPHS)
ASCII = Glyphs(box=richbox.ASCII, spinner=tokens.ASCII_SPINNER, todo=tokens.ASCII_TODO, **tokens.ASCII_GLYPHS)


@dataclass(frozen=True)
class Palette:
    """Rich style names for one theme; `""` means no colour."""

    accent: str
    muted: str
    ok: str
    bad: str
    code: str  # pygments theme for code blocks


PALETTES = {
    **{
        name: Palette(tokens.ROLES[name]["accent"], st["muted"], st["ok"], st["bad"], st["code"])
        for name, st in tokens.RICH_STYLES.items()
    },
    "mono": Palette("bold", "dim", "", "", "ansi_dark"),
}
PT_COLORS = {  # the same tokens as prompt_toolkit styles
    **{name: {k: r[k] for k in ("accent", "muted", "ok", "bad")} for name, r in tokens.ROLES.items()},
    "mono": {"accent": "bold", "muted": "", "ok": "", "bad": ""},
}
WARNING = {name: r["warn"] for name, r in tokens.ROLES.items()}  # amber for "tight" and "uses RAM"
SURFACES = {  # background, surface, panel and foreground of the full-screen UI
    name: (r["bg"], r["surface"], r["panel"], r["fg"]) for name, r in tokens.ROLES.items()
}
TEXTUAL_NAMES = {"dark": "hal-dark", "light": "hal-light", "mono": "hal-mono"}


@dataclass(frozen=True)
class Look:
    """Glyphs and palette resolved for this terminal, plus the display limits from `[ui]`."""

    glyphs: Glyphs
    palette: Palette
    color: bool
    theme: str = "dark"  # one of `THEMES`; `mono` also when `NO_COLOR` is set
    tool_output_lines: int = 8
    diff_max_lines: int = 80
    refresh_hz: int = 8
    show_thinking: bool = False
    notify: str = "bell"
    mention_max_bytes: int = 32768
    interface: str = "auto"
    animations: bool = True
    mouse: bool = True

    @property
    def unicode(self) -> bool:
        """True when this look draws Unicode glyphs."""
        return self.glyphs is UNICODE

    @property
    def textual_theme(self) -> str:
        """The name of the Textual theme that matches this look."""
        return TEXTUAL_NAMES[self.theme]


def encoding_is_utf8(stream: TextIO | None = None) -> bool:
    """True when `stream` (default stdout) writes UTF-8."""
    return (getattr(stream or sys.stdout, "encoding", "") or "").lower().replace("-", "") == "utf8"


def load_look(
    settings: Mapping[str, object] | None = None, *, stream: TextIO | None = None, environ: Mapping[str, str] | None = None
) -> Look:
    """Builds the `Look` from `[ui]` settings (defaults when None), the stream's encoding and `NO_COLOR`."""
    env = os.environ if environ is None else environ
    ui = dict(settings) if settings is not None else {key.name: key.default for key in UI_TABLE.keys}
    theme = str(ui.get("theme", "dark"))
    theme = theme if theme in THEMES else "dark"
    unicode_ok = bool(ui.get("unicode", True)) and encoding_is_utf8(stream)
    color = theme != "mono" and not env.get("NO_COLOR")
    return Look(
        glyphs=UNICODE if unicode_ok else ASCII,
        palette=PALETTES[theme],
        color=color,
        theme=theme if color else "mono",
        tool_output_lines=int(ui.get("tool_output_lines", 8)),  # type: ignore[call-overload]  # config validated the type
        diff_max_lines=int(ui.get("diff_max_lines", 80)),  # type: ignore[call-overload]  # config validated the type
        refresh_hz=max(1, int(ui.get("markdown_refresh_hz", 8))),  # type: ignore[call-overload]  # config validated the type
        show_thinking=bool(ui.get("show_thinking", False)),
        notify=str(ui.get("notify", "bell")),
        mention_max_bytes=int(ui.get("mention_max_bytes", 32768)),  # type: ignore[call-overload]  # config validated the type
        interface=str(ui.get("interface", "auto")),
        animations=bool(ui.get("animations", True)),
        mouse=bool(ui.get("mouse", True)),
    )


def make_console(look: Look, file: TextIO | None = None, *, width: int | None = None, record: bool = False) -> Console:
    """A rich console that never parses markup; colour only when `look.color` and the file is a terminal."""
    console = Console(
        file=file, markup=False, emoji=False, highlight=False, record=record,
        no_color=not look.color, width=width, soft_wrap=False,
    )  # fmt: skip
    if width is None and not console.is_terminal:
        console.width = DEFAULT_WIDTH
    return console


def textual_themes() -> list[Theme]:
    """`hal-dark`, `hal-light` and `hal-mono` as Textual themes, built from the tokens above."""
    from textual.theme import Theme  # noqa: PLC0415 - Textual loads only for the full-screen UI

    out = []
    for name in ("dark", "light"):
        r = tokens.ROLES[name]
        out.append(
            Theme(
                name=TEXTUAL_NAMES[name], primary=r["accent"], accent=r["accent"], secondary=r["muted"],
                success=r["ok"], error=r["bad"], warning=r["warn"], foreground=r["fg"], background=r["bg"],
                surface=r["surface"], panel=r["panel"], dark=name == "dark", variables=tokens.css_variables(name),
            )
        )  # fmt: skip
    mono = {f"hal-{role.replace('_', '-')}": "ansi_default" for role in tokens.ROLES["dark"]}
    out.append(Theme(name=TEXTUAL_NAMES["mono"], primary="ansi_default", ansi=True, dark=True, variables=mono))
    return out
