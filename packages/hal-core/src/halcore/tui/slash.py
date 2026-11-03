"""Slash commands of the TUI: built-ins plus `.hal/commands/NAME.md` files (RESOLUTIONS I8).

A command returns a `Result`: text to print, a prompt to send to the agent, or a quit. Commands
that touch files act on the host side of the box (checkpoints, diff, accept), because nothing
reaches the real project except through `accept` (RESOLUTIONS I3). A user command file is the
person's own text from the real project, not from the box copy, so it is a human message.

A bare listing command (`/model`, `/resume`, `/rewind`, `/mode`, `/effort`, `/theme`) also
returns a `PickRequest`: the line UI prints the numbered list as before, the full-screen UI opens
a picker whose choice runs the same command with the chosen value. Commands that need a project
copy (`CODE_ONLY`) are not offered in `hal chat`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from halkit import effort, timeutil
from halsandbox import isolated
from halsandbox.errors import SandboxError

from .. import sessionscmd
from ..acp.client import AcpError
from ..acp.records import RecordStore
from rich.console import RenderableType
from rich.table import Table

from .. import deepwork
from . import choices
from .render import Renderer, TurnStats, clip, safe
from .status import StatusInfo, context_bar, percent
from .theme import THEMES, load_look

if TYPE_CHECKING:
    from halsandbox.session import Session

    from ..acp.client import AcpClient

log = logging.getLogger(__name__)

COMMANDS_DIR = Path(".hal") / "commands"
RESUME_LIST_LIMIT = 10
MAX_COMMAND_BYTES = 64 * 1024  # a prompt template, not a document
MAX_IMAGE_BYTES = 5 * 2**20  # the ACP line cap is 8 MiB and base64 adds a third
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_DIFF_CHARS = 20_000
ALLOW_FLAGS = "--allow-flags"
CATALOG_TIMEOUT_S = 20.0  # discovery asks each source in turn, each with its own short timeout
MODEL_LIST_TIMEOUT_S = 3.0  # a picker must not hang on a stopped gateway
CONTEXT_KEYS = ("context_length", "context_window", "n_ctx", "max_model_len")
INIT_PROMPT = (
    "Read the repository (layout, build and test commands, conventions) and write AGENTS.md at the "
    "project root for future coding agents: what the project is, how to build, test and lint it, "
    "code conventions and pitfalls. Keep it under 60 lines and verify every command you list."
)
MODES = {"default": "review", "auto-edit": "auto", "plan": "plan"}  # TUI mode name -> hal-agent mode id
MODE_NOTES = {
    "default": "asks per policy",
    "auto-edit": "edits in the box copy without asking",
    "plan": "read-only; ends with a plan to approve",
}
ARGUMENTS = "$ARGUMENTS"
Block = dict[str, object]


@dataclass(frozen=True)
class PickItem:
    """One row of a picker; `label` and `note` are already escaped for display."""

    label: str
    value: str
    note: str = ""
    disabled: bool = False


@dataclass(frozen=True)
class PickRequest:
    """A list the full-screen UI shows as a picker; choosing a row runs `command` with its value."""

    title: str
    items: tuple[PickItem, ...]
    command: str  # a slash command with `{value}`, for example `/model {value}`
    current: str = ""

    def command_for(self, value: str) -> str:
        """The command line that picking `value` runs."""
        return self.command.replace("{value}", value)


@dataclass
class Result:
    """What a slash command asks the TUI to do."""

    message: str | None = None
    prompt: str | None = None
    quit: bool = False
    renderable: RenderableType | None = None
    pick: PickRequest | None = None
    theme: str | None = None  # a theme to apply to every surface now


@dataclass
class TuiState:
    """The mutable state the slash commands work on."""

    client: AcpClient
    sid: str
    cwd: str
    renderer: Renderer
    session: Session | None = None
    project: Path | None = None
    model: str = ""
    mode: str = "default"
    attachments: list[Block] = field(default_factory=list)
    new_conversation: Callable[[], Awaitable[str]] | None = None
    records: RecordStore | None = None
    user_commands: dict[str, str] = field(default_factory=dict)
    status: StatusInfo = field(default_factory=StatusInfo)
    turns: list[TurnStats] = field(default_factory=list)
    list_models: Callable[[], list[dict[str, object]]] | None = None
    model_choices: list[str] = field(default_factory=list)
    resume_choices: list[str] = field(default_factory=list)
    effort: str = ""  # the level `/effort` set for this session; "" = the model's default
    load_choices: Callable[[], list[choices.Choice]] | None = None  # the models of the configured sources
    choices: list[choices.Choice] | None = None  # filled by the first `/model` or `/effort`
    save_model: Callable[[choices.Choice, str], list[str]] | None = None  # writes the `code` role: choice, effort
    ide: object | None = None  # an `IdeBridge` when the IDE channel is open
    confirm: Callable[[str], Awaitable[bool]] | None = None  # a yes/no typed on the terminal; None refuses
    surface: str = "code"  # `code` or `chat`: the session kind `/resume` lists
    busy: bool = False  # True while a turn runs


def image_block(path: Path) -> Block:
    """An ACP image block for `path`; raises `ValueError` for a type or size the agent cannot take."""
    suffix = path.suffix.lower()
    if suffix not in IMAGE_TYPES:
        raise ValueError(f"unsupported image type {suffix!r}; use one of {', '.join(sorted(IMAGE_TYPES))}")
    if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"{path} is missing or larger than {MAX_IMAGE_BYTES // 2**20} MiB")
    return {"type": "image", "data": base64.b64encode(path.read_bytes()).decode("ascii"), "mimeType": IMAGE_TYPES[suffix]}


def load_commands(project: Path | None) -> dict[str, str]:
    """User command templates from `<project>/.hal/commands/*.md`, keyed by file stem."""
    found: dict[str, str] = {}
    folder = (project / COMMANDS_DIR) if project else None
    if folder is None or not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.md")):
        if path.is_symlink() or path.stat().st_size > MAX_COMMAND_BYTES:
            log.warning("command file %s skipped: symlink or over %d bytes", path, MAX_COMMAND_BYTES)
            continue
        found[path.stem] = path.read_text(encoding="utf-8", errors="replace")
    return found


async def dispatch(line: str, state: TuiState, user_commands: dict[str, str]) -> Result:
    """Runs the slash command in `line` (which starts with `/`)."""
    name, _, args = line[1:].partition(" ")
    args = args.strip()
    if name == "exit":
        return Result(quit=True)
    if name in CODE_ONLY and state.surface != "code":
        return Result(message=f"/{name} works in `hal code`; a chat has no project copy")
    handler = BUILTINS.get(name)
    if handler is not None:
        return await handler(state, args)
    if name in user_commands:
        return Result(prompt=user_commands[name].replace(ARGUMENTS, args))
    known = sorted({*commands_for(state.surface), *user_commands})
    return Result(message=f"unknown command /{safe(name)}; known: " + ", ".join("/" + n for n in known))


async def _help(state: TuiState, args: str) -> Result:
    look = state.renderer.look
    table = Table(box=look.glyphs.box, border_style=look.palette.muted, title="Commands", title_justify="left", expand=False)
    table.add_column("Command", style=look.palette.accent, no_wrap=True)
    table.add_column("What it does")
    for usage, text in commands_for(state.surface).values():
        table.add_row(usage, text)
    for name in sorted(state.user_commands):
        table.add_row(f"/{safe(name)}", "your command from .hal/commands")
    return Result(renderable=table, message=KEY_HELP[state.surface])


async def _clear(state: TuiState, args: str) -> Result:
    if state.new_conversation is None:
        return Result(message="/clear is not available here")
    state.sid = await state.new_conversation()
    state.renderer.todo = []
    if state.effort:  # effort belongs to the agent session, which is new
        await state.client.set_effort(state.sid, state.effort)
    return Result(message="(new conversation in the same box)")


async def _resume(state: TuiState, args: str) -> Result:
    if state.records is None:
        return Result(message="/resume is not available here")
    if not args:
        rows = sessionscmd.list_sessions(state.records, state.project, RESUME_LIST_LIMIT, mode=state.surface)
        if not rows:
            return Result(message="no stored chat sessions" if state.surface == "chat" else "no stored sessions for this project")
        state.resume_choices = [str(m["id"]) for m in rows]
        items = tuple(
            PickItem(safe(m.get("title", "")) or "(untitled)", str(m["id"]), f"{m['id']}  {m.get('updated', '')}") for m in rows
        )
        return Result(
            message="sessions (use /resume N or /resume ID):\n"
            + "\n".join(f"{i}  {m['id']}  {m.get('updated', '')}  {safe(m.get('title', ''))}" for i, m in enumerate(rows, 1)),
            pick=PickRequest("Resume a session", items, "/resume {value}", state.sid),
        )
    if args.isdigit() and 1 <= int(args) <= len(state.resume_choices):
        args = state.resume_choices[int(args) - 1]
    try:
        _, records = sessionscmd.load_for_resume(state.records, args, state.project)
        await state.client.load_session(args, state.cwd, records)
    except (sessionscmd.SessionsError, AcpError) as exc:
        return Result(message=f"/resume: {safe(str(exc))}")
    state.sid = args
    return Result(message=f"resumed {args}")


async def _fork(state: TuiState, args: str) -> Result:
    if state.records is None:
        return Result(message="/fork is not available here")
    try:
        new = sessionscmd.fork(state.records, state.sid)
        _, records = sessionscmd.load_for_resume(state.records, new)
        await state.client.load_session(new, state.cwd, records)
    except (sessionscmd.SessionsError, AcpError) as exc:
        return Result(message=f"/fork: {safe(str(exc))}")
    state.sid = new
    return Result(message=f"forked into {new}; the original session is unchanged")


async def _mode(state: TuiState, args: str) -> Result:
    if not args:
        items = tuple(PickItem(name, name, MODE_NOTES[name]) for name in MODES)
        return Result(message=f"mode: {state.mode} (default, auto-edit, plan)", pick=PickRequest("Mode", items, "/mode {value}", state.mode))
    if args not in MODES:
        return Result(message="modes: default (ask per policy), auto-edit (edits in the box copy without asking), plan (read-only)")
    await state.client.set_mode(state.sid, MODES[args])
    state.mode = args
    return Result(message=f"mode: {args}")


async def _todo(state: TuiState, args: str) -> Result:
    return Result(message=state.renderer.todo_text())


def context_size_of(row: dict[str, object]) -> int:
    """The context window a gateway model row lists, or 0 when it lists none."""
    for key in CONTEXT_KEYS:
        value = row.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 0


async def _catalog(state: TuiState) -> list[choices.Choice] | None:
    """The models of the configured sources (cached for the session); None when no loader is set or it fails."""
    if state.choices is None and state.load_choices is not None:
        try:
            state.choices = await asyncio.wait_for(asyncio.to_thread(state.load_choices), CATALOG_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - a listing failure falls back to the gateway list, never crashes the loop
            log.warning("model sources could not be listed: %s", exc)
    return state.choices


def _current(state: TuiState) -> choices.Choice | None:
    return choices.find(state.choices or [], state.model) if state.model else None


def effort_support(state: TuiState) -> tuple[tuple[str, ...], str]:
    """`(levels, reason)` for the session's model, from the capability table."""
    here = _current(state)
    return effort.levels_for(effort.capability(here.family if here else "", here.name if here else state.model))


