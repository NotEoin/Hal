"""The TUI turn loop: read a line, run a slash command or a turn, stream the answer, repeat.

Before each prompt the broker hears the human message (REQ-SEC-93, surface `tui`) and a
checkpoint is taken when the box has a work tree, so `/rewind` has something to return to. Ctrl-C
or Esc during a turn cancels the turn, not the program. Broker approvals appear in the escaped
approval panel and are answered on the same terminal; `hal approve` in another terminal also works.

Input prefixes: `/` a command, `!` a shell command run inside the box whose output joins the next
prompt, `#` a note appended to AGENTS.md in the copy, `@path` a file attached to the prompt. The
note and the mentions touch only the isolated copy, and only through symlink-safe opens, so the
model cannot redirect them to a host file.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import signal
import sys
from pathlib import Path
from typing import TextIO

from halkit import config as kitconfig
from halkit import paths
from halkit import fsafe, llm, modelconf, proc
from halsandbox import isolated
from halsandbox.errors import SandboxError
from rich.panel import Panel
from rich.text import Text

from .. import __version__, config, deepwork, modelscmd, pids, sessions, sessionscmd, unboxed
from ..acp.client import AcpClient, AcpError
from ..acp.permissions import PermissionBridge, socket_owned_by_us
from ..acp.records import RecordRefused, RecordStore
from ..acp.transport import AgentCrashed
from ..ide.bridge import IdeBridge
from ..ide.server import IdeServer
from .approval import make_asker
from .keys import KeyWatcher
from .mentions import attach_mentions
from .prompt import Hooks, PromptInput, private_history
from .render import Renderer, TurnStats, clean
from . import choices
from .slash import MODES, TuiState, apply_code_defaults, command_descriptions, dispatch, load_commands
from .status import StatusInfo, git_branch, segments, to_prompt_toolkit, to_rich
from .theme import UI_TABLE, Look, load_look

log = logging.getLogger(__name__)

PROMPT = "\nyou> "
MODE_ORDER = ("default", "auto-edit", "plan")  # Shift-Tab walks this ring
SHELL_TIMEOUT_S = 120.0  # a `!` command that runs longer is killed; the agent's own tools have their own caps
MAX_SHELL_CHARS = 20_000  # shell output that joins the context, about 5k tokens
MAX_NOTE_CHARS = 500
NOTIFY_MIN_S = 10.0  # a turn shorter than this never notifies
NOTIFY_LONG_S = 60.0  # a turn this long notifies even when the terminal cannot report focus
MODEL_LIST_TIMEOUT_S = 3.0
AGENTS_FILE = "AGENTS.md"
PLAN_PROMPT = "Run this plan? [d]efault mode, [a]uto-edit mode, anything else keeps plan mode: "
YES_ANSWERS = ("y", "yes")
PLAN_ANSWERS = {"d": "default", "a": "auto-edit"}  # typed by the human; agent text never reaches this table
PLAN_APPROVED_PROMPT = "The plan is approved. Carry it out."


class TuiHandler:
    """Routes the agent's updates to the renderer and its records to the host transcript."""

    def __init__(self, renderer: Renderer, records: RecordStore | None = None) -> None:
        self.renderer = renderer
        self.records = records or RecordStore()

    async def on_update(self, session_id: str, update: dict[str, object]) -> None:
        """Renders an update."""
        self.renderer.update(update)

    async def on_record(self, session_id: str, record: dict[str, object]) -> None:
        """Stores a transcript record."""
        try:
            self.records.append(session_id, record)
        except RecordRefused as exc:
            log.warning("transcript record refused: %s", exc)

    async def request_permission(
        self, session_id: str, tool_call: dict[str, object], options: list[dict[str, object]]
    ) -> dict[str, object]:
        """The agent's own asks are cancelled: only the broker grants (REQ-SEC-29)."""
        return {"outcome": "cancelled"}


def load_ui_look(out: TextIO) -> Look:
    """The `Look` from `[ui]` in the config file; a bad file is logged and the defaults are used."""
    try:
        return load_look(kitconfig.load(UI_TABLE), stream=out)
    except kitconfig.ConfigError as exc:
        log.warning("ignoring [ui] settings: %s", exc)
        return load_look(stream=out)


