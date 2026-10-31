"""The persistent status line: model, context bar, speed, mode, lockdown, jobs, git branch, changes.

`StatusInfo` is plain data the app refreshes between turns (never inside a keystroke handler, so
the line costs nothing per redraw). `segments` turns it into `(role, text)` pairs that both the
prompt_toolkit toolbar and the rich live region draw, so the two always agree. Roles map to the
palette: accent for focus, muted for metadata, ok and bad only for pass and fail marks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from halkit import proc
from rich.text import Text

from .render import clean, clip
from .theme import Glyphs, Look

log = logging.getLogger(__name__)

BAR_WIDTH = 10
GIT_TIMEOUT_S = 5.0
MAX_FIELD_CHARS = 40
WARN_PERCENT = 85  # the point where a 16k to 64k window starts truncating useful history
PT_COLORS = {
    "dark": {"accent": "#e5484d", "muted": "#8a8a8a", "ok": "#5faf5f", "bad": "#e5484d"},
    "light": {"accent": "#e5484d", "muted": "#6c6c6c", "ok": "#2e7d32", "bad": "#c62828"},
    "mono": {"accent": "bold", "muted": "", "ok": "", "bad": ""},
}


@dataclass
class StatusInfo:
    """What the status line shows; every field has a safe empty value."""

    model: str = ""
    used: int = 0
    size: int = 0
    tok_s: float = 0.0
    mode: str = "default"
    lockdown: str = ""
    paranoia: str = ""
    jobs: int = 0
    branch: str = ""
    changes: int = 0
    unsandboxed: bool = False
    notice: str = ""  # a short transient message, for example "verbose on"


def context_bar(used: int, size: int, glyphs: Glyphs, width: int = BAR_WIDTH) -> str:
    """A bar of `width` cells filled in proportion to `used` of `size`, empty when `size` is unknown."""
    filled = 0 if size <= 0 else min(width, round(width * used / size))
    return glyphs.bar_full * filled + glyphs.bar_empty * (width - filled)


def percent(used: int, size: int) -> int:
    """Whole percent of the window in use; 0 when the size is unknown."""
    return 0 if size <= 0 else min(100, round(100 * used / size))


def segments(info: StatusInfo, look: Look) -> list[tuple[str, str]]:
    """The status line as `(role, text)` pairs."""
    g = look.glyphs
    sep = (("muted", f" {g.sep} "),)
    out: list[tuple[str, str]] = [("accent", f" {clip(clean(info.model) or 'no model', MAX_FIELD_CHARS)}")]
    if info.size:
        pct = percent(info.used, info.size)
        out += [*sep, ("bad" if pct >= WARN_PERCENT else "muted", f"{context_bar(info.used, info.size, g)} {pct}%")]
    if info.tok_s:
        out += [*sep, ("muted", f"{info.tok_s:.0f} tok/s")]
    out += [*sep, ("muted", f"mode {info.mode}")]
    if info.lockdown:
        out += [*sep, ("muted", f"lockdown {info.lockdown}" + (f"/{info.paranoia}" if info.paranoia else ""))]
    if info.jobs:
        out += [*sep, ("muted", f"{info.jobs} job{'s' if info.jobs != 1 else ''}")]
    if info.branch:
        out += [*sep, ("muted", clip(clean(info.branch), MAX_FIELD_CHARS))]
    if info.changes:
        out += [*sep, ("muted", f"{info.changes} unaccepted")]
    if info.unsandboxed:
        out += [*sep, ("bad", "UNSANDBOXED")]
    if info.notice:
        out += [*sep, ("accent", clean(info.notice))]
    return out


def to_rich(parts: list[tuple[str, str]], look: Look) -> Text:
    """The segments as rich text in the look's palette."""
    pal = look.palette
    styles = {"accent": pal.accent, "muted": pal.muted, "ok": pal.ok, "bad": pal.bad}
    return Text.assemble(*[(text, styles.get(role, "")) for role, text in parts])


def to_prompt_toolkit(parts: list[tuple[str, str]], look: Look) -> list[tuple[str, str]]:
    """The segments as prompt_toolkit formatted text."""
    theme = "mono" if not look.color else ("light" if look.palette.muted == "grey42" else "dark")
    colors = PT_COLORS[theme]
    return [(colors.get(role, ""), text) for role, text in parts]


def git_branch(work: Path | None) -> str:
    """The current branch of the working copy at `work`, or "" when it is not a git tree."""
    if work is None or not (work / ".git").exists():
        return ""
    try:
        done = proc.run(["git", "-C", str(work), "rev-parse", "--abbrev-ref", "HEAD"], timeout=GIT_TIMEOUT_S, check=False)
    except (proc.ProcTimeout, OSError) as exc:
        log.warning("git branch lookup failed: %s", exc)
        return ""
    return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""
