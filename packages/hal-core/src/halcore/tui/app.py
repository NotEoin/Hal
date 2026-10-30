"""The TUI turn loop: read a line, run a slash command or a turn, stream the answer, repeat.

Before each prompt the broker hears the human message (REQ-SEC-93, surface `tui`) and a
checkpoint is taken when the box has a work tree, so `/rewind` has something to return to. Ctrl-C
during a turn cancels the turn, not the program. Broker approvals appear in the escaped
approval view and are answered on the same terminal; `hal approve` in another terminal also works.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path
from typing import TextIO

from .. import pids, sessions, sessionscmd, unboxed
from ..acp.client import AcpClient, AcpError
from ..acp.permissions import PermissionBridge, socket_owned_by_us
from ..acp.records import RecordRefused, RecordStore
from ..acp.transport import AgentCrashed
from .approval import make_asker
from .render import Renderer
from .slash import MODES, TuiState, dispatch, load_commands

log = logging.getLogger(__name__)

PROMPT = "\nyou> "
PLAN_PROMPT = "Run this plan? [d]efault mode, [a]uto-edit mode, anything else keeps plan mode: "
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


async def run_tui(box: sessions.BoxedAgent, **kwargs) -> int:  # noqa: ANN003 - same keywords as `_run_tui`
    """Runs the interactive loop with a pid file registered, so `hal kill` can find this process."""
    with pids.registered("tui"):
        return await _run_tui(box, **kwargs)


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
) -> int:
    """Runs the interactive loop on an already started box; returns the exit status."""
    out = out or sys.stdout
    loop = asyncio.get_running_loop()
    renderer = Renderer(out)
    store = store or RecordStore()
    client = AcpClient(box.transport, TuiHandler(renderer, store))

    async def read_line(prompt: str = "") -> str:
        return await loop.run_in_executor(None, input, prompt)

    read = stdin_line or read_line
    await client.initialize()
    if resume:
        try:
            _, records = sessionscmd.load_for_resume(store, resume, project)
        except sessionscmd.SessionsError as exc:
            print(f"hal code: {exc}", file=out)
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
        client, sid, cwd, renderer, session=box.session, project=project, model=model,
        new_conversation=fresh, records=store,
    )  # fmt: skip
    bridge = PermissionBridge(socket_owned_by_us, lambda: state.sid)
    approvals = asyncio.ensure_future(bridge.run(make_asker(bridge, lambda: read(""), out)))
    commands = load_commands(project)
    where = f"{box.session.backend}, work {'copy' if box.session.iso else cwd}"
    if box.session.backend == "none":
        print(unboxed.BANNER, file=out)
    print(f"Hal ({where}). /help lists commands; Ctrl-C stops a turn; Ctrl-D leaves.", file=out)
    try:
        while True:
            try:
                line = (await read(PROMPT)).strip()
            except (EOFError, KeyboardInterrupt):
                print(file=out)
                return 0
            if not line:
                continue
            prompt = line
            if line.startswith("/"):
                result = await dispatch(line, state, commands)
                if result.message:
                    print(result.message, file=out, flush=True)
                if result.quit:
                    return 0
                if result.prompt is None:
                    continue
                prompt = result.prompt
            reason = await _turn(state, box, prompt, out)
            while reason == "end_turn" and state.mode == "plan" and state.renderer.todo:
                follow_up = await approve_plan(state, read, out)
                if follow_up is None:
                    break
                reason = await _turn(state, box, follow_up, out)
    except AgentCrashed as exc:
        print(f"the agent failed: {exc}; the transcript is kept, `hal sessions` can resume it", file=sys.stderr)
        return 1
    finally:
        approvals.cancel()
        await client.close()


async def approve_plan(state: TuiState, read, out: TextIO) -> str | None:  # noqa: ANN001 - async line reader
    """Asks whether to run the plan the agent just made; returns the follow-up prompt, or None to stay in plan mode.

    Only the human's own typed answer leaves plan mode, never agent text (the answer is read from the terminal).
    """
    print(f"Plan:\n{state.renderer.todo_text()}", file=out)
    answer = (await read(PLAN_PROMPT)).strip().lower()
    chosen = PLAN_ANSWERS.get(answer)
    if chosen is None:
        print("staying in plan mode", file=out, flush=True)
        return None
    await state.client.set_mode(state.sid, MODES[chosen])
    state.mode = chosen
    print(f"plan approved; mode: {chosen}", file=out, flush=True)
    return PLAN_APPROVED_PROMPT


async def _turn(state: TuiState, box: sessions.BoxedAgent, text: str, out: TextIO) -> str:
    """Runs one prompt; returns the stop reason, or an empty string when the turn did not run."""
    session = box.session
    if session.checkpoints is not None:
        session.checkpoint(f"before: {text[:40]}")
    blocks: list[dict[str, object]] = [{"type": "text", "text": text}, *state.attachments]
    state.attachments.clear()
    try:
        session.broker.on_human_message(text, sessions.SURFACE_TUI)
    except ValueError as exc:
        print(f"cannot start the turn: {exc}", file=out)
        return ""
    if state.records is not None:
        sessionscmd.write_meta(state.records, state.sid, title=sessionscmd.title_of(text))
    task = asyncio.ensure_future(state.client.prompt(state.sid, blocks))
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError):  # not on a non-main thread or Windows
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.ensure_future(state.client.cancel(state.sid)))
    try:
        reason = await task
    except AcpError as exc:
        state.renderer.end_turn()
        print(f"agent error: {exc}", file=out)
        return ""
    finally:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGINT)
    state.renderer.end_turn()
    marker = " UNSANDBOXED" if session.backend == "none" else ""
    print(f"[{reason}] mode {state.mode}{marker}", file=out, flush=True)
    return str(reason)