def read_levels() -> tuple[str, str]:
    """The lockdown level and effective paranoia from the host config, or empty strings when unreadable."""
    try:
        from halhub import policy
        from halhub.classifier import effective_paranoia
        from halsandbox import config as sandbox_config
        from halsandbox.lockdown import Policy

        level = str(sandbox_config.load()["lockdown"])
        paranoia = effective_paranoia(policy.load_policy().classifier.paranoia, Policy.for_level(level).paranoia_floor)
        return level, str(paranoia)
    except Exception as exc:  # noqa: BLE001 - the status line is informative, a broken config must not stop the TUI
        log.warning("lockdown level unreadable: %s", exc)
        return "", ""


def workdir_of(session: object) -> Path | None:
    """The host path of the working copy the box edits, or None when there is none (chat)."""
    iso = getattr(session, "iso", None)
    if iso is not None:
        return Path(iso.work)
    work = getattr(session, "work", None)
    return Path(work) if isinstance(work, (str, Path)) else None


def welcome(state: TuiState, session: object, look: Look, version: str = __version__) -> Panel:
    """The compact header panel: Hal, version, model, context, levels, runtime, workspace and copy."""
    pal, g = look.palette, look.glyphs
    info = state.status
    sep = f" {g.sep} "
    head = Text.assemble(("Hal", f"bold {pal.accent}"), (f" {version}", pal.muted))
    facts = [clean(info.model) or "model: default"]
    if info.size:
        facts.append(f"{info.size // 1024}k context")
    facts += [f"lockdown {info.lockdown}" if info.lockdown else "", f"paranoia {info.paranoia}" if info.paranoia else ""]
    runtime = getattr(session, "runtime", None) or getattr(session, "backend", "")
    facts.append(f"box {runtime}" if runtime else "")
    body = Text.assemble(head, "\n", (sep.join(f for f in facts if f), pal.muted), "\n")
    project = getattr(session, "project", None)
    iso = getattr(session, "iso", None)
    body.append(f"workspace {project or state.cwd}", style=pal.muted)
    if iso is not None:
        body.append(f"\ncopy {iso.work}  (changes leave only through /accept)", style=pal.muted)
    body.append("\n/help commands  @file attach  !cmd shell  # note  Shift-Tab mode", style=pal.muted)
    return Panel(body, box=g.box, border_style=pal.muted, expand=False)


def notify(out: TextIO, mode: str) -> None:
    """Rings the terminal bell or sends an OSC 9 desktop notification (fixed text, nothing from the model)."""
    if mode == "off" or not out.isatty():
        return
    out.write("\x1b]9;Hal: turn finished\x07" if mode == "osc9" else "\a")
    out.flush()


def turn_line(stats: TurnStats, reason: str, mode: str, *, unsandboxed: bool) -> str:
    """The end-of-turn summary: stop reason, mode, files changed, +/- lines, tokens, time and speed."""
    parts = [f"[{reason}] mode {mode}" + (" UNSANDBOXED" if unsandboxed else "")]
    if stats.files:
        parts.append(f"{len(stats.files)} file{'s' if len(stats.files) != 1 else ''} +{stats.added} -{stats.removed}")
    parts.append(f"~{stats.tokens} tok")
    parts.append(f"{stats.elapsed:.1f}s")
    if stats.tok_s:
        parts.append(f"{stats.tok_s:.0f} tok/s")
    return " | ".join(parts)


