"""Slash commands of the TUI: built-ins plus `.hal/commands/NAME.md` files (RESOLUTIONS I8).

A command returns a `Result`: text to print, a prompt to send to the agent, or a quit. Commands
that touch files act on the host side of the box (checkpoints, diff, accept), because nothing
reaches the real project except through `accept` (RESOLUTIONS I3). A user command file is the
person's own text from the real project, not from the box copy, so it is a human message.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from halkit import timeutil
from halsandbox import isolated
from halsandbox.errors import SandboxError

from .. import sessionscmd
from ..acp.client import AcpError
from ..acp.records import RecordStore
from rich.console import RenderableType
from rich.table import Table

from .. import deepwork
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
MODEL_LIST_TIMEOUT_S = 3.0  # a picker must not hang on a stopped gateway
CONTEXT_KEYS = ("context_length", "context_window", "n_ctx", "max_model_len")
INIT_PROMPT = (
    "Read the repository (layout, build and test commands, conventions) and write AGENTS.md at the "
    "project root for future coding agents: what the project is, how to build, test and lint it, "
    "code conventions and pitfalls. Keep it under 60 lines and verify every command you list."
)
MODES = {"default": "review", "auto-edit": "auto", "plan": "plan"}  # TUI mode name -> hal-agent mode id
ARGUMENTS = "$ARGUMENTS"
Block = dict[str, object]


@dataclass
class Result:
    """What a slash command asks the TUI to do."""

    message: str | None = None
    prompt: str | None = None
    quit: bool = False
    renderable: RenderableType | None = None


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
    ide: object | None = None  # an `IdeBridge` when the IDE channel is open
    confirm: Callable[[str], Awaitable[bool]] | None = None  # a yes/no typed on the terminal; None refuses


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
    handler = BUILTINS.get(name)
    if handler is not None:
        return await handler(state, args)
    if name in user_commands:
        return Result(prompt=user_commands[name].replace(ARGUMENTS, args))
    known = sorted({*COMMAND_HELP, *user_commands})
    return Result(message=f"unknown command /{safe(name)}; known: " + ", ".join("/" + n for n in known))


async def _help(state: TuiState, args: str) -> Result:
    look = state.renderer.look
    table = Table(box=look.glyphs.box, border_style=look.palette.muted, title="Commands", title_justify="left", expand=False)
    table.add_column("Command", style=look.palette.accent, no_wrap=True)
    table.add_column("What it does")
    for usage, text in COMMAND_HELP.values():
        table.add_row(usage, text)
    for name in sorted(state.user_commands):
        table.add_row(f"/{safe(name)}", "your command from .hal/commands")
    keys = "Enter send | Alt-Enter newline | Shift-Tab mode | Esc stop | Esc Esc /rewind | Ctrl-O output | Ctrl-T todo | @file | !cmd | # note"
    return Result(renderable=table, message=keys)


async def _clear(state: TuiState, args: str) -> Result:
    if state.new_conversation is None:
        return Result(message="/clear is not available here")
    state.sid = await state.new_conversation()
    state.renderer.todo = []
    return Result(message="(new conversation in the same box)")


async def _resume(state: TuiState, args: str) -> Result:
    if state.records is None:
        return Result(message="/resume is not available here")
    if not args:
        rows = sessionscmd.list_sessions(state.records, state.project, RESUME_LIST_LIMIT)
        if not rows:
            return Result(message="no stored sessions for this project")
        state.resume_choices = [str(m["id"]) for m in rows]
        return Result(
            message="sessions (use /resume N or /resume ID):\n"
            + "\n".join(f"{i}  {m['id']}  {m.get('updated', '')}  {safe(m.get('title', ''))}" for i, m in enumerate(rows, 1))
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
        return Result(message=f"mode: {state.mode} (default, auto-edit, plan)")
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


async def _model(state: TuiState, args: str) -> Result:
    if args.isdigit() and 1 <= int(args) <= len(state.model_choices):
        args = state.model_choices[int(args) - 1]
    if args:
        try:
            await state.client.set_model(state.sid, args)
        except AcpError as exc:
            return Result(message=f"/model: {safe(str(exc))}")
        state.model = state.status.model = args
        return Result(message=f"model: {safe(args)} from the next turn")
    current = f"model: {safe(state.model) or '(default)'}"
    if state.list_models is None:
        return Result(message=current)
    try:
        rows = await asyncio.wait_for(asyncio.to_thread(state.list_models), MODEL_LIST_TIMEOUT_S + 1)
    except Exception as exc:  # noqa: BLE001 - a picker failure shows the current model, never crashes the loop
        log.warning("model listing failed: %s", exc)
        return Result(message=current + "\n(the gateway did not list its models)")
    state.model_choices = [str(r["id"]) for r in rows]
    lines = [current, "models (use /model N or /model NAME):"]
    for i, row in enumerate(rows, 1):
        size = context_size_of(row)
        mark = "*" if str(row["id"]) == state.model else " "
        lines.append(f"{mark}{i}  {safe(row['id'])}" + (f"  {size // 1024}k context" if size else ""))
    return Result(message="\n".join(lines))


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
            rows = [f"  {ck.n}  {ck.created}  {safe(ck.label)}" for ck in session.checkpoints.list()]
            return Result(message="checkpoints (use /rewind N):\n" + "\n".join(rows))
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
    if args not in THEMES:
        return Result(message="theme: pick one of " + ", ".join(THEMES) + " (set [ui].theme to keep it)")
    r = state.renderer
    r.look = load_look({"theme": args, "unicode": r.look.unicode, "show_thinking": r.look.show_thinking}, stream=r.out)
    return Result(message=f"theme: {args} for this session")


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
    "model": ("/model [N|NAME]", "pick a model from the gateway, with context sizes"),
    "mode": ("/mode [default|auto-edit|plan]", "permission mode (Shift-Tab cycles)"),
    "compact": ("/compact", "summarise history to free context"),
    "clear": ("/clear", "start a fresh conversation in the same box"),
    "resume": ("/resume [N|ID]", "pick a saved session of this project"),
    "fork": ("/fork", "branch this session; the original is unchanged"),
    "rewind": ("/rewind [N]", "list or restore checkpoints (Esc Esc)"),
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
    "help": _help, "clear": _clear, "mode": _mode, "todo": _todo, "model": _model, "compact": _compact, "resume": _resume, "fork": _fork,
    "image": _image, "diff": _diff, "rewind": _rewind, "apply": _apply, "accept": _accept, "ide": _ide, "export": _export, "cost": _cost,
    "context": _context, "jobs": _jobs, "init": _init, "theme": _theme, "lockdown": _lockdown, "paranoia": _paranoia, "quit": _quit,
}  # fmt: skip


def command_descriptions(user_commands: dict[str, str]) -> dict[str, str]:
    """Name -> one-line description for tab completion, built-ins first then the user's own."""
    out = {name: text for name, (_, text) in COMMAND_HELP.items()}
    out.update({name: "your command from .hal/commands" for name in user_commands})
    return out