async def _switch(state: TuiState, name: str) -> str:
    """Switches the session to the gateway model `name`; returns a note about effort, or "". Raises `AcpError`."""
    await state.client.set_model(state.sid, name)
    state.model = state.status.model = name
    if not state.effort:
        return ""
    levels, reason = effort_support(state)
    if state.effort in levels:
        return ""
    await state.client.set_effort(state.sid, "")
    old, state.effort, state.status.effort = state.effort, "", ""
    return f"effort {old} dropped: {reason}"


async def _model(state: TuiState, args: str) -> Result:
    await _catalog(state)
    if args == "save":
        return await _model_save(state)
    if state.choices and args:
        picked = state.choices[int(args) - 1] if args.isdigit() and 1 <= int(args) <= len(state.choices) else choices.find(state.choices, args)
        if picked is not None:
            if not picked.gateway:
                return Result(message=f"/model: {safe(picked.source)}/{safe(picked.name)} cannot be used yet: {picked.note}")
            args = picked.gateway
    elif not state.choices and args.isdigit() and 1 <= int(args) <= len(state.model_choices):
        args = state.model_choices[int(args) - 1]
    if args:
        try:
            note = await _switch(state, args)
        except AcpError as exc:
            return Result(message=f"/model: {safe(str(exc))}")
        return Result(message=f"model: {safe(args)} from the next turn" + (f"\n{note}" if note else ""))
    current = f"model: {safe(state.model) or '(default)'}"
    if state.choices:
        return Result(message=_choice_list(state, current), pick=_choice_pick(state))
    if state.list_models is None:
        return Result(message=current)
    try:
        rows = await asyncio.wait_for(asyncio.to_thread(state.list_models), MODEL_LIST_TIMEOUT_S + 1)
    except Exception as exc:  # noqa: BLE001 - a picker failure shows the current model, never crashes the loop
        log.warning("model listing failed: %s", exc)
        return Result(message=current + "\n(the gateway did not list its models)")
    state.model_choices = [str(r["id"]) for r in rows]
    lines = [current, "models (use /model N or /model NAME):"]
    items = []
    for i, row in enumerate(rows, 1):
        size = context_size_of(row)
        mark = "*" if str(row["id"]) == state.model else " "
        lines.append(f"{mark}{i}  {safe(row['id'])}" + (f"  {size // 1024}k context" if size else ""))
        items.append(PickItem(safe(row["id"]), str(row["id"]), f"{size // 1024}k context" if size else ""))
    return Result(message="\n".join(lines), pick=PickRequest("Model", tuple(items), "/model {value}", state.model))


