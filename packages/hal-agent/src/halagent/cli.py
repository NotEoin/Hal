"""The `hal-agent` entry point: an interactive REPL, or one prompt with `-p`.

This is the only module that prints. Exit codes: 0 done, 1 a limit was hit, 2 bad
configuration or an unreachable endpoint.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from halagent.loop import Agent
from halagent.mcp.lite import McpError
from halagent.provider import ProviderError
from halagent.settings import load_settings
from halagent.tools import Workspace
from halkit import config, log

EXIT_OK, EXIT_LIMIT, EXIT_CONFIG = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hal-agent", description="Hal coding agent")
    p.add_argument("-p", "--prompt", help="run one prompt and exit")
    p.add_argument("-C", "--workspace", default=".", help="workspace root (default: current directory)")
    p.add_argument("--endpoint", help="base URL or unix socket path of the model server")
    p.add_argument("--model")
    p.add_argument("--stream", action="store_true", default=None)
    p.add_argument("--tool-format", choices=("native", "prompt"))
    p.add_argument("--max-turns", type=int)
    p.add_argument("--max-tool-calls", type=int)
    p.add_argument("--broker", help="hub MCP socket path")
    return p


def main(argv: list[str] | None = None) -> int:
    """Runs the CLI and returns the exit code."""
    args = _parser().parse_args(argv)
    log.setup("halagent")
    cli = {
        k: v
        for k, v in {
            "endpoint": args.endpoint,
            "model": args.model,
            "stream": args.stream,
            "tool_format": args.tool_format,
            "max_turns": args.max_turns,
            "max_tool_calls": args.max_tool_calls,
            "broker": args.broker,
        }.items()
        if v is not None
    }
    try:
        settings = load_settings(cli=cli)
        agent = Agent(
            settings,
            Workspace(Path(args.workspace)),
            on_event=lambda kind, text: (
                print(f"[{kind}] {text[:200]}", file=sys.stderr) if kind == "tool" else None
            ),
        )
    except (config.ConfigError, ValueError, McpError) as exc:
        print(f"hal-agent: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        if args.prompt is not None:
            return _run_once(agent, args.prompt)
        return _repl(agent)
    finally:
        agent.close()


def _run_once(agent: Agent, prompt: str) -> int:
    try:
        result = agent.run(prompt)
    except ProviderError as exc:
        print(f"hal-agent: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(result.text)
    if result.stopped != "done":
        print(f"hal-agent: stopped at {result.stopped}", file=sys.stderr)
        return EXIT_LIMIT
    return EXIT_OK


def _repl(agent: Agent) -> int:
    print("hal-agent: type a task, /exit to quit", file=sys.stderr)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return EXIT_OK
        if line in ("/exit", "/quit"):
            return EXIT_OK
        if not line:
            continue
        try:
            result = agent.run(line)
        except ProviderError as exc:
            print(f"hal-agent: {exc}", file=sys.stderr)
            continue
        print(result.text)
        if result.stopped != "done":
            print(f"hal-agent: stopped at {result.stopped}", file=sys.stderr)
