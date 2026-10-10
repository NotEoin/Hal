"""`hal chat`: talk to Hal in the terminal, or send one message and print the reply.

    hal chat                       a conversation; /reset forgets it, /tools shows the last turn's tools
    hal chat "what's on today?"    one message
    hal chat --json "..."          one message, the whole turn as JSON (reply, tools, timings)
"""
from __future__ import annotations

import argparse
import json
import sys

from .brain import Brain


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="hal chat", description="Talk to Hal.")
    ap.add_argument("message", nargs="*", help="one message; omit for a conversation")
    ap.add_argument("--model", help="Ollama model for this run")
    ap.add_argument("--json", action="store_true", help="print the whole turn as JSON")
    ap.add_argument("--no-tools", action="store_true", help="plain chat, without extensions")
    ap.add_argument("--channel", default="chat", choices=["chat", "voice", "telegram"])
    args = ap.parse_args(argv)
    brain = Brain(channel=args.channel, model=args.model, tools=not args.no_tools)
    try:
        if args.message:
            text = " ".join(args.message)
            if args.json:
                brain.reply(text)
                print(json.dumps(brain.last_turn, indent=2, default=str))
            else:
                for chunk in brain.ask(text):
                    print(chunk, end="", flush=True)
                print()
            return 0
        return _repl(brain)
    except OSError as e:
        print(f"hal: cannot reach the model at {brain.url} ({e}). Is Ollama running?", file=sys.stderr)
        return 1


def _repl(brain: Brain) -> int:
    tools = "no extensions" if not brain.hub else f"{len(brain.hub.all_tools())} tools"
    print(f"Hal ({brain.model}, {tools}). /reset to start over, /tools for the last turn's tools, Ctrl-D to leave.")
    brain.warm()
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text == "/reset":
            brain.reset()
            print("(forgotten)")
            continue
        if text == "/tools":
            for t in brain.last_turn.get("tools", []):
                print(f"  {t['name']}({json.dumps(t['args'])}) -> {'ok' if t['ok'] else 'error'} {t['result'][:160]}")
            continue
        print("hal> ", end="", flush=True)
        for chunk in brain.ask(text):
            print(chunk, end="", flush=True)
        print()
