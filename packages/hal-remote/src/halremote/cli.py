"""The `hal-remote` command line; the only module of the package that prints.

Every subcommand of the design (07-remote.md, CLI synopsis) is registered; until its
implementation step lands it exits with `EXIT_USAGE` and says so, so a half-built install fails
loudly instead of doing nothing. Exit codes: 0 ok; 1 check or operation failed; 2 usage, config
or not-implemented; 3 not set up; 4 tunnel not up; 64 and 75 are reserved for `serve`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from . import config as _config  # noqa: F401 - import registers the [remote] tables

EXIT_OK = 0
EXIT_USAGE = 2

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
    for name in COMMANDS:
        # Options arrive with each command's step; until then the command takes any arguments.
        sub.add_parser(name, help="not implemented yet", add_help=True).add_argument(
            "args", nargs=argparse.REMAINDER
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Runs `hal-remote`; returns the process exit code."""
    args = build_parser().parse_args(argv)
    sys.stderr.write(f"hal-remote {args.command}: not implemented\n")
    return EXIT_USAGE


def tunnel_main() -> int:
    """Windowless entry point for the tunnel supervisor; runs `tunnel-run`."""
    return main(["tunnel-run", *sys.argv[1:]])
