"""`hal -p PROMPT`: one boxed turn without a terminal UI (RESOLUTIONS I8, REQ-CLI-97).

    hal -p "fix the failing test"                  coding box on the current directory, text out
    hal -p --assistant "what is on today?"         assistant box (/scratch only)
    hal -p --output-format json "..."              one JSON object: reply, stop reason, tool calls
    hal -p --output-format stream-json "..."       one JSON line per ACP update, then a result line

The prompt is the arguments, or standard input when none are given. The turn goes through the
same ACP client, broker hook and box as the TUI. Output is JSON written with ASCII escaping, so
agent text can never carry terminal control codes; text mode escapes it like `hal chat`. A coding
turn works on an isolated copy: the result names the session to `hal code accept`. Exit status:
0 for a finished turn, 1 for any other stop reason or an agent failure, 3 for a refused start.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import TextIO

from halsandbox.errors import SandboxError

from . import sessions
from .acp.client import AcpClient
from .acp.transport import AgentCrashed
from .chat import EXIT_REFUSED, ChatHandler

FORMATS = ("text", "json", "stream-json")
STOP_DONE = "end_turn"


class HeadlessHandler(ChatHandler):
    """A `ChatHandler` that also records tool calls and can stream every update as JSON lines."""

    def __init__(self, fmt: str, out: TextIO | None = None) -> None:
        super().__init__(out, quiet=fmt != "text")
        self.fmt = fmt
        self.tool_calls: list[dict[str, object]] = []

    async def on_update(self, session_id: str, update: dict[str, object]) -> None:
        """Collects text, notes tool calls and streams JSON lines in `stream-json` mode."""
        if update.get("sessionUpdate") == "tool_call":
            self.tool_calls.append({k: update.get(k) for k in ("toolCallId", "title", "kind")})
        if self.fmt == "stream-json":
            print(json.dumps({"type": "update", "update": update}), file=self.out, flush=True)
        await super().on_update(session_id, update)


def _read_prompt(words: list[str], stdin: TextIO) -> str:
    text = " ".join(words) if words else ("" if stdin.isatty() else stdin.read())
    if not text.strip():
        raise ValueError("no prompt: pass it as arguments or on standard input")
    return text.strip()


async def run(args: argparse.Namespace, prompt: str, out: TextIO | None = None) -> int:
    """Runs the turn and prints the result in the chosen format; returns the exit status."""
    out = out or sys.stdout
    handler = HeadlessHandler(args.output_format, out)
    if args.assistant:
        box = await sessions.BoxedAgent.start(kind="chat", project=None, channel="cli", model=args.model)
        cwd, project = sessions.BOX_SCRATCH, None
    else:
        project = Path(args.dir).resolve()
        box = await sessions.BoxedAgent.start(kind="code", project=project, channel="cli", model=args.model)
        cwd = sessions.BOX_WORK
    client = AcpClient(box.transport, handler)
    try:
        await client.initialize()
        sid = await client.new_session(cwd, [])
        box.session.broker.on_human_message(prompt, sessions.SURFACE_HEADLESS)
        reason = await client.prompt(sid, [{"type": "text", "text": prompt}])
        iso = getattr(box.session, "iso", None)
        result = {
            "type": "result",
            "reply": "".join(handler.text),
            "stop_reason": reason,
            "tool_calls": handler.tool_calls,
            "isolated_session": iso.sid if iso is not None else None,
        }
        if args.output_format == "text":
            print(file=out)
        else:
            print(json.dumps(result), file=out, flush=True)
        return 0 if reason == STOP_DONE else 1
    finally:
        await client.close()
        await box.close()


def main(argv: list[str]) -> int:
    """Runs `hal -p`; returns the exit status."""
    ap = argparse.ArgumentParser(prog="hal -p", description="One boxed turn, no terminal UI.")
    ap.add_argument("prompt", nargs="*", help="the prompt; standard input when omitted")
    ap.add_argument("--output-format", choices=FORMATS, default="text")
    ap.add_argument("--assistant", action="store_true", help="assistant box (/scratch) instead of a coding box")
    ap.add_argument("--dir", default=".", help="project for a coding turn")
    ap.add_argument("--model", help="model for this run")
    args = ap.parse_args(argv)
    try:
        prompt = _read_prompt(args.prompt, sys.stdin)
    except ValueError as exc:
        print(f"hal -p: {exc}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(args, prompt))
    except SandboxError as exc:
        print(f"hal -p: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except AgentCrashed as exc:
        print(f"hal -p: the agent failed: {exc}", file=sys.stderr)
        return 1
