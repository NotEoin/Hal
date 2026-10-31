"""The `hal-agent` entry point: an interactive REPL, or one prompt with `-p`.

This is the only module that prints. `-p` exit codes follow `halagent.headless` (0 done, 1
error, 3 model unreachable, 4 a limit or loop, 5 refusal, 124 timeout, 130 cancelled); 2 is bad
configuration or usage. The REPL exits 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from halagent import acp, boot, doctor, headless
from halagent.agentfile import AgentFile, AgentFileError, load_agent_file
from halagent.assistant import AssistantOptions, build_assistant
from halagent.hooks import Hooks
from halagent.loop import Agent
from halagent.mcp.lite import McpError
from halagent.profiles import SHIPPED, Profile, ProfileError, load_catalogue, resolve_profile
from halagent.provider import ProviderError
from halagent.sessions import LIST_LIMIT, SessionError, SessionStore, check_session_id, new_session_id
from halagent.settings import AgentSettings, load_settings
from halagent.tools import Toolset, Workspace
from halkit import config, log, paths

EXIT_OK, EXIT_LIMIT, EXIT_CONFIG = 0, 1, 2


def _notice(text: str) -> None:
    """Prints a notice to stderr."""
    print(f"hal-agent: {text}", file=sys.stderr)


def _echo_tool(kind: str, text: str) -> None:
    """Prints tool calls to stderr as they happen."""
    if kind == "tool":
        print(f"[{kind}] {text[:200]}", file=sys.stderr)


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
    p.add_argument(
        "--config", metavar="FILE", help="agent.json run file written by the host (03-agent 3.16.3)"
    )
    p.add_argument("--toolset", choices=("coding", "assistant"), help="tools to offer (default: coding)")
    p.add_argument("--acp", action="store_true", help="serve the Agent Client Protocol on stdio")
    p.add_argument("--resume", metavar="ID", help="continue a stored session")
    p.add_argument("--fork", action="store_true", help="with --resume or --continue: copy the session first")
    p.add_argument("--continue", dest="cont", action="store_true", help="continue the latest session here")
    p.add_argument("--no-session", action="store_true", help="do not write a transcript")
    p.add_argument("--sessions-dir", help="session store (default: state_dir()/sessions)")
    sub = p.add_subparsers(dest="command")
    ss = sub.add_parser("sessions", help="list, show or export stored sessions")
    ss.add_argument("action", nargs="?", choices=("list", "show", "export"), default="list")
    ss.add_argument("id", nargs="?", help="session id for `show` and `export`")
    ss.add_argument("--cwd", help="only sessions started in this directory (default: the workspace)")
    ss.add_argument("--all", action="store_true", help="every directory, not only this one")
    ss.add_argument("--limit", type=int, default=LIST_LIMIT, help="most sessions to list")
    ss.add_argument("--format", choices=("md", "jsonl"), default="md")
    ss.add_argument("--reasoning", action="store_true", help="include the model's reasoning")
    ss.add_argument("--out", metavar="FILE", help="write the export here instead of stdout")
    dr = sub.add_parser("doctor", help="check the setup")
    dr.add_argument("--json", dest="doctor_json", action="store_true", help="print the checks as JSON")
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
    if args.command == "doctor":
        return _doctor_command(args)
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
        run_file = load_agent_file(Path(args.config)) if args.config else None
        settings = load_settings(cli=cli)
        if run_file:  # the host's resolved values win over config files; explicit flags win over both
            settings = replace(run_file.to_settings(task=bool(args.prompt)), **{k: v for k, v in cli.items()})
        if run_file and args.toolset and args.toolset != run_file.toolset:
            raise ValueError(f"--toolset {args.toolset} conflicts with the toolset in {args.config}")
        toolset = run_file.toolset if run_file else (args.toolset or "coding")
        assistant = run_file.assistant_options() if run_file else AssistantOptions()
        mcp = run_file.mcp_kwargs() if run_file else {}
        extras = run_file.extras_kwargs() if run_file else {}
        profile = run_file.profile() if run_file else None
        first_sid = run_file.session_id if run_file else ""
        root = Path(args.workspace).resolve()
        resumed = _pick_session(args, store, root)
        if args.fork:
            if not resumed:
                raise SessionError("--fork needs --resume or --continue")
            resumed = store.fork(resumed)
        user_skills = tuple(d for d in (paths.config_dir() / "skills",) if d.is_dir())
        if args.acp:
            return _serve_acp(args, settings, store, root, profile, run_file.extra_prompt if run_file else "",
                              toolset, assistant, mcp, extras, run_file)  # fmt: skip
        writer = None if args.no_session else boot.open_writer(store, resumed, root, settings, first_sid)
        extra = run_file.extra_prompt if run_file else ""
        if toolset == "assistant":
            agent = build_assistant(
                settings,
                assistant,
                root,
                session=writer,
                system_extra=extra,
                profile=profile,
                on_event=_echo_tool,
            )
        else:
            agent = Agent(
                settings,
                Toolset(
                    Workspace(root, readonly=user_skills, protect=settings.tuning.protect),
                    task_mode=bool(args.prompt),
                    syntax_guard=settings.tuning.syntax_guard,
                ),
                user_mcp=boot.start_user_mcp(root, notice=_notice, **mcp),
                hooks=Hooks.load(root, paths.config_dir()),
                session=writer,
                system_extra=boot.join(boot.prompt_extras(root, **extras), extra),
                profile=profile,
                on_event=_echo_tool,
            )
    except (config.ConfigError, ValueError, McpError, SessionError, AgentFileError) as exc:
        print(f"hal-agent: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if run_file:
        agent.mode = run_file.permissions
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
    profile: Profile | None,
    extra: str,
    toolset: str = "coding",
    assistant: AssistantOptions | None = None,
    mcp: dict | None = None,
    extras: dict | None = None,
    run_file: AgentFile | None = None,
) -> int:
    """Serves ACP on stdin and stdout (stdout carries only protocol messages)."""
    acp.serve(
        settings, root, sys.stdin, sys.stdout,
        store=store, no_session=args.no_session, profile=profile, extra=extra,
        toolset=toolset, assistant=assistant, mcp=mcp, extras=extras,
        permissions=run_file.permissions if run_file else "auto",
        first_session_id=run_file.session_id if run_file else "",
    )  # fmt: skip
    return EXIT_OK


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


def _session_command(args: argparse.Namespace, store: SessionStore) -> int:
    try:
        if args.action == "list":
            cwd = None if args.all else str(Path(args.cwd or args.workspace).resolve())
            for info in store.list(cwd=cwd, limit=args.limit):
                print(f"{info.id}  {info.created}  {info.model}  {info.cwd}")
            return EXIT_OK
        if not args.id:
            print(f"hal-agent: sessions {args.action} needs a session id", file=sys.stderr)
            return EXIT_CONFIG
        if args.action == "show":
            print(store.summary(check_session_id(args.id)))
            return EXIT_OK
        text = store.export(check_session_id(args.id), args.format, reasoning=args.reasoning)
        if args.out:
            Path(args.out).write_text(text, encoding="ascii", errors="replace")
        else:
            print(text, end="")
        return EXIT_OK
    except (SessionError, OSError) as exc:
        print(f"hal-agent: {exc}", file=sys.stderr)
        return EXIT_CONFIG


def _doctor_command(args: argparse.Namespace) -> int:
    """Prints one line per check; exit 1 when a check failed."""
    cli = {
        k: v for k, v in {"endpoint": args.endpoint, "model": args.model, "broker": args.broker}.items() if v
    }
    checks = doctor.run_checks(Path(args.config) if args.config else None, cli)
    if args.doctor_json:
        print(json.dumps([vars(c) for c in checks]))
    else:
        for c in checks:
            print(f"{c.status:<4}  {c.name:<8} {c.detail}")
    return doctor.exit_code(checks, EXIT_OK, EXIT_LIMIT)


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
