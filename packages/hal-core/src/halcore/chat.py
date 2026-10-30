"""`hal chat`: talk to Hal in the terminal. Every turn runs in the assistant box (RESOLUTIONS I1).

    hal chat                       a conversation; /reset starts a new one, /quit leaves
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

EXIT_REFUSED = 3  # 06-core-cli exit code table
PROMPT = "\nyou> "


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
    """Opens the assistant box and runs one message or a conversation."""
    handler = ChatHandler(quiet=args.json)
    box_args = {"model": args.model} if args.model else {}
    chat = await sessions.open_assistant(args.channel, handler, **box_args)
    try:
        if args.message:
            reason = await chat.prompt(" ".join(args.message))
            if args.json:
                print(json.dumps({"reply": "".join(handler.text), "stop_reason": reason}))
            else:
                print()
            return 0
        return await _repl(chat, handler)
    finally:
        await chat.close()


async def _repl(chat: sessions.AssistantSession, handler: ChatHandler) -> int:
    print("Hal (boxed). /reset starts over, /quit or Ctrl-D leaves. Approvals: `hal approve` in another terminal.")
    loop = asyncio.get_running_loop()
    while True:
        try:
            text = (await loop.run_in_executor(None, input, PROMPT)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text == "/quit":
            return 0
        if text == "/reset":
            await chat.new_conversation()
            print("(new conversation)")
            continue
        handler.text.clear()
        print("hal> ", end="", flush=True)
        await chat.prompt(text)
        print()


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
