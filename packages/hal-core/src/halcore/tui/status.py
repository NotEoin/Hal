"""The persistent status line: model, context bar, speed, mode, lockdown, jobs, git branch, changes.

`StatusInfo` is plain data the app refreshes between turns (never inside a keystroke handler, so
the line costs nothing per redraw). `segments` turns it into `(role, text)` pairs that both the
prompt_toolkit toolbar and the rich live region draw, so the two always agree. Roles map to the
palette: accent for focus, muted for metadata, ok and bad only for pass and fail marks.

The branch is read from the copy's `.git/HEAD` file, never by running `git`: the box writes that
repository, and host `git` would honour its config (`core.fsmonitor` and friends).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from halkit import fsafe
from halkit.paths import UnsafePathError
from rich.text import Text

from .render import clean, clip
from . import tokens
from .theme import PT_COLORS, Glyphs, Look

log = logging.getLogger(__name__)

BAR_WIDTH = 10
MAX_HEAD_BYTES = 512  # a symbolic ref line; anything longer is not a HEAD file
HEAD_REF_PREFIX = "ref: refs/heads/"
MAX_FIELD_CHARS = 40
WARN_PERCENT = 85  # the point where a 16k to 64k window starts truncating useful history

TOKENS_PER_K = 1000

@dataclass
class StatusInfo:
    """What the status line shows; every field has a safe empty value."""

    model: str = ""
    effort: str = ""  # the level `/effort` set; shown next to the model
    used: int = 0
    size: int = 0
    tok_s: float = 0.0
    mode: str = "default"
    lockdown: str = ""
    paranoia: str = ""
    jobs: int = 0
    branch: str = ""
    changes: int = 0
    changed: list[tuple[str, str]] = field(default_factory=list)  # (A/M/D/T, path) per changed path, computed on the host
    unsandboxed: bool = False
    backend: str = ""  # the box runtime (`runsc`, `runc`, `docker`) or `none`
    notice: str = ""  # a short transient message, for example "verbose on"


def model_label(info: StatusInfo) -> str:
    """The model name as every bar shows it: cleaned, `default` when the session uses the default."""
    return clean(info.model) or "default"


def context_bar(used: int, size: int, glyphs: Glyphs, width: int = BAR_WIDTH) -> str:
    """A bar of `width` cells filled in proportion to `used` of `size`, empty when `size` is unknown."""
    return tokens.bar(used / size if size > 0 else 0.0, width, glyphs.bar_full, glyphs.bar_empty)


def short_tokens(n: int) -> str:
    """A token count in a few characters: `950`, `9.1k`."""
    return str(n) if n < TOKENS_PER_K else f"{n / TOKENS_PER_K:.1f}k"


def percent(used: int, size: int) -> int:
    """Whole percent of the window in use; 0 when the size is unknown."""
    return 0 if size <= 0 else min(100, round(100 * used / size))


def segments(info: StatusInfo, look: Look, where: str = "line") -> list[tuple[str, str]]:
    """The status line as `(role, text)` pairs; `where="status"` leaves out what the full screen's top bar shows.

    The line UI (`where="line"`) shows every field. The full-screen status line starts with the
    box and leaves model, effort, context, speed and mode to the top bar and the sparkline.
    """
    g = look.glyphs
    sep = (("muted", f" {g.sep} "),)
    out: list[tuple[str, str]] = []
    if where == "line":
        out.append(("accent", f" {clip(model_label(info), MAX_FIELD_CHARS)}"))
        if info.effort:
            out += [*sep, ("muted", f"effort {clip(clean(info.effort), MAX_FIELD_CHARS)}")]
        if info.size:
            pct = percent(info.used, info.size)
            out += [*sep, ("bad" if pct >= WARN_PERCENT else "muted", f"{context_bar(info.used, info.size, g)} {pct}%")]
        if info.tok_s:
            out += [*sep, ("muted", f"{info.tok_s:.0f} tok/s")]
        out += [*sep, ("muted", f"mode {info.mode}")]
    if info.backend and not info.unsandboxed:
        out += [*(sep if out else ()), ("muted", f"box {clip(clean(info.backend), MAX_FIELD_CHARS)}")]
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
    colors = PT_COLORS[look.theme]
    return [(colors.get(role, ""), text) for role, text in parts]


def git_branch(work: Path | None) -> str:
    """The current branch of the working copy at `work`, `HEAD` when detached, or "" when not a git tree."""
    if work is None:
        return ""
    try:
        fd = fsafe.open_regular_beneath(work, ".git/HEAD")
        with os.fdopen(fd, "rb") as fh:
            head = fh.read(MAX_HEAD_BYTES).decode("utf-8", "replace").strip()
    except (OSError, UnsafePathError):
        return ""
    if head.startswith(HEAD_REF_PREFIX):
        return head[len(HEAD_REF_PREFIX) :]
    return "HEAD" if head else ""
