"""The `hal-agent` entry point: an interactive REPL, or one prompt with `-p`.

This is the only module that prints. `-p` exit codes follow `halagent.headless` (0 done, 1
error, 3 model unreachable, 4 a limit or loop, 5 refusal, 124 timeout, 130 cancelled); 2 is bad
configuration or usage. The REPL exits 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from halagent import __version__, headless
from halagent.acp.server import AcpServer
from halagent.instructions import load_instructions
from halagent.loop import Agent
from halagent.mcp.lite import McpError
from halagent.profiles import SHIPPED, ProfileError, load_catalogue, resolve_profile
from halagent.provider import ProviderError
from halagent.sessions import SessionError, SessionStore, SessionWriter, check_session_id, new_session_id
from halagent.settings import AgentSettings, load_settings
from halagent.skills import discover_skills, render_skills
from halagent.tools import Toolset, Workspace
from halkit import config, log, paths

EXIT_OK, EXIT_LIMIT, EXIT_CONFIG = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hal-agent", description="Hal coding agent")
    p.add_argument("-p", "--prompt", help="run one prompt and exit")
    p.add_argument(
        "-C", "--workspace", "--dir", default=".", help="workspace root (default: current directory)"
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", dest="fmt", action="store_const", const="json", help="print one result object")
    fmt.add_argument("--stream-json", dest="fmt", action="store_const", const="stream-json",
                     help="print one JSON object per line")  # fmt: skip
    p.add_argument("--timeout", type=float, help="cancel the task after this many seconds (exit 124)")
    p.add_argument("--endpoint", help="base URL or unix socket path of the model server")
    p.add_argument("--model")
    p.add_argument("--stream", action="store_true", default=None)
    p.add_argument("--tool-format", choices=("native", "prompt"))
    p.add_argument("--max-turns", type=int)
    p.add_argument("--max-tool-calls", type=int)
    p.add_argument("--broker", help="hub MCP socket path")
    p.add_argument("--acp", action="store_true", help="serve the Agent Client Protocol on stdio")
    p.add_argument("--resume", metavar="ID", help="continue a stored session")
    p.add_argument("--continue", dest="cont", action="store_true", help="continue the latest session here")
    p.add_argument("--no-session", action="store_true", help="do not write a transcript")
    p.add_argument("--sessions-dir", help="session store (default: state_dir()/sessions)")
    sub = p.add_subparsers(dest="command")
    ls = sub.add_parser("sessions", help="list stored sessions")
    ls.add_argument("--all", action="store_true", help="every directory, not only this one")
    ex = sub.add_parser("export", help="print a session as Markdown or JSONL")
    ex.add_argument("id")
    ex.add_argument("--format", choices=("md", "jsonl"), default="md")
    ex.add_argument("--reasoning", action="store_true", help="include the model's reasoning")
    pr = sub.add_parser("profiles", help="show model profiles")
    pr.add_argument("action", choices=("list", "show"))
    pr.add_argument("model", nargs="?", help="model id for `show`")
    return p


def main(argv: list[str] | None = None) -> int:
    """Runs the CLI and returns the exit code."""
    args = _parser().parse_args(argv)
    log.setup("halagent")
    store = SessionStore(Path(args.sessions_dir) if args.sessions_dir else paths.state_dir() / "sessions")
    if args.command == "profiles":
        return _profiles_command(args)
    if args.command:
        return _session_command(args, store)
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
        root = Path(args.workspace).resolve()
        resumed = _pick_session(args, store, root)
        user_skills = tuple(d for d in (paths.config_dir() / "skills",) if d.is_dir())
        if args.acp:
            return _serve_acp(args, settings, store, root, user_skills)
        writer = None if args.no_session else _open_writer(store, resumed, root, settings)
        agent = Agent(
            settings,
            Toolset(Workspace(root, readonly=user_skills), task_mode=bool(args.prompt)),
            session=writer,
            system_extra=_prompt_extras(root),
            on_event=lambda kind, text: (
                print(f"[{kind}] {text[:200]}", file=sys.stderr) if kind == "tool" else None
            ),
        )
    except (config.ConfigError, ValueError, McpError, SessionError) as exc:
        print(f"hal-agent: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        if resumed:
            agent.resume(store.load(resumed))
        if args.prompt is not None:
            prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
            sid = writer.path.parent.name if writer else new_session_id()
            return headless.run_headless(
                agent, prompt, fmt=args.fmt or "text", out=sys.stdout, session_id=sid,
                cwd=str(root), timeout=args.timeout,
            )  # fmt: skip
        return _repl(agent)
    finally:
        agent.close()
        if writer:
            writer.close()


def _serve_acp(
    args: argparse.Namespace,
    settings: AgentSettings,
    store: SessionStore,
    root: Path,
    user_skills: tuple[Path, ...],
) -> int:
    """Serves ACP on stdin and stdout (stdout carries only protocol messages)."""

    def factory(cwd: Path, sid: str, on_update: Callable[[dict], None]) -> Agent:
        writer = None if args.no_session else _open_writer(store, "", cwd, settings, sid)
        return Agent(
            settings,
            Toolset(Workspace(cwd, readonly=user_skills)),
            session=writer,
            system_extra=_prompt_extras(cwd),
            on_update=on_update,
        )

    AcpServer(root, factory, sys.stdin, sys.stdout).serve()
    return EXIT_OK


def _prompt_extras(root: Path) -> str:
    """The "Project instructions" and "Skills" sections of the system prompt."""
    instructions = load_instructions(root, root, paths.config_dir() / "AGENTS.md")
    skills, _ = discover_skills(root, (paths.config_dir() / "skills",))
    parts = [
        f"## Project instructions\n\n{instructions.text}" if instructions.text else "",
        render_skills(skills),
    ]
    return "\n\n".join(p for p in parts if p)


def _pick_session(args: argparse.Namespace, store: SessionStore, root: Path) -> str:
    """The id to resume, or an empty string for a new session."""
    if args.resume:
        return check_session_id(args.resume)
    if args.cont:
        latest = store.latest(cwd=str(root))
        if latest is None:
            raise SessionError(f"no earlier session in {root}")
        return latest.id
    return ""


def _open_writer(
    store: SessionStore, resumed: str, root: Path, settings: AgentSettings, sid: str = ""
) -> SessionWriter:
    sid = resumed or sid or new_session_id()
    header = {"id": sid, "agent": f"halagent {__version__}", "mode": "headless", "toolset": "coding",
              "cwd": str(root), "model": settings.model, "api": "openai"}  # fmt: skip
    return SessionWriter(store.path(sid), header)


def _session_command(args: argparse.Namespace, store: SessionStore) -> int:
    try:
        if args.command == "sessions":
            cwd = None if args.all else str(Path(args.workspace).resolve())
            for info in store.list(cwd=cwd):
                print(f"{info.id}  {info.created}  {info.model}  {info.cwd}")
            return EXIT_OK
        print(store.export(args.id, args.format, reasoning=args.reasoning), end="")
        return EXIT_OK
    except SessionError as exc:
        print(f"hal-agent: {exc}", file=sys.stderr)
        return EXIT_CONFIG


def _profiles_command(args: argparse.Namespace) -> int:
    """`profiles list` prints the families; `profiles show MODEL` the resolved values and sources."""
    try:
        if args.action == "list":
            _, entries = load_catalogue(SHIPPED)
            for e in entries:
                print(f"{e['family']}  {', '.join(e['match'])}")
            return EXIT_OK
        if not args.model:
            print("hal-agent: profiles show needs a model id", file=sys.stderr)
            return EXIT_CONFIG
        profile = resolve_profile(args.model, user_file=paths.config_dir() / "model-profiles.toml")
    except ProfileError as exc:
        print(f"hal-agent: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"model {profile.model}  family {profile.family}")
    for key, value in profile.values.items():
        print(f"{key} = {json.dumps(value)}  # {profile.sources[key]}")
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
        if result.stopped not in ("done", "finished"):
            print(f"hal-agent: stopped at {result.stopped}", file=sys.stderr)
