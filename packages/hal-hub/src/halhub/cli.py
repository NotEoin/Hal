"""The `hal-hub` command line (04-hub.md, "CLI"): list, inspect, call and check tools.

    hal-hub [--config PATH] [--policy PATH] [-v|-q] COMMAND

      tools [QUERY] [--limit N] [--all] [--json]   list tools, or the best matches for a request
      show TOOL [--json]                           schema, tier, flags, verdict per profile
      modules [--all] [--json]                     modules with status, source and path
      call TOOL [key=value ...] [--json ARGS] [--yes] [--profile P] [--raw]
      TOOL [key=value ...] [--yes]                 shorthand for call (tool names contain "_")
      mcp [--transport stdio|http] [--mode meta|flat] [--profile P]
          [--approvals auto|elicit|out-of-band|deny] [--channel remote --origin remote:CLIENT]
          [--packs A,B] [--tools X,Y] [--host 127.0.0.1] [--port 8742] [--allow-origin O]...
                                                   serve the tools over MCP
      approvals [--json]                           pending approval requests of this user
      approve ID                                   allow one pending request, once
      deny ID                                      refuse one pending request
      doctor [--json]                              check manifests, workers and signatures
      setup MODULE... [--dry-run] [--force]        run declared install steps (typed y per step)
      new NAME [--dir DIR] [--exec]                create a v2 pack skeleton, hal-NAME/
      token [--rotate]                             path of the HTTP bearer token, or a new token
      version

Exit codes (REQ-HUB-56): 0 success, 1 tool error, 2 usage or configuration error, 3 denied
or approval missing. Design decisions:

- Every call goes through `Hub.call` with `front_end="cli"` in a session of kind `hub`,
  origin `cli`; near-miss argument names are mapped on this front end only.
- An `ask` is answered by the `tty` surface when stdin and stdout are terminals (REQ-HUB-58).
  `--yes` answers `once` for the one call and is refused with exit 2 unless stdin is a
  terminal and `HAL_HARNESS` is unset (REQ-HUB-53). Without a terminal the request waits on
  this process's control socket for `hal-hub approve ID` up to `[approval].timeout_s`, then
  exits 3. The `notify` surface joins it with 01's phase 2.
- A word without `_` that is not a subcommand is a usage error, never a tool lookup, so a
  subcommand can never shadow a tool (REQ-HUB-54).
- A malformed `config.toml` or `policy.toml`, or two modules with one name, exits 2 with the
  message (file and line when known) and no traceback (REQ-HUB-55).
- `--config` and `--policy` set `HAL_CONFIG` and `HAL_POLICY` for this process, so the
  registry, the policy and secret lookups all read the same files.
- `setup` runs a module's `[[install]]` steps only after a `y` typed on a terminal for each
  step; there is no `--yes`, and without a terminal it refuses (REQ-HUB-59). `--dry-run`
  prints the plan and works anywhere. A declined step exits 3, a failed one 1.
- `approvals`, `approve` and `deny` are thin wrappers over the control-socket client that
  06's `hal approvals`, `hal approve` and `hal deny` use, with the same checks: stdin and
  stdout are terminals and `HAL_HARNESS` is unset, and no flag skips the question
  (REQ-HUB-22). `approve` always answers `once`; `session` scope stays with richer surfaces.
- Results go to stdout as JSON (indent 2, or the bare string with `--raw`); messages and
  prompts go to stderr. This is the only module, with `__main__`, that prints.
- `halhub.pack` runs these same commands for one pack (`run_scoped` with a `Scope`): only
  `mcp`, `tools`, `call` and `doctor`, over the pack's own modules, with profile
  `standalone` unless `--profile` says otherwise, and `mcp` in flat mode limited to the
  pack's tools.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from halkit import log as halkit_log
from halkit.config import ConfigError
from halkit.termsafe import escape

from . import install, policy
from .approvals import ApprovalRequest, ApprovalRouter, control
from .classifier import floor_from_env
from .config import HubConfig
from .guard import GuardClient, GuardUnavailable
from .hub import CallResult, Hub
from .manifest import ModuleSpec, ToolSpec
from .registry import Registry, RegistryError
from .runner import SecretError, WorkerError, resolve_secrets
from .session import HubSession, SessionSink, new_session_id
from .surfaces import YES_ANSWERS, TtySurface, person_at_terminal, yes_allowed

EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_USAGE = 2
EXIT_DENIED = 3
USAGE_ERRORS = frozenset({"unknown_tool", "invalid_arguments"})
CLI_FIND_LIMIT = 15  # a terminal shows more than the 8 a chat model gets (v1's CLI value)
DESCRIPTION_COLUMNS = 80
SUBCOMMANDS = (
    "tools",
    "show",
    "modules",
    "call",
    "mcp",
    "approvals",
    "approve",
    "deny",
    "doctor",
    "new",
    "setup",
    "token",
    "version",
)
SECRET_ENV_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)
LOG_LEVELS = {-1: "ERROR", 0: "WARNING", 1: "DEBUG"}
REMOTE_ADDR_ENV = "HAL_REMOTE_ADDR"  # set by 07-remote's forced command; read only with --channel remote

PACK_COMMANDS = ("mcp", "tools", "call", "doctor")

__all__ = ["PACK_COMMANDS", "Scope", "main", "run_scoped"]


class UsageError(Exception):
    """A command line that cannot run; exit 2 with the message."""


@dataclass(frozen=True)
class Scope:
    """Who the commands run for: all of `hal-hub`, or one pack's modules (`halhub.pack`)."""

    prog: str = "hal-hub"
    pack: str | None = None
    modules: frozenset[str] | None = None  # None: every module the registry finds
    profile: str | None = None  # the default when `--profile` is not given

    def has(self, module: str) -> bool:
        """True when `module` belongs to this scope."""
        return self.modules is None or module in self.modules