def _choice_list(state: TuiState, current: str) -> str:
    """The numbered list of discovered models, grouped by source, with the current one marked."""
    here = _current(state)
    lines = [current + (f"  (effort {state.effort})" if state.effort else ""), "models (use /model N or /model NAME; /model save keeps it as the code role):"]
    last = ""
    for i, c in enumerate(state.choices or [], 1):
        if c.source != last:
            lines.append(f"{safe(c.source)} ({c.kind})")
            last = c.source
        size = f"  {c.size_bytes / 1024**3:.1f} GiB" if c.size_bytes else ""
        extra = "  thinks" if c.thinks else ""
        why = f"  [{c.note}]" if c.note and not c.gateway else ""
        lines.append(f"{'*' if c is here else ' '}{i}  {safe(c.name)}{size}{extra}{why}")
    return "\n".join(lines)


def _choice_pick(state: TuiState) -> PickRequest:
    """The discovered models as a picker; a model the gateway cannot serve yet is shown disabled with the reason."""
    items = []
    for c in state.choices or []:
        size = f"{c.size_bytes / 1024**3:.1f} GiB" if c.size_bytes else ""
        facts = "  ".join(f for f in (size, "thinks" if c.thinks else "") if f)
        value = c.gateway or f"{c.source}/{c.name}"
        items.append(PickItem(f"{safe(c.source)}/{safe(c.name)}", value, c.note if not c.gateway else facts, not c.gateway))
    return PickRequest("Model", tuple(items), "/model {value}", state.model)


