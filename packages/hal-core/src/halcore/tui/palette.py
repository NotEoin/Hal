"""The rows of the Ctrl+P palette: commands, stored sessions, files, models and settings.

Pure and Textual-free, so it is tested without a screen. Each source is a function of the session
state that returns `PaletteItem`s; the screen merges them with `fuzzy.rank`. An item only names
what to do (`send` a command, `insert` a mention, `compose` a command that needs an argument, or a
fixed `ui` action); the app does it. Session titles and file names come from disk, so they are
`clean()`ed here and only ever drawn as `Text`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from halkit import paths

from .. import sessionscmd
from . import fuzzy
from .render import clean
from .slash import MODES, TuiState, commands_for, effort_support

PALETTE_ROWS = 12  # rows shown at once; more letters narrow the list
PALETTE_SESSIONS = 20  # stored sessions listed per palette open
THEMES = ("dark", "light", "mono")
NEEDS_ARGUMENT = frozenset({"/image"})  # opened in the composer instead of sent bare
UI_ANIMATIONS_ON, UI_ANIMATIONS_OFF = "animations-on", "animations-off"
UI_SIDE, UI_EXPAND = "side", "expand"
WHEN_CHARS = 16  # `YYYY-MM-DDTHH:MM` of an ISO stamp


@dataclass(frozen=True)
class PaletteItem:
    """One palette row: its kind column, label and note, and what choosing it does."""

    kind: str  # command, session, file, model or setting
    label: str
    note: str
    action: str  # send, insert, compose or ui
    value: str


def when(stamp: str) -> str:
    """An ISO stamp as `YYYY-MM-DD HH:MM`, or "" for a stamp that does not look like one."""
    short = stamp[:WHEN_CHARS]
    return short.replace("T", " ") if len(short) == WHEN_CHARS and short[10] == "T" else ""


def commands(state: TuiState) -> list[PaletteItem]:
    """The built-in commands of the surface, then the user's own."""
    out = []
    for name, (_, text) in commands_for(state.surface).items():
        line = f"/{name}"
        out.append(PaletteItem("command", line, text, "compose" if line in NEEDS_ARGUMENT else "send", line))
    for name in state.user_commands:
        out.append(PaletteItem("command", f"/{clean(name)}", "your command from .hal/commands", "send", f"/{name}"))
    return out


def sessions(state: TuiState, limit: int = PALETTE_SESSIONS) -> list[PaletteItem]:
    """Stored sessions of this project and surface, newest first; each resumes by id.

    A meta's `id` is file content, so one that is not a session id is skipped: it becomes part of
    a command line.
    """
    if state.records is None:
        return []
    rows = sessionscmd.list_sessions(state.records, state.project, limit, mode=state.surface)
    return [
        PaletteItem("session", clean(m.get("title", "")) or "(untitled)", when(m.get("updated", "")), "send", f"/resume {m['id']}")
        for m in rows
        if paths.SESSION_ID_RE.fullmatch(str(m.get("id", ""))) and m["id"] != state.sid
    ]


def files(names: Sequence[str]) -> list[PaletteItem]:
    """Workspace files; choosing one puts `@path ` into the message box."""
    return [PaletteItem("file", clean(n), "", "insert", f"@{n} ") for n in names]


def models(state: TuiState) -> list[PaletteItem]:
    """The models of your sources when `/model` has listed them, else one row that lists them."""
    if not state.choices:
        return [PaletteItem("model", "models", "list the models of your sources", "send", "/model")]
    return [
        PaletteItem("model", clean(c.name), f"({clean(c.source)})", "send", f"/model {c.name}")
        for c in state.choices
        if c.gateway
    ]


def settings(state: TuiState, *, animations: bool) -> list[PaletteItem]:
    """Theme, mode (code), effort levels, animations, the side panel and expand all."""
    out = [PaletteItem("setting", f"theme: {t}", "", "send", f"/theme {t}") for t in THEMES]
    if state.surface == "code":
        out += [PaletteItem("setting", f"mode: {m}", "current" if m == state.mode else "", "send", f"/mode {m}") for m in MODES]
    levels, _ = effort_support(state)
    out += [PaletteItem("setting", f"effort: {lvl}", "", "send", f"/effort {lvl}") for lvl in levels]
    out.append(
        PaletteItem("setting", "animations: off" if animations else "animations: on", "this session only", "ui",
                    UI_ANIMATIONS_OFF if animations else UI_ANIMATIONS_ON)
    )  # fmt: skip
    out.append(PaletteItem("setting", "side panel", "show or hide", "ui", UI_SIDE))
    out.append(PaletteItem("setting", "expand all", "open or fold every tool row", "ui", UI_EXPAND))
    return out


def gather(state: TuiState, *, animations: bool, workdir: Path | None, file_names: Sequence[str] = ()) -> list[PaletteItem]:
    """Every source in display order; `file_names` only counts in code with a working copy."""
    found = commands(state) + sessions(state)
    if state.surface == "code" and workdir is not None:
        found += files(file_names)
    return found + models(state) + settings(state, animations=animations)


def search(query: str, items: Sequence[PaletteItem], limit: int = PALETTE_ROWS) -> list[PaletteItem]:
    """The best `limit` matches of `query` over label and note; an empty query keeps the order."""
    return fuzzy.rank(query, items, key=lambda i: f"{i.label} {i.note}", limit=limit)
