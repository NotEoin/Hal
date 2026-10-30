"""`hal chat`: talk to Hal in the terminal. Every turn runs in the assistant box (RESOLUTIONS I1).

    hal chat                       a conversation in the TUI; /clear starts a new one, /quit leaves
    hal chat "what's on today?"    one message
    hal chat --json "..."          one message; the reply and stop reason as JSON

The agent speaks ACP to this process through `halcore.sessions`; replies stream as they come.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import TextIO

from halkit import termsafe
from halsandbox.errors import SandboxError

from . import sessions
from .acp.records import RecordRefused, RecordStore
from .acp.transport import AgentCrashed
from .tui.app import run_tui

EXIT_REFUSED = 3  # 06-core-cli exit code table


class ChatHandler:
    """Prints the agent's text as it streams, keeps the transcript and refuses its own permission asks."""

    def __init__(self, out: TextIO | None = None, *, quiet: bool = False) -> None:
        self.out = out or sys.stdout
        self.quiet = quiet
        self.text: list[str] = []
        self.records = RecordStore()

    async def on_update(self, session_id: str, update: dict[str, object]) -> None:
        """Collects and prints message chunks; the text is escaped before it reaches the terminal."""
        if update.get("sessionUpdate") != "agent_message_chunk":
            return
        content = update.get("content")
        chunk = content.get("text") if isinstance(content, dict) else None
        if isinstance(chunk, str):
            self.text.append(chunk)
            if not self.quiet:
                print(termsafe.escape(chunk, multiline=True), end="", file=self.out, flush=True)

    async def on_record(self, session_id: str, record: dict[str, object]) -> None:
        """Stores a transcript record on the host."""
        try:
            self.records.append(session_id, record)
        except RecordRefused as exc:
            print(f"hal chat: transcript record refused: {exc}", file=sys.stderr)

    async def request_permission(
        self, session_id: str, tool_call: dict[str, object], options: list[dict[str, object]]
    ) -> dict[str, object]:
        """The agent's own asks are never granted here; broker approvals use `hal approve`."""
        return {"outcome": "cancelled"}


async def run(args: argparse.Namespace) -> int:
    """Runs one message in an assistant box, or the TUI for a conversation."""
    box_args = {"model": args.model} if args.model else {}
    if not args.message:
        box = await sessions.BoxedAgent.start(kind="chat", project=None, channel=args.channel, **box_args)
        try:
            return await run_tui(box, cwd=sessions.BOX_SCRATCH, model=args.model or "")
        finally:
            await box.close()
    handler = ChatHandler(quiet=args.json)
    chat = await sessions.open_assistant(args.channel, handler, **box_args)
    try:
        reason = await chat.prompt(" ".join(args.message))
        if args.json:
            print(json.dumps({"reply": "".join(handler.text), "stop_reason": reason}))
        else:
            print()
        return 0
    finally:
        await chat.close()


def main(argv: list[str]) -> int:
    """Runs `hal chat`; returns the exit status."""
    ap = argparse.ArgumentParser(prog="hal chat", description="Talk to Hal.")
    ap.add_argument("message", nargs="*", help="one message; omit for a conversation")
    ap.add_argument("--model", help="model for this run")
    ap.add_argument("--json", action="store_true", help="print the reply as JSON")
    ap.add_argument("--channel", default="chat", choices=["chat", "voice", "telegram"])
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except SandboxError as exc:
        print(f"hal chat: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except AgentCrashed as exc:
        print(f"hal chat: the agent failed: {exc}", file=sys.stderr)
        return 1