async def _model_save(state: TuiState) -> Result:
    here = _current(state)
    if here is None:
        return Result(message="/model save: the current model is not one of your sources; pick one with /model first")
    if state.save_model is None:
        return Result(message="/model save is not available here")
    try:
        lines = await asyncio.to_thread(state.save_model, here, state.effort)
    except Exception as exc:  # noqa: BLE001 - a failed write is shown, the session goes on; nothing was written
        return Result(message=f"/model save: {safe(str(exc))}")
    return Result(message="\n".join([f"saved {safe(here.source)}/{safe(here.name)} as the code role", *map(safe, lines)]))


async def _effort(state: TuiState, args: str) -> Result:
    await _catalog(state)
    levels, reason = effort_support(state)
    if not args:
        if not levels:
            return Result(message=f"effort: not available for {safe(state.model) or 'this model'}, {reason}")
        items = tuple(PickItem(level, level) for level in ("default", *levels))
        return Result(
            message=f"effort: {state.effort or 'default'}; this model supports {', '.join(levels)} (or default)",
            pick=PickRequest("Effort", items, "/effort {value}", state.effort or "default"),
        )
    if args not in (*effort.LEVELS, "default"):
        return Result(message=f"/effort: use one of {', '.join(effort.LEVELS)}, or default")
    if not levels:
        return Result(message=f"/effort: not available, {reason}")
    level = "" if args == "default" else args
    try:
        await state.client.set_effort(state.sid, level)
    except AcpError as exc:
        return Result(message=f"/effort: {safe(str(exc))}")
    state.effort = state.status.effort = level
    return Result(message=f"effort: {level or 'default'} from the next turn")