_HAL_HUB = Scope()
_scope: ContextVar[Scope | None] = ContextVar("halhub_cli_scope", default=None)


def _current() -> Scope:
    """The scope of the running command; plain `hal-hub` outside `run_scoped`."""
    return _scope.get() or _HAL_HUB


def run_scoped(scope: Scope, argv: Sequence[str]) -> int:
    """Runs one command of `scope` (`PACK_COMMANDS` only, for a pack) and returns its exit code."""
    token = _scope.set(scope)
    try:
        return main(argv)
    finally:
        _scope.reset(token)


def main(argv: Sequence[str] | None = None) -> int:
    """Runs one `hal-hub` command and returns its exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(args)
    except UsageError as exc:
        _error(str(exc))
        return EXIT_USAGE
    except (ConfigError, RegistryError) as exc:
        _error(str(exc))
        return EXIT_USAGE
    except SystemExit as exc:  # argparse exits 2 on a bad option and 0 on --help
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    except KeyboardInterrupt:
        _error("interrupted")
        return EXIT_TOOL_ERROR


def _main(args: list[str]) -> int:
    verbosity = 0
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag in ("-h", "--help"):
            print(_usage())
            return EXIT_OK
        if flag == "-v":
            verbosity = 1
        elif flag == "-q":
            verbosity = -1
        elif flag in ("--config", "--policy"):
            if not args:
                raise UsageError(f"{flag} needs a path")
            os.environ["HAL_CONFIG" if flag == "--config" else "HAL_POLICY"] = args.pop(0)
        else:
            raise UsageError(f"unknown option {flag}; see hal-hub --help")
    halkit_log.setup("halhub", level=LOG_LEVELS[verbosity])
    if not args:
        print(_usage())
        return EXIT_USAGE
    command, rest = args[0], args[1:]
    scope = _current()
    if scope.pack is not None and command not in PACK_COMMANDS:
        raise UsageError(f"unknown command {command!r}; {scope.prog} has {', '.join(PACK_COMMANDS)}")
    if command == "version":
        from . import __version__

        print(__version__)
        return EXIT_OK
    if command == "call":
        return _call(rest)
    if command not in SUBCOMMANDS:
        if "_" not in command:
            raise UsageError(f"unknown command {command!r}; tool names contain '_'. See hal-hub --help")
        return _call([command, *rest])
    handler = {
        "tools": _tools,
        "show": _show,
        "modules": _modules,
        "mcp": _mcp,
        "approvals": _approvals,
        "approve": _approve,
        "deny": _deny,
        "doctor": _doctor,
        "new": _new,
        "setup": _setup,
        "token": _token,
    }[command]
    return handler(rest)


# -- commands --


def _call(argv: list[str]) -> int:
    parser = _parser("call", "run one tool")
    parser.add_argument("tool", nargs="?")
    parser.add_argument("pairs", nargs="*", metavar="key=value")
    parser.add_argument("--json", dest="json_args", metavar="ARGS", help="the whole argument object")
    parser.add_argument("--yes", action="store_true", help="approve this one call (a person at a terminal)")
    parser.add_argument("--profile", help="policy profile for this call")
    parser.add_argument("--raw", action="store_true", help="print a string result without JSON quoting")
    opts = parser.parse_args(argv)
    if not opts.tool:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    if opts.pairs and opts.json_args is not None:
        raise UsageError("give key=value pairs or --json, not both")
    if opts.yes and not yes_allowed():
        raise UsageError("--yes needs a person at a terminal: stdin must be a terminal and HAL_HARNESS unset")
    arguments = _json_object(opts.json_args) if opts.json_args is not None else _pairs(opts.pairs)
    hub = _new_hub()
    try:
        with _session(hub, profile=opts.profile or _current().profile, approver_yes=opts.yes) as session:
            result = hub.call(opts.tool, arguments, session, front_end="cli")
        return _report(result, raw=opts.raw)
    finally:
        hub.close()


def _tools(argv: list[str]) -> int:
    parser = _parser("tools", "list tools, or the best matches for a request")
    parser.add_argument("query", nargs="*")
    parser.add_argument("--limit", type=int, default=CLI_FIND_LIMIT)
    parser.add_argument("--all", action="store_true", help="also tools that are denied or unavailable")
    parser.add_argument("--json", action="store_true")
    opts = parser.parse_args(argv)
    hub = _new_hub()
    try:
        session = hub.open_session(kind="hub", origin="cli", profile=_current().profile)
        if opts.all:
            rows = [_tool_row(hub, session, tool, status) for tool, status in _every_tool(hub, session)]
        else:
            query = " ".join(opts.query)
            found = hub.find(query, opts.limit, session=session) if query else hub.tools(session)
            rows = [_tool_row(hub, session, tool, "ok") for tool in found]
        rows = [row for row in rows if _current().has(str(row["module"]))]
        session.close()
    finally:
        hub.close()
    if opts.json:
        _print_json(rows)
    else:
        for row in rows:
            status = "" if row["status"] == "ok" else f" [{row['status']}]"
            first = str(row["description"]).splitlines()[0] if row["description"] else ""
            columns = f"{row['name']:28} {row['tier']:6} {row['approval']:4}"
            print(f"  {columns} {first[:DESCRIPTION_COLUMNS]}{status}")
    return EXIT_OK


def _show(argv: list[str]) -> int:
    parser = _parser("show", "a tool's schema, tier, flags and verdict per profile")
    parser.add_argument("tool")
    parser.add_argument("--json", action="store_true")
    opts = parser.parse_args(argv)
    hub = _new_hub()
    try:
        found = hub.registry.lookup(opts.tool)
        if found is None:
            raise UsageError(f"no tool named {opts.tool}; try hal-hub tools {opts.tool.split('_')[0]}")
        tool, state = found
        meta = tool.meta(state.spec)
        verdicts = {
            name: str(policy.load_policy(profile=name).tool(meta).verdict) for name in policy.PROFILES
        }
    finally:
        hub.close()
    info = {
        "name": tool.name,
        "module": tool.module,
        "pack": tool.pack,
        "status": state.status,
        "description": tool.description,
        "tier": tool.tier,
        "tier_inferred": tool.tier_inferred,
        "flags": {
            "network": tool.network,
            "untrusted_output": tool.untrusted_output,
            "reviewable": tool.reviewable,
            "reversible": tool.reversible,
            "runs_code": tool.runs_code,
        },
        "timeout": tool.timeout,
        "verdicts": verdicts,
        "parameters": tool.input_schema(),
    }
    if opts.json:
        _print_json(info)
    else:
        print(f"{tool.name}  [{tool.tier}]  module {tool.module}, pack {tool.pack}, {state.status}")
        print(f"  {tool.description}")
        flags = [name for name, on in info["flags"].items() if on]
        print(f"  flags: {', '.join(flags) or 'none'}; timeout {tool.timeout:g} s")
        print("  verdicts: " + ", ".join(f"{p} {v}" for p, v in verdicts.items()))
        for p in tool.params:
            required = "required" if p.required else "optional"
            print(f"  {p.name}: {p.type}, {required}, format {p.format}  {p.description}")
    return EXIT_OK


def _modules(argv: list[str]) -> int:
    parser = _parser("modules", "modules with status, source and path")
    parser.add_argument(
        "--all", action="store_true", help="also disabled and unavailable modules, and problems"
    )
    parser.add_argument("--json", action="store_true")
    opts = parser.parse_args(argv)
    hub = _new_hub()
    try:
        states = [s for s in hub.registry.modules.values() if opts.all or s.status == "ok"]
        problems = hub.registry.problems if opts.all else ()
    finally:
        hub.close()
    rows = [
        {
            "module": s.spec.name,
            "pack": s.spec.pack,
            "status": s.status,
            "reason": s.reason,
            "source": s.spec.source,
            "version": s.spec.version,
            "path": str(s.spec.root),
            "tools": len(s.spec.tools),
            "summary": s.spec.summary,
        }
        for s in states
    ]
    if opts.json:
        _print_json(
            {"modules": rows, "problems": [{"path": str(p.path), "message": p.message} for p in problems]}
        )
        return EXIT_OK
    for row in rows:
        status = "" if row["status"] == "ok" else f"  [{row['status']}: {row['reason']}]"
        print(f"  {row['module']:14} {row['tools']:3} tools  {row['source']:11} {row['summary']}{status}")
    for problem in problems:
        print(f"  problem: {problem.path}: {problem.message}")
    return EXIT_OK


def _mcp(argv: list[str]) -> int:
    parser = _parser("mcp", "serve the tools over MCP on stdio or loopback HTTP")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--mode", choices=("meta", "flat"), help="default: [hub.mcp].mode")
    parser.add_argument("--profile", help="policy profile for this server's sessions")
    parser.add_argument("--channel", choices=("remote",), help="remote: a laptop over SSH (hal remote)")
    parser.add_argument("--origin", help="remote:<client>, with --channel remote only")
    parser.add_argument(
        "--approvals", choices=("auto", "elicit", "out-of-band", "deny"), help="default: [hub.mcp].approvals"
    )
    parser.add_argument("--packs", type=_names, help="serve only these packs (A,B)")
    parser.add_argument("--tools", type=_names, help="serve only these tools (X,Y)")
    parser.add_argument("--host", help="HTTP bind, 127.0.0.1 or ::1 (default: [hub.mcp].http_host)")
    parser.add_argument(
        "--port", type=int, help="HTTP port, 0 for any free one (default: [hub.mcp].http_port)"
    )
    parser.add_argument("--allow-origin", action="append", default=None, help="a browser origin to accept")
    parser.add_argument("--module", action="append", default=None, help="a pack's command: serve this module")
    opts = parser.parse_args(argv)
    scope = _current()
    http = opts.transport == "http"
    if not http and (opts.host or opts.port is not None or opts.allow_origin):
        raise UsageError("--host, --port and --allow-origin need --transport http")
    if opts.module and scope.modules is None:
        raise UsageError("--module is for a pack's own command; use --tools or --packs")
    profile = opts.profile or scope.profile
    from . import mcp  # deferred: only this command needs the SDK

    remote = opts.channel == mcp.REMOTE_CHANNEL
    if remote and opts.approvals not in (None, "auto"):
        raise UsageError("--channel remote always uses --approvals auto (the laptop's dialog or hal approve)")
    if opts.origin is not None and not remote:
        raise UsageError("--origin is accepted only with --channel remote")
    # a bad policy, profile or channel exits 2 before serving
    loaded = policy.load_policy(None, profile=profile, channel=opts.channel)
    if loaded.guard.enabled:  # 01 REQ-SEC-35: an enabled guard that cannot run stops the server
        try:
            GuardClient(loaded.guard).check()
        except GuardUnavailable as exc:
            _error(f"hal-hub mcp: {exc}")
            return EXIT_USAGE
    hub = _new_hub()
    try:
        tools = opts.tools if scope.modules is None else _pack_tools(hub, scope, opts.module, opts.tools)
        options = mcp.ServeOptions(
            mode=opts.mode or ("flat" if scope.pack is not None else hub.config.mcp.mode),
            profile=profile,
            channel=opts.channel,
            origin=opts.origin or mcp.ORIGIN,
            remote_addr=(os.environ.get(REMOTE_ADDR_ENV) or None) if remote else None,
            approvals="auto" if remote else opts.approvals or hub.config.mcp.approvals,
            packs=opts.packs,
            tools=tools,
        )
    except (ValueError, UsageError) as exc:
        hub.close()
        raise UsageError(str(exc)) from None
    try:
        if http:
            host = opts.host or hub.config.mcp.http_host
            mcp.check_http_host(host)  # before the token file, so a bad host changes nothing
            mcp.serve_http(
                hub,
                options,
                host=host,
                port=hub.config.mcp.http_port if opts.port is None else opts.port,
                token=mcp.load_token(),
                allow_origins=tuple(opts.allow_origin or hub.config.mcp.allow_origins),
                on_ready=lambda url: _error(f"serving MCP on {url} (token in {mcp.token_path()})"),
            )
        else:
            mcp.serve_stdio(hub, options)
    finally:
        hub.close()
    return EXIT_OK


def _pack_tools(
    hub: Hub, scope: Scope, modules: list[str] | None, tools: frozenset[str] | None
) -> frozenset[str]:
    """The tool names a pack's `mcp` serves: its modules' tools (or those of `--module`)."""
    assert scope.modules is not None
    wanted = set(modules or scope.modules)
    if wanted - scope.modules:
        names = ", ".join(sorted(wanted - scope.modules))
        raise UsageError(f"{names}: not a module of pack {scope.pack} ({', '.join(sorted(scope.modules))})")
    found = frozenset(t.name for t in hub.registry.tools() if t.module in wanted)
    if not found:
        raise UsageError(f"no usable tools found for pack {scope.pack}; run {scope.prog} doctor")
    return found if tools is None else found & tools


def _new(argv: list[str]) -> int:
    parser = _parser("new", "create the skeleton of a v2 tool pack")
    parser.add_argument("name", help="lowercase letters and digits; also the tool name prefix")
    parser.add_argument("--dir", type=Path, default=Path.cwd(), help="where to create hal-NAME/")
    parser.add_argument(
        "--exec", action="store_true", help="a stdlib worker script; imports nothing from Hal"
    )
    opts = parser.parse_args(argv)
    from . import scaffold  # deferred: only this command writes a skeleton

    try:
        written = scaffold.create(opts.name, opts.dir, exec_worker=opts.exec)
    except (ValueError, FileExistsError) as exc:
        raise UsageError(str(exc)) from None
    for path in written:
        print(path)
    return EXIT_OK


def _setup(argv: list[str]) -> int:
    parser = _parser("setup", "run a module's declared install steps, each after a typed y")
    parser.add_argument("modules", nargs="+", metavar="MODULE")
    parser.add_argument("--dry-run", action="store_true", help="print the steps and their state only")
    parser.add_argument("--force", action="store_true", help="also run steps that already succeeded")
    opts = parser.parse_args(argv)
    if not opts.dry_run:
        _require_person("setup")
    hub = _new_hub()
    try:
        specs = []
        for name in opts.modules:
            state = hub.registry.modules.get(name)
            if state is None:
                raise UsageError(f"no module named {name}; see hal-hub modules")
            specs.append(state.spec)
    finally:
        hub.close()
    for spec in specs:
        steps = install.plan(spec)
        if not steps:
            print(f"{spec.name}: no install steps")
            continue
        for step, prior in steps:
            done = "done" if prior is not None and prior.status == "ok" else "to run"
            net = ", downloads" if step.network else ""
            print(f"{spec.name}/{step.id} [{done}{net}, up to {step.timeout:g} s]: {escape(step.summary)}")
            print(f"    {escape(' '.join(step.run), multiline=False)}")
        if opts.dry_run:
            continue
        states = install.run_steps(spec, confirm=_confirm_step, force=opts.force)
        for state in states:
            print(f"{spec.name}/{state.step}: {state.status} (exit {state.exit_code})")
        if any(s.status != "ok" for s in states):
            return EXIT_TOOL_ERROR
        remaining = [step for step, prior in install.plan(spec) if prior is None or prior.status != "ok"]
        if remaining:
            _error(f"{spec.name}: stopped before {remaining[0].id}; nothing else ran")
            return EXIT_DENIED
    return EXIT_OK


def _confirm_step(step: object) -> bool:
    """A typed `y` on the terminal for one install step; anything else stops."""
    print(f"Run step {getattr(step, 'id', '?')}? Type y and Enter to run it: ", end="", flush=True)
    return sys.stdin.readline().strip().lower() in YES_ANSWERS


def _token(argv: list[str]) -> int:
    parser = _parser("token", "print the path of the HTTP bearer token, or rotate it")
    parser.add_argument("--rotate", action="store_true", help="write a new token; old clients get 401")
    opts = parser.parse_args(argv)
    from . import mcp  # deferred: the token helpers live with the server

    if opts.rotate:
        print(mcp.rotate_token())
        return EXIT_OK
    mcp.load_token()  # creates it on first use and refuses one others can read
    print(mcp.token_path())
    return EXIT_OK


def _approvals(argv: list[str]) -> int:
    parser = _parser("approvals", "pending approval requests of this user")
    parser.add_argument("--json", action="store_true")
    opts = parser.parse_args(argv)
    _require_person("approvals")
    pending = control.list_pending()
    if opts.json:
        _print_json(pending)
        return EXIT_OK
    if not pending:
        print("No pending approval requests.")
    for request in pending:
        print(f"  {request['id']}  {request['tool']} [{request['tier']}]  expires {request['expires_at']}")
        print(f"      {escape(str(request['summary']), multiline=False)}")
    return EXIT_OK


def _approve(argv: list[str]) -> int:
    return _answer("approve", argv)


def _deny(argv: list[str]) -> int:
    return _answer("deny", argv)


def _answer(command: str, argv: list[str]) -> int:
    """`approve ID` or `deny ID`: show the full call, ask, and send the answer (REQ-HUB-22)."""
    parser = _parser(command, f"{command} one pending approval request")
    parser.add_argument("id")
    opts = parser.parse_args(argv)
    _require_person(command)
    request = next((r for r in control.list_pending() if r.get("id") == opts.id), None)
    if request is None:
        raise UsageError(f"no pending request {opts.id}; see hal-hub approvals")
    print(escape(str(request["summary"]), multiline=False))
    print(escape(str(request["display"])))
    print(f"  ({escape(str(request['reason']), multiline=False)})")
    question = "Allow this exact call once? [y/N] " if command == "approve" else "Deny this call? [y/N] "
    print(question, end="", flush=True)
    if sys.stdin.readline().strip().lower() not in YES_ANSWERS:
        print("Nothing sent.")
        return EXIT_OK
    verdict = "allow" if command == "approve" else "deny"
    try:
        control.answer(request, verdict, scope="once" if verdict == "allow" else None)
    except control.ControlError as exc:
        _error(f"{opts.id}: {exc.code}: {exc}" if str(exc) != exc.code else f"{opts.id}: {exc.code}")
        return EXIT_DENIED if exc.code == "harness" else EXIT_TOOL_ERROR
    print("Allowed once." if verdict == "allow" else "Denied.")
    return EXIT_OK


def _require_person(command: str) -> None:
    if not person_at_terminal():
        raise UsageError(
            f"hal-hub {command} needs a person at a terminal: stdin and stdout must be terminals "
            "and HAL_HARNESS unset"
        )


def _doctor(argv: list[str]) -> int:
    parser = _parser("doctor", "check manifests, workers and signatures")
    parser.add_argument("--json", action="store_true")
    opts = parser.parse_args(argv)
    hub = _new_hub()
    try:
        report = doctor_report(hub)
    finally:
        hub.close()
    if opts.json:
        _print_json(report)
    else:
        for entry in report["modules"]:
            line = f"  {entry['module']:14} {entry['tools']:3} tools  {entry['status']:11} {entry['source']}"
            if entry.get("worker_ms") is not None:
                line += f"  worker {entry['worker_ms']} ms"
            print(line)
            for error in entry["errors"]:
                print(f"      error: {error}")
            for note in entry["notes"]:
                print(f"      note: {note}")
        for problem in report["problems"]:
            print(f"  problem: {problem['path']}: {problem['message']}")
        print(f"  sandbox: {report['sandbox']}")
    return EXIT_TOOL_ERROR if report["failed"] else EXIT_OK


# -- doctor --


def doctor_report(hub: Hub) -> dict[str, object]:
    """Starts every module's worker and checks functions, signatures, secrets and units (REQ-HUB-57).

    Worker failures are data in the report, never exceptions.
    """
    modules = []
    for state in hub.registry.modules.values():
        if not _current().has(state.spec.name):
            continue
        spec = state.spec
        errors: list[str] = []
        notes: list[str] = list(spec.warnings)
        worker_ms = None
        if state.status == "ok":
            worker_ms, found = _check_functions(hub, spec)
            errors += found + _check_secrets(spec)
        elif state.reason:
            notes.append(f"{state.status}: {state.reason}")
        errors += [f"unit file missing: {u.file}" for u in spec.units if not u.file.exists()]
        notes += [
            f"env {name} looks like a secret; declare it in [[module.secrets]]"
            for name in spec.env
            if SECRET_ENV_RE.search(name)
        ]
        notes += [
            f"install step {step.id} has not run; run: hal-hub setup {spec.name}"
            for step, prior in install.plan(spec)
            if prior is None or prior.status != "ok"
        ]
        modules.append(
            {
                "module": spec.name,
                "pack": spec.pack,
                "status": state.status,
                "source": spec.source,
                "version": spec.version,
                "interpreter": list(spec.interpreter) if spec.interpreter else None,
                "tools": len(spec.tools),
                "worker_ms": worker_ms,
                "errors": errors,
                "notes": notes,
            }
        )
    problems = [{"path": str(p.path), "message": p.message} for p in hub.registry.problems]
    failed = bool(problems) or any(m["errors"] for m in modules)
    return {
        "modules": modules,
        "problems": problems,
        "sandbox": hub.runner.sandbox_status(),
        "failed": failed,
    }


def _check_functions(hub: Hub, spec: ModuleSpec) -> tuple[int | None, list[str]]:
    """Starts the worker (or imports the in-process module) and compares each function with the manifest."""
    if spec.exec is not None and spec.protocol == "oneshot":
        found = shutil.which(spec.exec[0]) or os.path.exists(spec.exec[0])
        return None, [] if found else [f"one-shot command not found: {spec.exec[0]}"]
    started = time.monotonic()
    if spec.inprocess and spec.source == "builtin" and spec.python:
        functions, missing = _inprocess_signatures(spec)
    else:
        try:
            info = hub.runner.probe(spec)
        except WorkerError as exc:
            return None, [f"worker failed to start: {exc.message}"]
        functions, missing = dict(info.functions), list(info.missing)
    worker_ms = int((time.monotonic() - started) * 1000)
    issues = [f"missing function: {name}" for name in missing]
    for tool in spec.tools:
        signature = functions.get(tool.function)
        if isinstance(signature, dict):
            issues += _signature_issues(tool, signature)
    return worker_ms, issues


def _inprocess_signatures(spec: ModuleSpec) -> tuple[dict[str, object], list[str]]:
    assert spec.python is not None
    module = importlib.import_module(spec.python)
    functions: dict[str, object] = {}
    missing = []
    for tool in spec.tools:
        fn = getattr(module, tool.function, None)
        if fn is None:
            missing.append(tool.function)
            continue
        params = []
        var_keyword = False
        for p in inspect.signature(fn).parameters.values():
            if p.kind is inspect.Parameter.VAR_KEYWORD:
                var_keyword = True
            elif p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.POSITIONAL_ONLY):
                entry: dict[str, object] = {"name": p.name, "required": p.default is inspect.Parameter.empty}
                if p.default is not inspect.Parameter.empty:
                    entry["default"] = repr(p.default)
                params.append(entry)
        functions[tool.function] = {"params": params, "var_keyword": var_keyword}
    return functions, missing


def _signature_issues(tool: ToolSpec, signature: dict) -> list[str]:
    """A manifest parameter the function does not take, a required one the manifest leaves optional,
    and a manifest default that differs from the function's."""
    params = {str(p["name"]): p for p in signature.get("params", []) if isinstance(p, dict)}
    accepts_any = bool(signature.get("var_keyword"))
    issues = []
    for p in tool.params:
        found = params.get(p.name)
        if found is None:
            if not accepts_any:
                issues.append(f"{tool.name}: {tool.function}() does not accept manifest parameter {p.name!r}")
            continue
        if p.default is not None and "default" in found and found["default"] != repr(p.default):
            issues.append(
                f"{tool.name}: default of {p.name!r} is {p.default!r} in the manifest, "
                f"{found['default']} in code"
            )
    declared = {p.name: p for p in tool.params}
    for name, found in params.items():
        if found.get("required") and not (name in declared and declared[name].required):
            issues.append(
                f"{tool.name}: {tool.function}() requires {name!r}, which the manifest does not require"
            )
    return issues


