"""The turn loop of `hal code` and `hal chat`, shared by the line UI and the full-screen UI.

`Controller` owns the ACP client, the session, the `TuiState` the slash commands work on and the
approval bridge. It reads each message from a `Ui`, runs the slash command, prefix or turn it
names, and hands every question back to the `Ui`: a yes/no confirm, the plan choice, the
approval answer. Nothing here draws or reads a key; the line UI (`halcore.tui.app.LineUi`) and
the full-screen UI (`halcore.tui.agentapp`) implement `Ui`, so both run exactly this logic.

Before each prompt the broker hears the human message (REQ-SEC-93, surface `tui`) and a
checkpoint is taken when the box has a work tree, so `/rewind` has something to return to.
Input prefixes: `/` a command, `!` a shell command run inside the box whose output joins the
next prompt, `#` a note appended to AGENTS.md in the copy, `@path` a file attached to the prompt.
The note and the mentions touch only the isolated copy, through symlink-safe opens, so the model
cannot redirect them to a host file.

Design decisions:

- Plan mode ends only on the human's choice: `Ui.choose_plan` returns a key of `PLAN_CHOICES`
  or None, and agent text never reaches that path.
- Approvals stay on the bridge: the `Ui` gives the sender `PermissionBridge.run` calls, and every
  answer still goes through `bridge.consume`, bound to the request key (REQ-SEC-23).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Protocol

from halkit import config as kitconfig
from halkit import fsafe, llm, modelconf, paths, proc
from halsandbox import isolated
from halsandbox.errors import SandboxError
from rich.panel import Panel
from rich.text import Text

from .. import __version__, config, deepwork, modelscmd, sessions, sessionscmd, unboxed
from ..acp.client import AcpClient, AcpError
from ..acp.permissions import PermissionBridge, socket_owned_by_us
from ..acp.records import RecordRefused, RecordStore
from ..acp.transport import AgentCrashed
from ..ide.bridge import IdeBridge
from ..ide.server import IdeServer
from . import choices
from .mentions import attach_mentions
from .render import Renderer, TurnStats, clean
from .slash import MODES, Result, TuiState, apply_code_defaults, dispatch, load_commands
from .status import StatusInfo, git_branch, segments, to_rich
from .theme import Look

log = logging.getLogger(__name__)

MODE_ORDER = ("default", "auto-edit", "plan")  # Shift-Tab walks this ring
SHELL_TIMEOUT_S = 120.0  # a `!` command that runs longer is killed; the agent's own tools have their own caps
MAX_SHELL_CHARS = 20_000  # shell output that joins the context, about 5k tokens
MAX_NOTE_CHARS = 500
MODEL_LIST_TIMEOUT_S = 3.0
AGENTS_FILE = "AGENTS.md"
PLAN_PROMPT = "Run this plan? [d]efault mode, [a]uto-edit mode, anything else keeps plan mode: "
PLAN_ANSWERS = {"d": "default", "a": "auto-edit"}  # typed by the human; agent text never reaches this table
PLAN_CHOICES = ("default", "auto-edit")  # what `Ui.choose_plan` may return besides None
PLAN_APPROVED_PROMPT = "The plan is approved. Carry it out."
STOP_WORDS = {  # ACP stop reasons in plain words; a normal finish says nothing
    "end_turn": "",
    "max_tokens": "stopped: hit the output limit",
    "max_turn_requests": "stopped: hit the tool-call limit for one turn",
    "refusal": "stopped: the model refused",
    "cancelled": "cancelled",
}
Sender = Callable[[dict[str, object]], Awaitable[None]]


class Ui(Protocol):
    """What the loop needs from a user interface."""

    async def read_message(self) -> str:
        """The next message the human sends; raises `EOFError` to quit."""

    async def confirm(self, question: str) -> bool:
        """A yes/no question; anything but a deliberate yes is no."""

    async def choose_plan(self, state: TuiState) -> str | None:
        """`default` or `auto-edit` to run the plan the agent made, None to stay in plan mode."""

    def approval_sender(self, bridge: PermissionBridge, on_note: Callable[[str], None]) -> Sender:
        """The `send` callable `PermissionBridge.run` calls for each broker request."""

    def show(self, result: Result) -> None:
        """Shows what a slash command returned."""

    def turn_started(self) -> None:
        """A turn began (the line UI starts its key watcher)."""

    def turn_ended(self, stats: TurnStats, reason: str) -> None:
        """A turn ended with ACP stop reason `reason`: show the end-of-turn line (and maybe ring the bell)."""

    def status_changed(self) -> None:
        """The status fields changed; redraw them."""


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
    if state.surface == "chat":
        body.append("\n/help commands  Shift-Tab mode", style=pal.muted)
    else:
        body.append("\n/help commands  @file attach  !cmd shell  # note  Shift-Tab mode", style=pal.muted)
    return Panel(body, box=g.box, border_style=pal.muted, expand=False)


def turn_line(stats: TurnStats, reason: str, mode: str, *, unsandboxed: bool) -> str:
    """The end-of-turn summary: stop reason, mode, files changed, +/- lines, tokens, time and speed."""
    parts = [f"[{reason}] mode {mode}" + (" UNSANDBOXED" if unsandboxed else "")]
    parts += turn_facts(stats)
    return " | ".join(parts)


def turn_facts(stats: TurnStats) -> list[str]:
    """Files changed, +/- lines, tokens, time and speed of one turn, as short phrases."""
    parts = []
    if stats.files:
        parts.append(f"{len(stats.files)} file{'s' if len(stats.files) != 1 else ''} +{stats.added} -{stats.removed}")
    parts.append(f"~{stats.tokens} tok")
    parts.append(f"{stats.elapsed:.1f}s")
    if stats.tok_s:
        parts.append(f"{stats.tok_s:.0f} tok/s")
    return parts


def stop_words(reason: str) -> str:
    """An ACP stop reason in plain words; empty for a normal finish."""
    return STOP_WORDS.get(reason, f"stopped: {clean(reason)}")


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


async def read_plan_choice(renderer: Renderer, read: Callable[[str], Awaitable[str]]) -> str | None:
    """The line UI's plan question: shows the todo list and the question, maps the typed letter."""
    renderer.write(renderer.todo_panel())
    renderer.line(PLAN_PROMPT.strip(), "bold")  # the editor draws only its glyph, so the question is printed here
    answer = (await read("")).strip().lower()
    return PLAN_ANSWERS.get(answer)