async def apply_code_defaults(state: TuiState, roles: dict[str, object]) -> None:
    """Sends the `code` role's `effort` when its model supports it; a failure only logs."""
    role = roles.get("code")
    level = getattr(role, "effort", "")
    if not level:
        return
    await _catalog(state)
    if level not in effort_support(state)[0]:
        return
    try:
        await state.client.set_effort(state.sid, level)
    except AcpError as exc:
        log.warning("effort %s was not applied: %s", level, exc)
        return
    state.effort = state.status.effort = level


async def _compact(state: TuiState, args: str) -> Result:
    try:
        before, after = await state.client.compact(state.sid)
    except AcpError as exc:
        return Result(message=f"/compact: {safe(str(exc))}")
    return Result(message=f"compacted: about {before} -> {after} tokens")


OUTSIDE_FLAG = "--outside"


async def _image(state: TuiState, args: str) -> Result:
    outside = OUTSIDE_FLAG in args.split()
    name = " ".join(w for w in args.split() if w != OUTSIDE_FLAG)
    path = Path(name).expanduser()
    root = (state.project or Path.cwd()).resolve()
    resolved = path if path.is_absolute() else root / path
    resolved = resolved.resolve()  # follows symlinks, so a link out of the workspace is caught
    inside = resolved == root or root in resolved.parents
    if not inside and not outside:
        return Result(message=f"/image: {safe(str(resolved))} is outside the workspace; pass {OUTSIDE_FLAG} to attach it anyway")
    try:
        state.attachments.append(image_block(resolved))
    except (ValueError, OSError) as exc:
        return Result(message=f"/image: {safe(str(exc))}")
    note = f"attached {resolved.name} to the next prompt ({len(state.attachments)} pending)"
    if not inside:
        log.warning("image outside the workspace attached: %s", resolved)
        note = f"WARNING: {resolved} is outside the workspace and goes to the model as is. {note}"
    return Result(message=note)


def _require_session(state: TuiState) -> Session:
    if state.session is None or state.session.checkpoints is None:
        raise SandboxError("this box has no checkpoints (a chat box has no work tree)")
    return state.session


async def _diff(state: TuiState, args: str) -> Result:
    try:
        patch = _require_session(state).diff(int(args) if args else None)
    except (SandboxError, ValueError) as exc:
        return Result(message=f"/diff: {exc}")
    shown = clip(safe(patch, multiline=True) or "(no changes)", MAX_DIFF_CHARS)
    return Result(message=shown)


async def _rewind(state: TuiState, args: str) -> Result:
    try:
        session = _require_session(state)
        if not args:
            assert session.checkpoints is not None
            listed = session.checkpoints.list()
            rows = [f"  {ck.n}  {ck.created}  {safe(ck.label)}" for ck in listed]
            items = tuple(PickItem(f"{ck.n}  {safe(ck.label)}", str(ck.n), str(ck.created)) for ck in reversed(listed))
            return Result(message="checkpoints (use /rewind N):\n" + "\n".join(rows), pick=PickRequest("Rewind to", items, "/rewind {value}"))
        result = session.undo(int(args))
    except (SandboxError, ValueError) as exc:
        return Result(message=f"/rewind: {exc}")
    lines = [f"restored {len(result.restored)} files, deleted {len(result.deleted)}; safety checkpoint {result.safety.n}"]
    lines += [f"  cannot restore {safe(p)} ({why})" for p, why in result.not_restorable]
    lines.append("the agent still remembers the later turns: /clear starts a fresh conversation")
    return Result(message="\n".join(lines))


