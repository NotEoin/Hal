"""The `hal-hub` command line (04-hub.md, "CLI"): list, inspect, call and check tools.

    hal-hub [--config PATH] [--policy PATH] [-v|-q] COMMAND

      tools [QUERY] [--limit N] [--all] [--json]   list tools, or the best matches for a request
      show TOOL [--json]                           schema, tier, flags, verdict per profile
      modules [--all] [--json]                     modules with status, source and path
      call TOOL [key=value ...] [--json ARGS] [--yes] [--profile P] [--raw]
      TOOL [key=value ...] [--yes]                 shorthand for call (tool names contain "_")
      mcp [--mode meta|flat] [--profile P] [--approvals auto|elicit|out-of-band|deny]
          [--packs A,B] [--tools X,Y]              serve the tools over MCP on stdio
      approvals [--json]                           pending approval requests of this user
      approve ID                                   allow one pending request, once
      deny ID                                      refuse one pending request
      doctor [--json]                              check manifests, workers and signatures
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
- `approvals`, `approve` and `deny` are thin wrappers over the control-socket client that
  06's `hal approvals`, `hal approve` and `hal deny` use, with the same checks: stdin and
  stdout are terminals and `HAL_HARNESS` is unset, and no flag skips the question
  (REQ-HUB-22). `approve` always answers `once`; `session` scope stays with richer surfaces.
- Results go to stdout as JSON (indent 2, or the bare string with `--raw`); messages and
  prompts go to stderr. This is the only module, with `__main__`, that prints.
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
from pathlib import Path

from halkit import log as halkit_log
from halkit.config import ConfigError

from . import _control_standin as control
from . import _policy_standin as policy
from ._approvals_standin import ApprovalRequest, ApprovalRouter, escape
from .hub import CallResult, Hub
from .manifest import ModuleSpec, ToolSpec
from .registry import RegistryError
from .runner import SecretError, WorkerError, resolve_secrets
from .session import HubSession, new_session_id
from .surfaces import YES_ANSWERS, TtySurface, person_at_terminal, yes_allowed

EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_USAGE = 2
EXIT_DENIED = 3
USAGE_ERRORS = frozenset({"unknown_tool", "invalid_arguments"})
CLI_FIND_LIMIT = 15  # a terminal shows more than the 8 a chat model gets (v1's CLI value)
DESCRIPTION_COLUMNS = 80
SUBCOMMANDS = ("tools", "show", "modules", "call", "mcp", "approvals", "approve", "deny", "doctor", "version")
SECRET_ENV_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)
LOG_LEVELS = {-1: "ERROR", 0: "WARNING", 1: "DEBUG"}

__all__ = ["main"]


class UsageError(Exception):
    """A command line that cannot run; exit 2 with the message."""


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
            print(__doc__.split("Exit codes")[0].rstrip())
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
        print(__doc__.split("Exit codes")[0].rstrip())
        return EXIT_USAGE
    command, rest = args[0], args[1:]
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
    hub = Hub()
    try:
        with _session(hub, profile=opts.profile, approver_yes=opts.yes) as session:
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
    hub = Hub()
    try:
        session = hub.open_session(kind="hub", origin="cli")
        if opts.all:
            rows = [_tool_row(hub, session, tool, status) for tool, status in _every_tool(hub, session)]
        else:
            query = " ".join(opts.query)
            found = hub.find(query, opts.limit, session=session) if query else hub.tools(session)
            rows = [_tool_row(hub, session, tool, "ok") for tool in found]
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
    hub = Hub()
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
    hub = Hub()
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
    parser = _parser("mcp", "serve the tools over MCP on stdio")
    parser.add_argument("--transport", choices=("stdio",), default="stdio")
    parser.add_argument("--mode", choices=("meta", "flat"), help="default: [hub.mcp].mode")
    parser.add_argument("--profile", help="policy profile for this server's sessions")
    parser.add_argument(
        "--approvals", choices=("auto", "elicit", "out-of-band", "deny"), help="default: [hub.mcp].approvals"
    )
    parser.add_argument("--packs", type=_names, help="serve only these packs (A,B)")
    parser.add_argument("--tools", type=_names, help="serve only these tools (X,Y)")
    opts = parser.parse_args(argv)
    from . import mcp  # deferred: only this command needs the SDK

    policy.load_policy(None, profile=opts.profile)  # a bad policy or profile exits 2 before serving
    hub = Hub()
    options = mcp.ServeOptions(
        mode=opts.mode or hub.config.mcp.mode,
        profile=opts.profile,
        approvals=opts.approvals or hub.config.mcp.approvals,
        packs=opts.packs,
        tools=opts.tools,
    )
    try:
        mcp.serve_stdio(hub, options)
    finally:
        hub.close()
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
    hub = Hub()
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
    holder: list[HubSession] = []

    def audit(event: str, **fields: object) -> None:
        if holder:
            holder[0].emit(event, **fields)

    tty = TtySurface(yes=approver_yes)
    server = control.ControlServer(session_id, audit=audit, on_pending=_waiting)
    router = ApprovalRouter([tty] if tty.available() else [server], audit=audit)
    session = hub.open_session(
        kind="hub", origin="cli", profile=profile, approver=router, session_id=session_id
    )
    holder.append(session)
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


def _parser(name: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=f"hal-hub {name}", description=description)


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def _error(message: str) -> None:
    print(f"hal-hub: {message}", file=sys.stderr)
