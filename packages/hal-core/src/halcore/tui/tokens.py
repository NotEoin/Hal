"""Design tokens of every Hal terminal UI: colour roles, glyph sets and spacing, as plain tables.

One source for the line UI, the full-screen `hal code` / `hal chat` and `hal tune`: `theme.py`
builds its `Glyphs`, `Palette`, prompt_toolkit colours and Textual themes from these tables, so a
change here shows the same everywhere. The module imports nothing from Textual or rich, so the
headless and line UI paths can read it.

Colour roles: one accent (HAL red), three greys for text (`fg`, `muted`, `faint`), green and
amber only for status, and `bad` for failure. Glyphs are given as escapes (the repo is ASCII);
each Unicode glyph has an ASCII fallback with the same field name.
"""

from __future__ import annotations

ACCENT = "#e5484d"  # HAL red
ACCENT_LIGHT = "#c9373c"  # HAL red darkened: #e5484d on #fafafa is 3.75:1, under the 4.5:1 text minimum
WCAG_TEXT = 4.5  # WCAG 2.1 SC 1.4.3 minimum for body text
WCAG_HINT = 3.0  # WCAG 2.1 minimum for large text; used for the faint grey of timings and hints

# fmt: off
ROLES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#121212", "surface": "#1b1b1b", "panel": "#2a2a2a", "fg": "#e4e4e4", "muted": "#8a8a8a",
        "faint": "#666666", "accent": ACCENT, "accent_dim": "#6b2a2c", "ok": "#5faf5f", "warn": "#d4a72c",
        "bad": ACCENT,
    },
    "light": {
        "bg": "#fafafa", "surface": "#f0f0f0", "panel": "#dedede", "fg": "#1c1c1c", "muted": "#5f5f5f",
        "faint": "#8a8a8a", "accent": ACCENT_LIGHT, "accent_dim": "#e8b4b6", "ok": "#2e7d32", "warn": "#9a6700",
        "bad": ACCENT_LIGHT,
    },
}
# fmt: on
TEXT_ROLES = ("fg", "muted", "accent", "ok", "warn", "bad")  # held to the WCAG 4.5:1 text minimum
HINT_ROLES = ("faint",)  # timings and hints: held to 3:1



def luminance(hex_colour: str) -> float:
    """WCAG relative luminance of `#rrggbb`."""
    value = hex_colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]  # noqa: PLR2004 - WCAG constants
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two `#rrggbb` colours (1 to 21)."""
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def css_variables(theme: str) -> dict[str, str]:
    """The roles of `theme` as Textual CSS variables: `hal-fg`, `hal-muted`, `hal-faint`, ..."""
    return {f"hal-{role.replace('_', '-')}": value for role, value in ROLES[theme].items()}


# Rich style names of the line UI (a terminal's own palette reads better there than hex values).
RICH_STYLES: dict[str, dict[str, str]] = {
    "dark": {"muted": "grey58", "ok": "green", "bad": "red", "code": "github-dark"},
    "light": {"muted": "grey42", "ok": "dark_green", "bad": "red3", "code": "default"},
}

# fmt: off
UNICODE_GLYPHS: dict[str, str] = {
    "prompt": "\u203a", "tool": "\u25cf", "ok": "\u2713", "fail": "\u2717", "running": "\u25cb", "nest": "\u2514",
    "bullet": "\u2022", "think": "\u2234", "bar_full": "\u25b0", "bar_empty": "\u25b1", "sep": "\u00b7",
    "rail": "\u2502", "rail_end": "\u2514", "branch": "\u251c", "fold_closed": "\u25b8", "fold_open": "\u25be",
    "caret": "\u25be", "eye": "\u25cf", "ellipsis": "\u2026", "rule": "\u2500", "handle": "\u2502",
    "spark": "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588",
}
ASCII_GLYPHS: dict[str, str] = {
    "prompt": ">", "tool": "*", "ok": "ok", "fail": "FAIL", "running": "...", "nest": "`-", "bullet": "-",
    "think": "~", "bar_full": "#", "bar_empty": "-", "sep": "|", "rail": "|", "rail_end": "`", "branch": "|-",
    "fold_closed": ">", "fold_open": "v", "caret": "v", "eye": "o", "ellipsis": "...", "rule": "-", "handle": "|",
    "spark": " .:-=+*#",
}
# fmt: on
UNICODE_SPINNER_FRAMES = tuple("\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f")  # braille dots
ASCII_SPINNER_FRAMES = ("|", "/", "-", "\\")
UNICODE_EYE = {"idle": "\u25cf", "thinking": "\u25cf", "approval": "\u25cf"}
ASCII_EYE = {"idle": "o", "thinking": "O", "approval": "!"}  # the state in the letter, not only the colour
UNICODE_PLAN = {"completed": "\u2713", "in_progress": "\u203a", "pending": "\u25cb"}
ASCII_PLAN = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}
UNICODE_TODO = {"pending": "\u2610", "in_progress": "\u25d0", "completed": "\u2611"}
ASCII_TODO = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
UNICODE_SPINNER = "dots"  # names of rich spinners for the line UI's live region
ASCII_SPINNER = "line"

SPARK_LEVELS = 8  # one glyph per eighth of a cell


def bar(fraction: float, cells: int, full: str, empty: str) -> str:
    """`cells` cells, `round(cells * fraction)` of them full; `fraction` is clamped to 0..1."""
    filled = round(max(0, cells) * min(1.0, max(0.0, fraction)))
    return full * filled + empty * (max(0, cells) - filled)


def spark(values: list[float], cells: int, levels: str) -> str:
    """The last `cells` values as one glyph each, scaled to the window's maximum; flat when all are 0."""
    window = [max(0.0, v) for v in values[-cells:]] if cells > 0 else []
    top = max(window, default=0.0)
    if top <= 0:
        return levels[0] * len(window)
    last = len(levels) - 1
    return "".join(levels[min(last, round(v / top * last))] for v in window)


def spinner_frame(frames: tuple[str, ...], index: int) -> str:
    """Frame `index` of a spinner, wrapping around."""
    return frames[index % len(frames)]


# `hal tune` slider track and gauge: (line, fill, knob) and (full, empty)
TRACK_UNICODE = ("\u2500", "\u2501", "\u25cf")  # light line, heavy line, black circle
TRACK_ASCII = ("-", "=", "O")
GAUGE_UNICODE = ("\u2588", "\u2591")  # full block, light shade
GAUGE_ASCII = ("#", "-")