def _check_secrets(spec: ModuleSpec) -> list[str]:
    if not spec.secrets:
        return []
    try:
        resolve_secrets(spec)
    except SecretError as exc:
        return [f"secret: {exc}"]
    return []


# -- helpers --


@contextmanager
def _session(hub: Hub, *, profile: str | None = None, approver_yes: bool = False) -> Iterator[HubSession]:
    """The CLI session: kind `hub`, origin `cli`, with 01's router over the `tty` surface on a
    terminal, else over this process's control socket (REQ-HUB-58)."""
    session_id = new_session_id()
    audit = SessionSink()
    tty = TtySurface(yes=approver_yes)
    server = control.ControlServer(session_id, audit=audit, on_pending=_waiting)
    router = ApprovalRouter([tty] if tty.available() else [server], audit=audit)
    session = hub.open_session(
        kind="hub", origin="cli", profile=profile, approver=router, session_id=session_id
    )
    audit.session = session
    try:
        yield session
    finally:
        server.close()
        session.close()


def _waiting(request: ApprovalRequest, path: Path) -> None:
    """Tells the person how to answer a request that waits on the control socket."""
    _error(f"{request.summary} Waiting for approval: run `hal-hub approve {request.id}` (or deny it).")


def _report(result: CallResult, *, raw: bool) -> int:
    if result.status == "ok":
        if raw and isinstance(result.result, str):
            print(result.result)
        else:
            _print_json(result.result)
        for warning in result.warnings:
            _error(f"warning: {warning}")
        return EXIT_OK
    rule = f" ({result.rule})" if result.rule else ""
    _error(f"{result.tool}: {result.error_type}{rule}: {result.message}")
    if result.status == "denied":
        return EXIT_DENIED
    return EXIT_USAGE if result.error_type in USAGE_ERRORS else EXIT_TOOL_ERROR


