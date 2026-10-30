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

from .. import sessions, unboxed
from ..acp.client import AcpClient, AcpError
from ..acp.permissions import PermissionBridge, socket_owned_by_us
from ..acp.records import RecordRefused, RecordStore
from ..acp.transport import AgentCrashed
from .approval import make_asker
from .render import Renderer
from .slash import TuiState, dispatch, load_commands

log = logging.getLogger(__name__)

PROMPT = "\nyou> "


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


async def run_tui(
    box: sessions.BoxedAgent,
    *,
    cwd: str,
    project: Path | None = None,
    model: str = "",
    out: TextIO | None = None,
    stdin_line=None,  # noqa: ANN001 - an async callable returning one line; tests inject a script
) -> int:
    """Runs the interactive loop on an already started box; returns the exit status."""
    out = out or sys.stdout
    loop = asyncio.get_running_loop()
    renderer = Renderer(out)
    client = AcpClient(box.transport, TuiHandler(renderer))

    async def read_line(prompt: str = "") -> str:
        return await loop.run_in_executor(None, input, prompt)

    read = stdin_line or read_line
    await client.initialize()
    sid = await client.new_session(cwd, [])
    state = TuiState(
        client, sid, cwd, renderer, session=box.session, project=project, model=model,
        new_conversation=lambda: client.new_session(cwd, []),
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
            await _turn(state, box, prompt, out)
    except AgentCrashed as exc:
        print(f"the agent failed: {exc}; the transcript is kept, `hal sessions` can resume it", file=sys.stderr)
        return 1
    finally:
        approvals.cancel()
        await client.close()


async def _turn(state: TuiState, box: sessions.BoxedAgent, text: str, out: TextIO) -> None:
    session = box.session
    if session.checkpoints is not None:
        session.checkpoint(f"before: {text[:40]}")
    blocks: list[dict[str, object]] = [{"type": "text", "text": text}, *state.attachments]
    state.attachments.clear()
    try:
        session.broker.on_human_message(text, sessions.SURFACE_TUI)
    except ValueError as exc:
        print(f"cannot start the turn: {exc}", file=out)
        return
    task = asyncio.ensure_future(state.client.prompt(state.sid, blocks))
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError):  # not on a non-main thread or Windows
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.ensure_future(state.client.cancel(state.sid)))
    try:
        reason = await task
    except AcpError as exc:
        state.renderer.end_turn()
        print(f"agent error: {exc}", file=out)
        return
    finally:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGINT)
    state.renderer.end_turn()
    print(f"[{reason}] mode {state.mode}", file=out, flush=True)