async def _apply(state: TuiState, args: str) -> Result:
    """Shows the diff report, asks for a typed yes, then applies exactly the reviewed patch."""
    session = state.session
    if session is None or session.iso is None:
        return Result(message="/accept: this session has no isolated copy")
    if args not in ("", ALLOW_FLAGS):
        return Result(message=f"/accept [{ALLOW_FLAGS}]")
    try:
        rep = isolated.report(session.iso)
        reviewed = isolated.patch_bytes(session.iso)
    except SandboxError as exc:
        return Result(message=f"/accept: {exc}")
    if not rep.files:
        return Result(message="/accept: the copy has no changes")
    state.renderer.plain(isolated.render(rep))
    if state.confirm is None:  # headless or scripted: an accept needs a human at the terminal
        return Result(message="/accept: nothing applied; confirming needs a terminal (or run `hal code accept`)")
    question = f"apply {len(rep.files)} changed paths to {safe(str(session.iso.project))}? [y/N] "
    if not await state.confirm(question):
        return Result(message="/accept: nothing applied")
    try:
        applied = session.apply_isolated(allow_flags=args == ALLOW_FLAGS, expected_patch=reviewed)
    except SandboxError as exc:
        return Result(message=f"/accept: {exc}")
    return Result(message=f"applied {len(applied)} changed paths")


async def _cost(state: TuiState, args: str) -> Result:
    look = state.renderer.look
    table = Table(box=look.glyphs.box, border_style=look.palette.muted, title="Tokens (estimated until the server reports usage)", title_justify="left")
    for col in ("Turn", "Tokens", "Seconds", "tok/s"):
        table.add_column(col, justify="right")
    for i, t in enumerate(state.turns, 1):
        table.add_row(str(i), str(t.tokens), f"{t.elapsed:.1f}", f"{t.tok_s:.0f}")
    total, secs = sum(t.tokens for t in state.turns), sum(t.elapsed for t in state.turns)
    table.add_row("all", str(total), f"{secs:.1f}", f"{total / secs:.0f}" if secs > 0.05 else "0", style="bold")
    return Result(renderable=table, message="local model: no money is spent")


async def _context(state: TuiState, args: str) -> Result:
    info = state.status
    if not info.size:
        return Result(message="context size unknown: the server has not reported usage yet")
    written = min(info.used, sum(t.tokens for t in state.turns))
    prompt_side = max(0, info.used - written)
    free = max(0, info.size - info.used)
    g = state.renderer.look.glyphs
    bar = context_bar(info.used, info.size, g, 40)
    return Result(
        message=f"{bar} {percent(info.used, info.size)}%\n"
        f"  input side (system, tools, files, history)  ~{prompt_side}\n"
        f"  model output kept in history                ~{written}\n"
        f"  free                                        {free} of {info.size}\n"
        "  /compact summarises history; /clear starts fresh"
    )


async def _jobs(state: TuiState, args: str) -> Result:
    try:
        jobs = deepwork.listing()
    except (OSError, ValueError) as exc:
        return Result(message=f"/jobs: {safe(str(exc))}")
    if not jobs:
        return Result(message="no background jobs (see `hal jobs`)")
    return Result(message="\n".join(f"{j.id}  {j.state:<9} {safe(clip(j.task, 60))}" for j in jobs))


async def _ide(state: TuiState, args: str) -> Result:
    bridge = state.ide
    if bridge is None:
        return Result(message="/ide: the IDE channel is not open (it needs a project and a terminal)")
    if args in ("on", "off"):
        bridge.ambient_on = args == "on"  # type: ignore[attr-defined]
    elif args:
        return Result(message="/ide [on|off]: share the editor's open file and selection with each prompt")
    return Result(message=bridge.status())  # type: ignore[attr-defined]


async def _accept(state: TuiState, args: str) -> Result:
    return await _apply(state, args)


async def _export(state: TuiState, args: str) -> Result:
    session = state.session
    if session is None or session.iso is None:
        return Result(message="/export: this session has no isolated copy")
    target = Path(args or f"hal-{timeutil.iso_z().replace(':', '')}.patch").expanduser()
    try:
        done = isolated.export(session.iso, target, "bundle" if target.suffix == ".bundle" else "patch")
    except SandboxError as exc:
        return Result(message=f"/export: {exc}")
    return Result(message=f"exported to {done}")


async def _init(state: TuiState, args: str) -> Result:
    return Result(prompt=INIT_PROMPT)