def append_note(work: Path | None, note: str) -> str:
    """Appends `note` as a bullet to AGENTS.md in the copy without following symlinks; returns a message."""
    if work is None:
        return "# note: this session has no working copy"
    text = " ".join(note.split())[:MAX_NOTE_CHARS]
    if not text:
        return "# note: write the note after the #"
    try:
        fd = fsafe.open_regular_beneath(work, AGENTS_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    except (fsafe.UnsafePathError, OSError) as exc:
        return f"# note refused: {exc}"
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(f"- {text}\n")
    return f"noted in {AGENTS_FILE} (in the copy; /accept applies it)"


async def shell_in_box(session: object, command: str, cwd: str) -> tuple[bool, str]:
    """Runs `command` in the box's container; returns (ok, output text). Refuses outside a docker box."""
    container = getattr(session, "container", "")
    if getattr(session, "backend", "") != "docker" or not container:
        return False, "!cmd needs the docker box; this session has none"
    argv = [getattr(session, "docker", "docker"), "exec", "-w", cwd, container, "sh", "-c", command]
    try:
        done = await asyncio.to_thread(proc.run, argv, timeout=SHELL_TIMEOUT_S, check=False)
    except proc.ProcTimeout:
        return False, f"timed out after {SHELL_TIMEOUT_S:.0f}s"
    except OSError as exc:
        return False, f"cannot run docker: {exc}"
    text = (done.stdout + done.stderr).decode("utf-8", "replace")[:MAX_SHELL_CHARS]
    return done.returncode == 0, f"exit {done.returncode}\n{text}"


async def refresh_status(state: TuiState, session: object) -> None:
    """Refreshes the slow parts of the status line (branch, unaccepted changes, jobs) off the event loop."""
    info = state.status
    work = workdir_of(session)
    info.branch = await asyncio.to_thread(git_branch, work)
    iso = getattr(session, "iso", None)
    if iso is not None:
        try:
            info.changes = len(await asyncio.to_thread(isolated.changes, iso))
        except (SandboxError, OSError) as exc:
            log.warning("change count unavailable: %s", exc)
    try:
        info.jobs = sum(j.state in ("queued", "running") for j in await asyncio.to_thread(deepwork.listing))
    except (OSError, ValueError) as exc:
        log.warning("job count unavailable: %s", exc)


async def open_ide(state: TuiState, session: object, project: Path | None, sid: str, renderer: Renderer) -> IdeServer | None:
    """Opens the editor socket for this session; returns None (with a note) when it cannot be opened."""
    iso = getattr(session, "iso", None)
    if project is None or iso is None:
        return None

    def changed() -> list[str]:
        try:
            return [c.path for c in isolated.changes(iso)]
        except SandboxError as exc:
            log.warning("ide: cannot list changes: %s", exc)
            return []

    holder: list[IdeBridge] = []

    async def handler(msg) -> None:  # noqa: ANN001 - a validated `ide.protocol.Message`
        await holder[0].handle(msg)

    server = IdeServer(sid, project, handler)
    try:
        await server.start()
    except (OSError, ValueError, paths.UnsafePathError, paths.RuntimeDirMissing) as exc:
        renderer.info(f"ide: channel not opened ({exc})")
        return None
    holder.append(IdeBridge(project, server, renderer.info, changed, lambda: iso.work))
    state.ide = holder[0]
    return server


async def run_tui(box: sessions.BoxedAgent, **kwargs) -> int:  # noqa: ANN003 - same keywords as `_run_tui`
    """Runs the interactive loop with a pid file registered, so `hal kill` can find this process."""
    with pids.registered("tui"):
        return await _run_tui(box, **kwargs)


def attach_model_sources(state: TuiState) -> dict[str, modelconf.Role]:
    """Lets `/model` and `/effort` use the configured model sources; returns the configured roles."""
    try:
        sources, roles = modelconf.load_sources(), modelconf.load_roles()
    except kitconfig.ConfigError as exc:
        log.warning("model config not read: %s", exc)
        return {}
    if sources:
        ollama_url = config.shared()["ollama_url"]
        state.load_choices = lambda: choices.load(sources, roles, ollama_url)
        state.save_model = lambda c, level: modelscmd.assign_role("code", c.source, c.name, level)
    return roles


async def _run_tui(
    box: sessions.BoxedAgent,
    *,
    cwd: str,
    project: Path | None = None,
    model: str = "",
    resume: str = "",
    store: RecordStore | None = None,
    out: TextIO | None = None,
    stdin_line=None,  # noqa: ANN001 - an async callable returning one line; tests inject a script
    look: Look | None = None,
) -> int:
    """Runs the interactive loop on an already started box; returns the exit status."""
    out = out or sys.stdout
    look = look or load_ui_look(out)
    loop = asyncio.get_running_loop()
    renderer = Renderer(out, look)
    store = store or RecordStore()
    client = AcpClient(box.transport, TuiHandler(renderer, store))
    session = box.session
    workdir = workdir_of(session)
    queued: list[str] = []  # notes from the approval panel's `e` key, sent as the next messages

    await client.initialize()
    if resume:
        try:
            _, records = sessionscmd.load_for_resume(store, resume, project)
        except sessionscmd.SessionsError as exc:
            renderer.plain(f"hal code: {exc}")
            return 1
        await client.load_session(resume, cwd, records)
        sid = resume
    else:
        sid = await client.new_session(cwd, [])
    sessionscmd.write_meta(store, sid, project=str(project or ""), model=model, mode="code")

    async def fresh() -> str:
        new = await client.new_session(cwd, [])
        sessionscmd.write_meta(store, new, project=str(project or ""), model=model, mode="code")
        return new

    state = TuiState(
        client, sid, cwd, renderer, session=session, project=project, model=model,
        new_conversation=fresh, records=store,
    )  # fmt: skip
    interactive = stdin_line is None and sys.stdin.isatty()
    if stdin_line is None:
        state.list_models = lambda: llm.models(timeout=MODEL_LIST_TIMEOUT_S)
    lockdown, paranoia = read_levels() if stdin_line is None else ("", "")
    state.status = StatusInfo(
        model=model, lockdown=lockdown, paranoia=paranoia, unsandboxed=session.backend == "none"
    )  # fmt: skip
    state.user_commands = load_commands(project)
    if stdin_line is None:
        await apply_code_defaults(state, attach_model_sources(state))
    def live_status() -> Text:
        """The status line with this turn's context use and speed, so the bar moves while the model works."""
        live = renderer.stats
        info = dataclasses.replace(state.status, tok_s=live.tok_s or state.status.tok_s)
        if live.size:
            info.used, info.size = live.used, live.size
        return to_rich(segments(info, look), look)

    renderer.status_provider = live_status
    await refresh_status(state, session)
    ide_server = await open_ide(state, session, project, sid, renderer) if interactive else None

    def cycle_mode() -> None:
        nxt = MODE_ORDER[(MODE_ORDER.index(state.mode) + 1) % len(MODE_ORDER)]
        asyncio.ensure_future(client.set_mode(state.sid, MODES[nxt]))
        state.mode = state.status.mode = nxt
        state.status.notice = f"mode: {nxt}"

    editor: PromptInput | None = None
    if interactive:
        editor = PromptInput(
            look,
            command_descriptions(state.user_commands),
            lambda: to_prompt_toolkit(segments(state.status, look), look),
            root=lambda: workdir,
            history=private_history(Path(os.environ.get("HAL_STATE_DIR") or "~/.local/state/hal").expanduser() / "tui-history"),
            hooks=Hooks(cycle_mode, renderer.toggle_verbose, renderer.toggle_todo),
        )

    async def read_plain(prompt: str = "") -> str:
        if prompt:
            renderer.info(prompt)
        return await loop.run_in_executor(None, input, "")

    read = stdin_line or (editor.read if editor else read_plain)

    watcher = KeyWatcher(lambda key: on_key(key))

    def on_key(key: str) -> None:
        if key == "escape":
            asyncio.ensure_future(client.cancel(state.sid))
        elif key == "verbose":
            renderer.toggle_verbose()
        elif key == "todo":
            renderer.toggle_todo()

    @contextlib.contextmanager
    def suspend():  # noqa: ANN202 - a context manager over the live region and the key watcher
        with renderer.suspended(), watcher.suspended():
            yield

    async def read_approval() -> str:
        if stdin_line is not None:
            return await stdin_line("")
        return await loop.run_in_executor(None, input, "approve [y once / a session / n deny / e note]> ")

    async def read_note() -> str:
        if stdin_line is not None:
            return await stdin_line("")
        return await loop.run_in_executor(None, input, "note for the agent> ")

    async def confirm(question: str) -> bool:
        if stdin_line is not None:
            answer = await stdin_line("")
        else:
            with suspend():
                answer = await loop.run_in_executor(None, input, question)
        return answer.strip().lower() in YES_ANSWERS

    state.confirm = confirm
    bridge = PermissionBridge(socket_owned_by_us, lambda: state.sid)
    approvals = asyncio.ensure_future(
        bridge.run(make_asker(bridge, read_approval, out, console=renderer.console, suspend=suspend, read_note=read_note, on_note=queued.append))
    )
    if session.backend == "none":
        renderer.console.print(Text(unboxed.BANNER, style=f"bold {look.palette.bad}"))
    renderer.console.print(welcome(state, session, look))
    try:
        while True:
            state.status.notice = ""
            try:
                line = (await read(PROMPT)).strip()
            except (EOFError, KeyboardInterrupt):
                renderer.console.print()
                return 0
            if not line:
                continue
            prompt = line
            if line.startswith("/"):
                result = await dispatch(line, state, state.user_commands)
                if result.renderable is not None:
                    renderer.console.print(result.renderable)
                if result.message:
                    renderer.plain(result.message)
                if result.quit:
                    return 0
                if result.prompt is None:
                    continue
                prompt = result.prompt
            elif line.startswith("!"):
                await run_shell(state, session, line[1:].strip(), cwd)
                continue
            elif line.startswith("#"):
                renderer.info(append_note(workdir, line[1:]))
                continue
            else:
                blocks, notes = attach_mentions(line, workdir, look.mention_max_bytes)
                state.attachments.extend(blocks)
                for note in notes:
                    renderer.info(note)
            reason = await _turn(state, box, prompt, out, watcher, look)
            while reason == "end_turn" and state.mode == "plan" and state.renderer.todo:
                follow_up = await approve_plan(state, read, out)
                if follow_up is None:
                    break
                reason = await _turn(state, box, follow_up, out, watcher, look)
            while queued and reason:
                reason = await _turn(state, box, queued.pop(0), out, watcher, look)
            await refresh_status(state, session)
    except AgentCrashed as exc:
        print(f"the agent failed: {exc}; the transcript is kept, `hal sessions` can resume it", file=sys.stderr)
        return 1
    finally:
        watcher.stop()
        approvals.cancel()
        if ide_server is not None:
            await ide_server.close()
        await client.close()


async def run_shell(state: TuiState, session: object, command: str, cwd: str) -> None:
    """Runs a `!` command in the box, shows a folded output and adds the full text to the next prompt."""
    if not command:
        state.renderer.info("!: write a shell command after the !")
        return
    ok, text = await shell_in_box(session, command, cwd)
    state.renderer.info(f"$ {command}")
    state.renderer.print_output(text)
    if ok or text.startswith("exit"):
        state.attachments.append({"type": "text", "text": f"Output of the shell command `{command}` the user ran:\n{text}"})
        state.renderer.info("(output added to your next message)")


async def approve_plan(state: TuiState, read, out: TextIO) -> str | None:  # noqa: ANN001 - async line reader
    """Asks whether to run the plan the agent just made; returns the follow-up prompt, or None to stay in plan mode.

    Only the human's own typed answer leaves plan mode, never agent text (the answer is read from the terminal).
    """
    state.renderer.console.print(state.renderer.todo_panel())
    answer = (await read(PLAN_PROMPT)).strip().lower()
    chosen = PLAN_ANSWERS.get(answer)
    if chosen is None:
        state.renderer.info("staying in plan mode")
        return None
    await state.client.set_mode(state.sid, MODES[chosen])
    state.mode = state.status.mode = chosen
    state.renderer.info(f"plan approved; mode: {chosen}")
    return PLAN_APPROVED_PROMPT


async def _turn(
    state: TuiState, box: sessions.BoxedAgent, text: str, out: TextIO, watcher: KeyWatcher | None = None, look: Look | None = None
) -> str:
    """Runs one prompt; returns the stop reason, or an empty string when the turn did not run."""
    session = box.session
    look = look or state.renderer.look
    if session.checkpoints is not None:
        session.checkpoint(f"before: {text[:40]}")
    ide_blocks = state.ide.blocks() if state.ide is not None else []  # type: ignore[attr-defined]
    blocks: list[dict[str, object]] = [{"type": "text", "text": text}, *state.attachments, *ide_blocks]
    state.attachments.clear()
    try:
        session.broker.on_human_message(text, sessions.SURFACE_TUI)
    except ValueError as exc:
        state.renderer.plain(f"cannot start the turn: {exc}")
        return ""
    if state.records is not None:
        sessionscmd.write_meta(state.records, state.sid, title=sessionscmd.title_of(text))
    state.renderer.begin_turn()
    if watcher is not None:
        watcher.start()
    task = asyncio.ensure_future(state.client.prompt(state.sid, blocks))
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError):  # not on a non-main thread or Windows
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.ensure_future(state.client.cancel(state.sid)))
    try:
        reason = await task
    except AcpError as exc:
        state.renderer.end_turn()
        state.renderer.plain(f"agent error: {exc}")
        return ""
    finally:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGINT)
        if watcher is not None:
            watcher.stop()
    stats = state.renderer.end_turn()
    state.turns.append(stats)
    state.status.tok_s = stats.tok_s
    if stats.size:
        state.status.used, state.status.size = stats.used, stats.size
    unsandboxed = session.backend == "none"
    state.renderer.line(turn_line(stats, str(reason), state.mode, unsandboxed=unsandboxed), look.palette.muted)
    background = watcher is not None and watcher.focus_known and not watcher.focused
    if stats.elapsed >= NOTIFY_LONG_S or (stats.elapsed >= NOTIFY_MIN_S and background):
        notify(out, look.notify)
    return str(reason)