async def apply_plan_choice(state: TuiState, chosen: str | None) -> str | None:
    """Leaves plan mode for `chosen` and returns the follow-up prompt, or None to stay in plan mode."""
    if chosen not in PLAN_CHOICES:
        state.renderer.info("staying in plan mode")
        return None
    await state.client.set_mode(state.sid, MODES[chosen])
    state.mode = state.status.mode = chosen
    state.renderer.info(f"plan approved; mode: {chosen}")
    return PLAN_APPROVED_PROMPT


async def approve_plan(state: TuiState, read, out: object = None) -> str | None:  # noqa: ANN001 - async line reader
    """Asks on the terminal whether to run the plan; returns the follow-up prompt, or None to stay in plan mode.

    Only the human's own typed answer leaves plan mode, never agent text (the answer is read from the terminal).
    """
    return await apply_plan_choice(state, await read_plan_choice(state.renderer, read))


async def run_turn(state: TuiState, box: sessions.BoxedAgent, text: str, ui: Ui | None = None) -> str:
    """Runs one prompt; returns the stop reason, or an empty string when the turn did not run."""
    session = box.session
    look = state.renderer.look
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
    state.busy = True
    if ui is not None:
        ui.turn_started()
    task = asyncio.ensure_future(state.client.prompt(state.sid, blocks))
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):  # not on a non-main thread or Windows
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.ensure_future(state.client.cancel(state.sid)))
    try:
        reason = await task
    except AcpError as exc:
        state.renderer.end_turn()
        state.renderer.plain(f"agent error: {exc}")
        return ""
    finally:
        state.busy = False
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(signal.SIGINT)
    stats = state.renderer.end_turn()
    state.turns.append(stats)
    state.status.tok_s = stats.tok_s
    if stats.size:
        state.status.used, state.status.size = stats.used, stats.size
    if ui is not None:
        ui.turn_ended(stats, str(reason))
    else:
        state.renderer.line(turn_line(stats, str(reason), state.mode, unsandboxed=session.backend == "none"), look.palette.muted)
    return str(reason)


