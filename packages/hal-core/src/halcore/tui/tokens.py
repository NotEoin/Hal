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
    "dark": {"muted": "grey58", "ok": "green", "bad": "red", "code": "monokai"},
    "light": {"muted": "grey42", "ok": "dark_green", "bad": "red3", "code": "default"},
}

# fmt: off
UNICODE_GLYPHS: dict[str, str] = {
    "prompt": "\u276f", "tool": "\u25cf", "ok": "\u2713", "fail": "\u2717", "running": "\u25cb", "nest": "\u2514",
    "bullet": "\u2022", "think": "\u2237", "bar_full": "\u2588", "bar_empty": "\u2591", "sep": "\u00b7",
}
ASCII_GLYPHS: dict[str, str] = {
    "prompt": ">", "tool": "*", "ok": "ok", "fail": "FAIL", "running": "...", "nest": "`-", "bullet": "-",
    "think": "~", "bar_full": "#", "bar_empty": "-", "sep": "|",
}
# fmt: on
UNICODE_TODO = {"pending": "\u2610", "in_progress": "\u25d0", "completed": "\u2611"}
ASCII_TODO = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
UNICODE_SPINNER = "dots"  # names of rich spinners for the line UI's live region
ASCII_SPINNER = "line"

# `hal tune` slider track and gauge: (line, fill, knob) and (full, empty)
TRACK_UNICODE = ("\u2500", "\u2501", "\u25cf")  # light line, heavy line, black circle
TRACK_ASCII = ("-", "=", "O")
GAUGE_UNICODE = ("\u2588", "\u2591")  # full block, light shade
GAUGE_ASCII = ("#", "-")
