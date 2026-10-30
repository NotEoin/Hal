"""The `hal-remote` command line; the only module of the package that prints.

Every subcommand of the design (07-remote.md, CLI synopsis) is registered; until its
implementation step lands it exits with `EXIT_USAGE` and says so, so a half-built install fails
loudly instead of doing nothing. Exit codes: 0 ok; 1 check or operation failed; 2 usage, config
or not-implemented; 3 not set up; 4 tunnel not up; 64 and 75 are reserved for `serve`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import __version__, serve, servercheck
from . import config as _config  # noqa: F401 - import registers the [remote] tables

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
IMPLEMENTED = frozenset({"server-check", "serve"})

HARNESSES = ("claude", "qwen", "opencode", "codex", "crush", "goose", "cline", "openai", "mcp")
COMMANDS = (
    "setup", "up", "down", "status", "env", "connect", "profiles",
    "authorize", "revoke", "clients", "server-check", "serve", "tunnel-run",
)  # fmt: skip


def build_parser() -> argparse.ArgumentParser:
    """Builds the argparse tree for `hal-remote`."""
    parser = argparse.ArgumentParser(prog="hal-remote", description="Hal remote: SSH add-on")
    parser.add_argument("--version", action="version", version=f"hal-remote {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("server-check", help="check the desktop's sshd and Hal lines (read-only)")
    check.add_argument("--json", action="store_true", help="print the results as JSON")
    check.add_argument(
        "--sshd-t", action="store_true", help="read the effective config with `sudo -n sshd -T`"
    )
    check.add_argument("--fix", action="store_true", help="delete token deliveries past their TTL")
    # serve is the forced command: it parses its own arguments and must not print argparse help.
    sub.add_parser("serve", add_help=False).add_argument("args", nargs=argparse.REMAINDER)
    for name in COMMANDS:
        if name in IMPLEMENTED:
            continue
        # Options arrive with each command's step; until then the command takes any arguments.
        sub.add_parser(name, help="not implemented yet", add_help=True).add_argument(
            "args", nargs=argparse.REMAINDER
        )
    return parser


def _server_check(args: argparse.Namespace) -> int:
    from pathlib import Path

    from halkit import proc

    from .config import SERVER_TABLE  # noqa: F401 - TTL default lives with the table

    results = servercheck.run_checks(home=Path.home(), runner=proc.run, sshd_t=args.sshd_t, fix=args.fix)
    if args.json:
        rows = [r.__dict__ for r in results]
        ok = not servercheck.has_critical_failure(results)
        sys.stdout.write(json.dumps({"ok": ok, "checks": rows}, indent=2) + "\n")
    else:
        for r in results:
            sys.stdout.write(f"{r.id} {r.severity:<8} {r.status:<7} {r.title}: {r.detail}\n")
            if r.fix:
                sys.stdout.write(f"     fix: {r.fix}\n")
    return EXIT_FAILED if servercheck.has_critical_failure(results) else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Runs `hal-remote`; returns the process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return serve.main(args.args)
    if args.command == "server-check":
        return _server_check(args)
    sys.stderr.write(f"hal-remote {args.command}: not implemented\n")
    return EXIT_USAGE


def tunnel_main() -> int:
    """Windowless entry point for the tunnel supervisor; runs `tunnel-run`."""
    return main(["tunnel-run", *sys.argv[1:]])