class Controller:
    """One interactive session: start it, then `run` it with a `Ui` until the human quits."""

    def __init__(
        self,
        box: sessions.BoxedAgent,
        renderer: Renderer,
        *,
        cwd: str,
        project: Path | None = None,
        model: str = "",
        resume: str = "",
        store: RecordStore | None = None,
        mode: str = "code",
        live: bool = True,
    ) -> None:
        self.box = box
        self.renderer = renderer
        self.cwd = cwd
        self.project = project
        self.model = model
        self.resume = resume
        self.store = store or RecordStore()
        self.mode = mode
        self.live = live  # False in tests: no gateway listing, no host config, no IDE socket
        self.session = box.session
        self.workdir = workdir_of(self.session)
        self.client = AcpClient(box.transport, TuiHandler(renderer, self.store))
        self.queued: list[str] = []  # notes from the approval view's "deny with a note", sent as the next messages
        self.bridge: PermissionBridge | None = None
        self.state: TuiState | None = None
        self._ide: IdeServer | None = None
        self._approvals: asyncio.Future[None] | None = None
        self._defaults: asyncio.Future[None] | None = None

    async def start(self) -> str:
        """Opens or resumes the ACP session and fills the state; returns an error message, or ""."""
        await self.client.initialize()
        if self.resume:
            try:
                _, records = sessionscmd.load_for_resume(self.store, self.resume, self.project)
            except sessionscmd.SessionsError as exc:
                return f"hal code: {exc}"
            await self.client.load_session(self.resume, self.cwd, records)
            sid = self.resume
        else:
            sid = await self.client.new_session(self.cwd, [])
        sessionscmd.write_meta(self.store, sid, project=str(self.project or ""), model=self.model, mode=self.mode)
        state = TuiState(
            self.client, sid, self.cwd, self.renderer, session=self.session, project=self.project, model=self.model,
            new_conversation=self.fresh, records=self.store, surface=self.mode,
        )  # fmt: skip
        self.state = state
        if self.live:
            state.list_models = lambda: llm.models(timeout=MODEL_LIST_TIMEOUT_S)
        lockdown, paranoia = read_levels() if self.live else ("", "")
        state.status = StatusInfo(
            model=self.model, lockdown=lockdown, paranoia=paranoia, unsandboxed=self.session.backend == "none",
            backend=str(getattr(self.session, "runtime", None) or getattr(self.session, "backend", "") or ""),
        )  # fmt: skip
        state.user_commands = load_commands(self.project)
        if self.live:
            # model discovery can take seconds per source; the UI must not wait for it to appear
            self._defaults = asyncio.ensure_future(apply_code_defaults(state, attach_model_sources(state)))
        self.renderer.status_provider = self.live_status
        await refresh_status(state, self.session)
        return ""

    async def open_ide(self) -> None:
        """Opens the editor channel (interactive sessions with a project only)."""
        assert self.state is not None
        self._ide = await open_ide(self.state, self.session, self.project, self.state.sid, self.renderer)

    async def fresh(self) -> str:
        """A new ACP session in the same box, recorded with this session's kind."""
        new = await self.client.new_session(self.cwd, [])
        sessionscmd.write_meta(self.store, new, project=str(self.project or ""), model=self.model, mode=self.mode)
        return new

    def live_status(self) -> Text:
        """The status line with this turn's context use and speed, so the bar moves while the model works."""
        assert self.state is not None
        return to_rich(segments(self.live_info(), self.renderer.look), self.renderer.look)

    def live_info(self) -> StatusInfo:
        """The status fields with this turn's live context use and speed."""
        assert self.state is not None
        live = self.renderer.stats
        info = dataclasses.replace(self.state.status, tok_s=live.tok_s or self.state.status.tok_s)
        if live.size:
            info.used, info.size = live.used, live.size
        return info

    def cycle_mode(self) -> str:
        """Moves to the next mode of `MODE_ORDER` (Shift-Tab) at once and tells the agent; returns it."""
        assert self.state is not None
        state = self.state
        nxt = MODE_ORDER[(MODE_ORDER.index(state.mode) + 1) % len(MODE_ORDER)]
        state.mode = state.status.mode = nxt
        asyncio.ensure_future(self.client.set_mode(state.sid, MODES[nxt]))
        return nxt

    async def cancel(self) -> None:
        """Asks the agent to stop the running turn."""
        assert self.state is not None
        await self.client.cancel(self.state.sid)

    def banner(self) -> None:
        """Writes the unsandboxed banner (when it applies) and the welcome panel."""
        assert self.state is not None
        if self.session.backend == "none":
            self.renderer.write(Text(unboxed.BANNER, style=f"bold {self.renderer.look.palette.bad}"))
        self.renderer.write(welcome(self.state, self.session, self.renderer.look))

    async def handle(self, line: str, ui: Ui) -> bool:
        """Runs one message: a slash command, a prefix or a turn; True when the human asked to quit."""
        assert self.state is not None
        state, renderer = self.state, self.renderer
        prompt = line
        if line.startswith("/"):
            result = await dispatch(line, state, state.user_commands)
            ui.show(result)
            if result.quit:
                return True
            if result.prompt is None:
                return False
            prompt = result.prompt
        elif line.startswith("!") and state.surface == "code":
            await run_shell(state, self.session, line[1:].strip(), self.cwd)
            return False
        elif line.startswith("#") and state.surface == "code":
            renderer.info(append_note(self.workdir, line[1:]))
            return False
        elif state.surface == "code":
            blocks, notes = attach_mentions(line, self.workdir, renderer.look.mention_max_bytes)
            state.attachments.extend(blocks)
            for note in notes:
                renderer.info(note)
        reason = await run_turn(state, self.box, prompt, ui)
        while reason == "end_turn" and state.mode == "plan" and renderer.todo:
            follow_up = await apply_plan_choice(state, await ui.choose_plan(state))
            if follow_up is None:
                break
            reason = await run_turn(state, self.box, follow_up, ui)
        while self.queued and reason:
            reason = await run_turn(state, self.box, self.queued.pop(0), ui)
        await refresh_status(state, self.session)
        return False

    def start_approvals(self, ui: Ui) -> None:
        """Starts polling for broker approvals of this session; `ui` shows each one."""
        assert self.state is not None
        state = self.state
        self.bridge = PermissionBridge(socket_owned_by_us, lambda: state.sid)
        self._approvals = asyncio.ensure_future(self.bridge.run(ui.approval_sender(self.bridge, self.queued.append)))

    async def run(self, ui: Ui) -> int:
        """Reads and runs messages until the human quits; returns the exit status."""
        assert self.state is not None
        self.state.confirm = ui.confirm
        self.start_approvals(ui)
        self.banner()
        try:
            while True:
                self.state.status.notice = ""
                try:
                    line = (await ui.read_message()).strip()
                except (EOFError, KeyboardInterrupt):
                    self.renderer.write()
                    return 0
                if not line:
                    continue
                if await self.handle(line, ui):
                    return 0
                ui.status_changed()
        except AgentCrashed as exc:
            sys.stderr.write(f"the agent failed: {exc}; the transcript is kept, `hal sessions` can resume it\n")
            return 1
        finally:
            await self.close()

    async def close(self) -> None:
        """Stops the approval poll, the IDE channel and the ACP client."""
        for task in (self._approvals, self._defaults):
            if task is not None:
                task.cancel()
        if self._ide is not None:
            await self._ide.close()
            self._ide = None
        await self.client.close()

    def unaccepted(self) -> int:
        """How many paths of the isolated copy differ from the project; 0 without a copy."""
        iso = getattr(self.session, "iso", None)
        if iso is None:
            return 0
        try:
            return len(isolated.changes(iso))
        except (SandboxError, OSError) as exc:
            log.warning("change count unavailable: %s", exc)
            return 0


def history_path() -> Path:
    """The private prompt history file shared by both UIs."""
    return Path(os.environ.get("HAL_STATE_DIR") or "~/.local/state/hal").expanduser() / "tui-history"


def offered(mapping: Mapping[str, object], key: str) -> bool:
    """True when the approval request offers the scope `key`."""
    scopes = mapping.get("scopes")
    return isinstance(scopes, list) and key in scopes
