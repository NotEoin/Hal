"""The `hal-gateway` command line; the only module of the package that prints.

Only `check-config` exists so far. Exit codes: 0 ok, 2 bad usage, 3 invalid configuration.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from halkit import config

from .config import load_config

EXIT_OK = 0
EXIT_CONFIG = 3


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the `hal-gateway` command; returns the process exit code."""
    parser = argparse.ArgumentParser(prog="hal-gateway", description="Hal model gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-config", help="validate the [gateway] configuration and exit")
    check.add_argument("--config", type=Path, default=None, help="config file (default: the Hal config file)")
    args = parser.parse_args(argv)
    if args.command == "check-config":
        try:
            cfg = load_config(args.config)
        except config.ConfigError as exc:
            print(f"hal-gateway: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        print(f"ok: {len(cfg.backends)} backend(s), {len(cfg.aliases)} alias(es)")
    return EXIT_OK
