"""One-shot v1 tool program: `tool.py FUNCTION` with JSON arguments on stdin."""

from __future__ import annotations

import json
import subprocess
import sys
import time

CHILD_SLEEP_S = "30"  # far longer than any test timeout, so only a kill ends it


def main() -> int:
    """Runs the function named by the last argument."""
    function = sys.argv[-1]
    args = json.loads(sys.stdin.read() or "{}")
    if function == "add":
        sys.stdout.write(json.dumps({"sum": args["a"] + args["b"], "cwd_ok": True}))
    elif function == "say":
        sys.stdout.write("plain words, not JSON")
    elif function == "fail":
        sys.stderr.write("first line\nthe calendar file is locked\n")
        return 2
    elif function == "sleep":
        child = subprocess.Popen(["sleep", CHILD_SLEEP_S])
        with open(args["pidfile"], "w", encoding="ascii") as handle:
            handle.write(str(child.pid))
        time.sleep(30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
