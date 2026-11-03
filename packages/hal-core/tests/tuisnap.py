"""Snapshot helper for the full-screen UIs: a screen as an ASCII text grid plus a colour-role layer.

A golden is a structural fingerprint, not an SVG: Textual's SVG export is 40 to 100 KB per
screen, changes on any Textual upgrade and holds non-ASCII glyphs the repo's ASCII gate rejects.
`grid` renders the screen the way `App.export_screenshot` does (Textual 8.2.8) and walks the
segments into cells. Text keeps one row per screen row, with any non-ASCII character written as
`\\uXXXX`; roles keep one letter per cell for the foreground colour found by exact lookup in the
theme's colour table (uppercase when bold), and a background line only where the background is
not the screen's own. The SVG is written only when a golden does not match.

`HAL_SNAPSHOT_UPDATE=1` rewrites goldens; `HAL_SNAPSHOT_DIR` also receives the failure artifacts.
"""

from __future__ import annotations

import difflib
import io
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import textual
from rich.console import Console
from rich.segment import Segment

GOLDEN_DIR = Path(__file__).parent / "data" / "tui_snapshots"
UPDATE_ENV = "HAL_SNAPSHOT_UPDATE"
ARTIFACT_ENV = "HAL_SNAPSHOT_DIR"
OTHER = "x"  # a colour outside the theme's role table (Textual's own blends)
BLANK = "_"  # a space with no foreground of its own


@dataclass
class Cell:
    """One screen cell: its character, foreground and background hex (or ""), and boldness."""

    char: str
    fg: str
    bg: str
    bold: bool


@dataclass
class Snapshot:
    """A rendered screen as rows of cells."""

    width: int
    height: int
    rows: list[list[Cell]]

    @property
    def lines(self) -> list[str]:
        """The rows as plain strings (Unicode kept)."""
        return ["".join(c.char for c in row) for row in self.rows]

    def text(self, masks: Sequence[tuple[str, str]] = ()) -> list[str]:
        """The rows as ASCII, non-ASCII characters escaped as `\\uXXXX`, after the `masks` substitutions."""
        out = []
        for line in self.lines:
            for pattern, repl in masks:
                line = re.sub(pattern, repl, line)
            out.append("".join(ch if ord(ch) < 0x80 else f"\\u{ord(ch):04x}" for ch in line))
        return out

    def roles(self, table: dict[str, str], background: str) -> list[str]:
        """Run-length role rows: `a1 _2 F8` per row, plus a `bg:` row where the background differs."""
        bg_table = {hex_: letter.upper() for hex_, letter in table.items()}
        out = []
        for n, row in enumerate(self.rows):
            fg = [BLANK if c.char == " " else _bold(table.get(c.fg, OTHER), c.bold) for c in row]
            out.append(f"{n:02d} {_runs(fg)}")
            bg = ["." if (c.bg in ("", background)) else bg_table.get(c.bg, "X") for c in row]
            if any(b != "." for b in bg):
                out.append(f"{n:02d}bg {_runs(bg)}")
        return out


def _bold(letter: str, bold: bool) -> str:
    return letter.upper() if bold else letter


def _runs(cells: list[str]) -> str:
    runs: list[str] = []
    for letter in cells:
        if runs and runs[-1][0] == letter:
            runs[-1] = (letter, runs[-1][1] + 1)  # type: ignore[assignment]
        else:
            runs.append((letter, 1))  # type: ignore[arg-type]
    return " ".join(f"{letter}{count}" for letter, count in runs)  # type: ignore[misc]


def _hex(color) -> str:  # noqa: ANN001 - a rich Color or None
    if color is None or color.is_default:
        return ""
    return color.get_truecolor().hex.lower()


def grid(app) -> Snapshot:  # noqa: ANN001 - a running Textual App
    """The current screen of `app` as cells, rendered like `App.export_screenshot` (Textual 8.2.8)."""
    width, height = app.size
    console = Console(width=width, height=height, file=io.StringIO(), force_terminal=True, color_system="truecolor",
                      legacy_windows=False, safe_box=False)  # fmt: skip
    update = app.screen._compositor.render_update(full=True, screen_stack=app._background_screens)  # noqa: SLF001 - Textual has no public cell grid (8.2.8)
    rows: list[list[Cell]] = []
    for line in Segment.split_lines(console.render(update, console.options)):
        row: list[Cell] = []
        for seg in line:
            if seg.control:
                continue
            style = seg.style
            fg = _hex(style.color) if style else ""
            bg = _hex(style.bgcolor) if style else ""
            bold = bool(style and style.bold)
            if style and style.reverse:
                fg, bg = bg, fg
            for ch in seg.text:
                row.append(Cell(ch, fg, bg, bold))
        rows.append(row[:width] + [Cell(" ", "", "", False)] * max(0, width - len(row)))
    rows = rows[:height]
    return Snapshot(width, height, rows)