def _every_tool(hub: Hub, session: HubSession) -> list[tuple[ToolSpec, str]]:
    rows = []
    for state in hub.registry.modules.values():
        for tool in state.spec.tools:
            if state.status != "ok":
                rows.append((tool, state.status))
            else:
                rows.append((tool, "ok" if hub.listed(tool, session) is not None else "denied"))
    rows += [(tool, "ok" if hub.listed(tool, session) else "denied") for tool in hub.provider_tools()]
    return sorted(rows, key=lambda row: row[0].name)


def _tool_row(hub: Hub, session: HubSession, tool: ToolSpec, status: str) -> dict[str, object]:
    if status == "ok":
        row = hub.describe(tool, session)
    else:
        row = {"name": tool.name, "module": tool.module, "pack": tool.pack, "description": tool.description,
               "tier": tool.tier, "approval": "-", "parameters": tool.input_schema()}  # fmt: skip
    row["status"] = status
    return row


def _names(text: str) -> frozenset[str]:
    """A comma-separated list of names for `--packs` and `--tools`; empty is an error."""
    names = frozenset(n.strip() for n in text.split(",") if n.strip())
    if not names:
        raise argparse.ArgumentTypeError("expected one or more names, separated by commas")
    return names


def _pairs(pairs: list[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for pair in pairs:
        if "=" not in pair:
            raise UsageError(f"expected key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key] = value
    return out


def _json_object(text: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"--json is not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise UsageError("--json must be a JSON object of arguments")
    return value


def _new_hub() -> Hub:
    """The hub for this command: every module, or only the scope's modules for a pack."""
    scope = _current()
    floor = floor_from_env()  # set by the broker for a boxed session, never by the box
    if scope.modules is None:
        return Hub(paranoia_floor=floor)
    config = HubConfig.load()
    return Hub(Registry.load(config, only=scope.modules), config=config, paranoia_floor=floor)


def _usage() -> str:
    scope = _current()
    if scope.pack is None:
        return __doc__.split("Exit codes")[0].rstrip()
    return (
        f"{scope.prog} COMMAND (pack {scope.pack}, profile {scope.profile or 'default'})\n\n"
        "  mcp [--transport stdio|http] [--profile P] [--module NAME]... [--port N]   flat mode\n"
        "  tools [QUERY] [--json]\n"
        "  call TOOL [key=value ...] [--json ARGS] [--yes]\n"
        "  doctor [--json]"
    )


def _parser(name: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=f"{_current().prog} {name}", description=description)


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def _error(message: str) -> None:
    print(f"{_current().prog}: {message}", file=sys.stderr)
