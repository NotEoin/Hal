"""`hal scratch clean [DIR]`: empties the /scratch pad of one project, or of all of them."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from halsandbox import scratch


def main(argv: list[str]) -> int:
    """Runs `hal scratch`; returns the exit status."""
    p = argparse.ArgumentParser(prog="hal scratch")
    sub = p.add_subparsers(dest="cmd", required=True)
    clean = sub.add_parser("clean", help="delete scratchpads")
    clean.add_argument("dir", nargs="?", help="project folder; without it every scratchpad is deleted")
    opts = p.parse_args(argv)
    removed = scratch.clean(Path(opts.dir) if opts.dir else None)
    for path in removed:
        print(f"removed {path}", file=sys.stdout)
    return 0
