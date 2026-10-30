"""Slash commands of the TUI: built-ins plus `.hal/commands/NAME.md` files (RESOLUTIONS I8).

A command returns a `Result`: text to print, a prompt to send to the agent, or a quit. Commands
that touch files act on the host side of the box (checkpoints, diff, accept), because nothing
reaches the real project except through `accept` (RESOLUTIONS I3). A user command file is the
person's own text from the real project, not from the box copy, so it is a human message.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from halsandbox import isolated
from halsandbox.errors import SandboxError

from ..acp.client import AcpError
from .render import Renderer, clip, safe

if TYPE_CHECKING:
    from halsandbox.session import Session

    from ..acp.client import AcpClient

log = logging.getLogger(__name__)

COMMANDS_DIR = Path(".hal") / "commands"
MAX_COMMAND_BYTES = 64 * 1024  # a prompt template, not a document
MAX_IMAGE_BYTES = 8 * 2**20  # the ACP line cap is 8 MiB; base64 adds a third, so keep well below it
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_DIFF_CHARS = 20_000
MODES = {"default": "review", "auto-edit": "auto", "plan": "plan"}  # TUI mode name -> hal-agent mode id
ARGUMENTS = "$ARGUMENTS"
Block = dict[str, object]


@dataclass
class Result:
    """What a slash command asks the TUI to do."""

    message: str | None = None
    prompt: str | None = None
    quit: bool = False


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
    if name in ("quit", "exit"):
        return Result(quit=True)
    handler = BUILTINS.get(name)
    if handler is not None:
        return await handler(state, args)
    if name in user_commands:
        return Result(prompt=user_commands[name].replace(ARGUMENTS, args))
    known = sorted({*BUILTINS, "quit", *user_commands})
    return Result(message=f"unknown command /{safe(name)}; known: " + ", ".join("/" + n for n in known))


async def _help(state: TuiState, args: str) -> Result:
    return Result(message=HELP)


async def _clear(state: TuiState, args: str) -> Result:
    if state.new_conversation is None:
        return Result(message="/clear is not available here")
    state.sid = await state.new_conversation()
    state.renderer.todo = []
    return Result(message="(new conversation in the same box)")


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


async def _model(state: TuiState, args: str) -> Result:
    if args:
        try:
            await state.client.set_model(state.sid, args)
        except AcpError as exc:
            return Result(message=f"/model: {safe(str(exc))}")
        state.model = args
        return Result(message=f"model: {safe(args)} from the next turn")
    return Result(message=f"model: {safe(state.model) or '(default)'}")


async def _image(state: TuiState, args: str) -> Result:
    try:
        state.attachments.append(image_block(Path(args).expanduser()))
    except (ValueError, OSError) as exc:
        return Result(message=f"/image: {exc}")
    return Result(message=f"attached {Path(args).name} to the next prompt ({len(state.attachments)} pending)")


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
    session = state.session
    if session is None or session.iso is None:
        return Result(message="/apply: this session has no isolated copy")
    try:
        print_report = isolated.render(isolated.report(session.iso))
        applied = session.apply_isolated(allow_flags=args == "--allow-flags")
    except SandboxError as exc:
        return Result(message=f"/apply: {exc}")
    return Result(message=print_report + f"\napplied {len(applied)} changed paths")


HELP = """/help  /quit  /clear  /mode [default|auto-edit|plan]  /todo  /model  /image PATH
/diff [N]  /rewind [N]  /apply [--allow-flags]  and your commands in .hal/commands/NAME.md"""
BUILTINS = {
    "help": _help, "clear": _clear, "mode": _mode, "todo": _todo, "model": _model,
    "image": _image, "diff": _diff, "rewind": _rewind, "apply": _apply,
}  # fmt: skip
