"""`hal kill`: stops everything Hal runs, whoever started it.

Order: mark Hal off and free the GPU, stop the systemd units hal installed, stop the model server,
then signal every process that registered a pid file (`pids.py`) and remove the box containers they
leave behind. Pid files replace the old `pkill -f` on a command-line pattern, which could hit an
unrelated process whose arguments looked alike. Exit status is 1 when something could not be stopped.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import urllib.error
from collections.abc import Callable
from pathlib import Path

from . import pids, power, units

log = logging.getLogger(__name__)

UNIT_GLOBS = ("hal.target", "hal-*.service", "hal-*.timer")  # the old script's stop list
SERVE_STOP_TIMEOUT_S = 120.0  # the llama.cpp router unloads its models before it exits


def stop_units() -> list[str]:
    """Stops hal.target, every `hal-*` service and timer and each recorded unit; returns the names asked."""
    names = [*UNIT_GLOBS, *sorted(units.installed())]
    result = units.systemctl("stop", *names)
    if result.returncode:
        log.warning("systemctl stop: %s", (result.stderr or "").strip()[:200])
    return names


def stop_model_server() -> None:
    """Runs `hal-serve stop` (max profile) when the launcher says where it lives."""
    root = os.environ.get("HAL_ROOT")
    script = Path(root) / "hal-serve" if root else None
    if script is None or not script.is_file():
        return
    try:
        subprocess.run([str(script), "stop"], capture_output=True, timeout=SERVE_STOP_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("hal-serve stop failed: %s", exc)


def kill_all(*, quiet: bool = False, sweep: Callable[[], object] | None = None) -> tuple[int, list[str]]:
    """Stops everything; returns (processes signalled, descriptions of what survived)."""
    try:
        power.off(quiet=True)
    except (OSError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        log.warning("hal off failed, continuing: %s", exc)
    stop_units()
    stop_model_server()
    records = pids.live()
    left = [f"{r.name} (pid {r.pid})" for r in pids.terminate(records)]
    try:
        (sweep or _sweep)()
    except Exception as exc:  # noqa: BLE001 - a failed container sweep must not hide the processes already stopped
        log.warning("container sweep failed: %s", exc)
    return len(records), left


def _sweep() -> object:
    from halsandbox import labels

    return labels.sweep()


def main(argv: list[str]) -> int:
    """Runs `hal kill [--quiet]`; returns the exit status."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser(prog="hal kill", description="Stop every Hal process, unit and box.")
    ap.add_argument("--quiet", action="store_true", help="no desktop notification")
    quiet = ap.parse_args(argv).quiet  # an unknown flag or --help must never reach the kill below
    stopped, left = kill_all(quiet=quiet)
    if left:
        print("Hal is still running: " + ", ".join(left), file=sys.stderr)
        power._notify("Hal is still running", "Could not stop: " + ", ".join(left), quiet)
        return 1
    msg = f"Hal is stopped ({stopped} processes ended); `hal on` starts it again."
    print(msg)
    power._notify("Hal is stopped", msg, quiet)
    return 0
