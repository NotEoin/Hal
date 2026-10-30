"""Measures box startup time under runc and runsc with the real docker daemon.

Usage: python3 scripts/bench-sandbox.py [N] [CLI]

Runs `CLI --version` (default `crush`) N times (default 5) through `halsandbox.box.run` for each
installed runtime, in a throwaway project and state dir, and prints the median and the slowest run
in seconds. Needs `hal-sandbox image build` and `hal-sandbox image build CLI`. Record the numbers in
docs/dev/sandbox.md. No model is loaded.
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_RUNS = 5
DEFAULT_CLI = "crush"
RUNTIMES = ("runc", "runsc")


class _NoModel:
    """A `start_model` result that does nothing, since `--version` never calls the model."""

    def close(self) -> None:
        """Nothing to stop."""


def main(argv: list[str]) -> int:
    """Prints one line per runtime; returns 1 when no runtime could be measured."""
    runs = int(argv[0]) if argv else DEFAULT_RUNS
    cli = argv[1] if len(argv) > 1 else DEFAULT_CLI
    from halsandbox.box import run
    from halsandbox.code import docker_runtimes

    measured = 0
    with tempfile.TemporaryDirectory(prefix="hal-bench-") as tmp:
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        runtime_dir = Path(tmp) / "run"
        runtime_dir.mkdir(mode=0o700)  # the live runtime dir must stay untouched
        os.environ["HAL_RUNTIME_DIR"] = str(runtime_dir)
        project = Path(tmp) / "proj"
        project.mkdir()
        (project / "a.txt").write_text("a\n")
        for runtime in RUNTIMES:
            if runtime not in docker_runtimes():
                sys.stdout.write(f"{runtime}: not installed" + "\n")
                continue
            times = []
            for _ in range(runs):
                t0 = time.perf_counter()
                status = run(cli, ["--version"], project=project, runtime=runtime,
                             start_model=lambda _s, _i: _NoModel())  # fmt: skip
                times.append(time.perf_counter() - t0)
                if status != 0:
                    sys.stderr.write(f"{runtime}: {cli} exited {status}\n")
                    return 1
            measured += 1
            sys.stdout.write(
                f"{runtime}: median {statistics.median(times):.2f} s, max {max(times):.2f} s, n={runs}" + "\n"
            )
    return 0 if measured else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