def role_table(theme: str) -> tuple[dict[str, str], str]:
    """`({hex: letter}, background hex)` for a theme, from the TUI's colour tokens."""
    from halcore.tui import theme as look  # noqa: PLC0415 - the helper loads after the test's own imports

    background, surface, panel, foreground = look.SURFACES[theme]
    colors = look.PT_COLORS[theme]
    roles = {"f": foreground, "m": colors["muted"], "a": look.ACCENT, "o": colors["ok"], "b": colors["bad"],
             "w": look.WARNING[theme], "p": panel, "s": surface}  # fmt: skip
    table: dict[str, str] = {}
    for letter, hex_ in roles.items():
        table.setdefault(hex_.lower(), letter)
    return table, background.lower()


def render_golden(snap: Snapshot, *, name: str, theme: str, look: str, masks: Sequence[tuple[str, str]] = ()) -> str:
    """The golden file text for `snap`."""
    table, background = role_table(theme)
    head = f"# hal tui snapshot: screen={name} size={snap.width}x{snap.height} theme={theme} look={look} textual={textual.__version__}"
    return "\n".join([head, "## text", *snap.text(masks), "## roles", *snap.roles(table, background)]) + "\n"


def compare(app, snap: Snapshot, *, name: str, theme: str, look: str, tmp_path: Path,
            masks: Sequence[tuple[str, str]] = ()) -> None:  # fmt: skip  # noqa: ANN001 - a running App
    """Fails with a text diff when `snap` differs from its golden; rewrites it with `HAL_SNAPSHOT_UPDATE=1`."""
    stem = f"{name}-{snap.width}x{snap.height}-{theme}" + ("-ascii" if look == "ascii" else "")
    golden = GOLDEN_DIR / f"{stem}.snap"
    actual = render_golden(snap, name=name, theme=theme, look=look, masks=masks)
    if os.environ.get(UPDATE_ENV) == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual, encoding="ascii")
        return
    assert golden.exists(), f"no golden {golden.name}; run with {UPDATE_ENV}=1 to record it"
    expected = golden.read_text(encoding="ascii")
    head = expected.splitlines()[0]
    version = re.search(r"textual=(\S+)", head)
    assert version and version.group(1) == textual.__version__, (
        f"goldens were made with textual {version.group(1) if version else '?'}; regenerate them"
    )
    if expected == actual:
        return
    diff = "".join(difflib.unified_diff(expected.splitlines(True), actual.splitlines(True), "golden", "actual"))
    for folder in [tmp_path, *([Path(os.environ[ARTIFACT_ENV])] if os.environ.get(ARTIFACT_ENV) else [])]:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{stem}.actual.snap").write_text(actual, encoding="ascii")
        (folder / f"{stem}.diff").write_text(diff, encoding="ascii", errors="backslashreplace")
        app.save_screenshot(str(folder / f"{stem}.actual.svg"))
    raise AssertionError(f"snapshot {stem} changed:\n{diff[:4000]}")


def assert_unbroken(snap: Snapshot, words: Iterable[str], ellipses: Sequence[str] = ("...", "\u2026")) -> None:
    """Every label in `words` that shows up appears whole on one row, or cut with an ellipsis; never split across rows."""
    lines = snap.lines
    for word in words:
        if len(word) < 4 or any(word in line for line in lines):
            continue
        head = word[: max(3, len(word) // 2)]
        for n, line in enumerate(lines[:-1]):
            at = line.rfind(head)
            if at < 0:
                continue
            shown = line[at:].rstrip()
            if any(shown.endswith(e) for e in ellipses) or not word.startswith(shown):
                continue
            rest = word[len(shown):].lstrip()
            assert not (rest and lines[n + 1].lstrip().startswith(rest[: max(2, len(rest) // 2)])), (
                f"{word!r} is broken across rows {n} and {n + 1}:\n{line}\n{lines[n + 1]}"
            )


def assert_inside(app, selectors: Iterable[str]) -> None:  # noqa: ANN001 - a running App
    """Each widget matched by `selectors` lies fully inside the screen."""
    width, height = app.size
    for selector in selectors:
        for widget in app.screen.query(selector):
            if not widget.display:
                continue
            region = widget.region
            assert region.right <= width and region.bottom <= height, f"{selector} leaves the screen: {region}"


def assert_one_row(app, selectors: Iterable[str]) -> None:  # noqa: ANN001 - a running App
    """Each displayed widget matched by `selectors` is exactly one row high."""
    for selector in selectors:
        for widget in app.screen.query(selector):
            if widget.display and widget.region.height:
                assert widget.region.height == 1, f"{selector} takes {widget.region.height} rows"