async def _theme(state: TuiState, args: str) -> Result:
    r = state.renderer
    if args not in THEMES:
        items = tuple(PickItem(name, name) for name in THEMES)
        return Result(
            message="theme: pick one of " + ", ".join(THEMES) + " (set [ui].theme to keep it)",
            pick=PickRequest("Theme", items, "/theme {value}", r.look.theme),
        )
    r.look = load_look({"theme": args, "unicode": r.look.unicode, "show_thinking": r.look.show_thinking}, stream=r.out)
    return Result(message=f"theme: {args} for this session", theme=args)


async def _lockdown(state: TuiState, args: str) -> Result:
    info = state.status
    return Result(message=f"lockdown: {info.lockdown or 'unknown'} (read only; change it on the host with `hal config set`)")


async def _paranoia(state: TuiState, args: str) -> Result:
    info = state.status
    return Result(message=f"paranoia: {info.paranoia or 'unknown'} (read only; change it on the host with `hal config set`)")


async def _quit(state: TuiState, args: str) -> Result:
    return Result(quit=True)


COMMAND_HELP = {
    "help": ("/help", "this table"),
    "model": ("/model [N|NAME|save]", "models of your sources by source; switch, or save as the code role"),
    "effort": ("/effort [off|low|medium|high|max]", "thinking effort for models that support it"),
    "mode": ("/mode [default|auto-edit|plan]", "permission mode (Shift-Tab cycles)"),
    "compact": ("/compact", "summarise history to free context"),
    "clear": ("/clear", "start a fresh conversation in the same box"),
    "resume": ("/resume [N|ID]", "pick a saved session of this project"),
    "fork": ("/fork", "branch this session; the original is unchanged"),
    "rewind": ("/rewind [N]", "list or restore checkpoints"),
    "diff": ("/diff [N]", "patch of the copy against the real repo"),
    "accept": ("/accept [--allow-flags]", "apply the copy to the real repo"),
    "export": ("/export [FILE]", "write a patch (or .bundle) instead of applying"),
    "ide": ("/ide [on|off]", "editor connection; share the open file with prompts"),
    "cost": ("/cost", "tokens per turn and session, tok/s (local, no money)"),
    "context": ("/context", "what fills the context window"),
    "todo": ("/todo", "show the todo list (Ctrl-T toggles)"),
    "jobs": ("/jobs", "background jobs"),
    "init": ("/init", "write AGENTS.md for this repository"),
    "image": ("/image PATH", "attach an image to the next prompt"),
    "theme": ("/theme dark|light|mono", "change the colours for this session"),
    "lockdown": ("/lockdown", "show the lockdown level (read only)"),
    "paranoia": ("/paranoia", "show the paranoia level (read only)"),
    "quit": ("/quit", "leave (Ctrl-D too)"),
}
BUILTINS = {
    "help": _help, "clear": _clear, "mode": _mode, "todo": _todo, "model": _model, "effort": _effort, "compact": _compact, "resume": _resume, "fork": _fork,
    "image": _image, "diff": _diff, "rewind": _rewind, "apply": _apply, "accept": _accept, "ide": _ide, "export": _export, "cost": _cost,
    "context": _context, "jobs": _jobs, "init": _init, "theme": _theme, "lockdown": _lockdown, "paranoia": _paranoia, "quit": _quit,
}  # fmt: skip


CODE_ONLY = frozenset({"rewind", "diff", "accept", "apply", "export", "ide", "init"})  # need a project copy
KEY_HELP = {
    "code": "Enter send | Alt-Enter newline | Shift-Tab mode | Esc stop | Esc Esc /rewind | Ctrl-O output | Ctrl-T todo"
    " | @file | !cmd | # note",
    "chat": "Enter send | Alt-Enter newline | Esc stop | Ctrl-O output | Ctrl-T todo",
}


def commands_for(surface: str) -> dict[str, tuple[str, str]]:
    """The built-in commands of a surface (`code` or `chat`) with their usage and description."""
    return {name: row for name, row in COMMAND_HELP.items() if surface == "code" or name not in CODE_ONLY}


def command_descriptions(user_commands: dict[str, str], surface: str = "code") -> dict[str, str]:
    """Name -> one-line description for tab completion, built-ins first then the user's own."""
    out = {name: text for name, (_, text) in commands_for(surface).items()}
    out.update({name: "your command from .hal/commands" for name in user_commands})
    return out
