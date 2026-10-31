"""Windows tunnel control: a detached windowless supervisor and an optional logon task (design 07, Windows).

Windows has no signals, so `down` writes the stop file, waits briefly so the supervisor can
record `stopped`, and only then ends the process tree. Before ending anything it checks that
the recorded pid still belongs to a Hal image, so a recycled pid is never killed. The module
imports on every OS; the Windows-only calls are injected, which is how the tests run them.
[unverified on a Windows machine: schtasks without administrator rights, see manual check M4]
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from halkit import files, proc, timeutil

from .model import ProfilePaths
from .supervisor import STOP_FILE

log = logging.getLogger(__name__)

PID_RECORD = "supervisor.pid.json"
TASKLIST_TIMEOUT_S = 10.0
TASKKILL_TIMEOUT_S = 15.0
SCHTASKS_TIMEOUT_S = 30.0
STOP_WAIT_S = 3.0  # design 07: lets the supervisor record `stopped` before the tree is killed
GUI_IMAGE = "hal-remote-tunnelw.exe"
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

Runner = Callable[..., subprocess.CompletedProcess[bytes]]
Spawn = Callable[[list[str]], int]


class WindowsControlError(RuntimeError):
    """Starting or stopping the tunnel failed; the message says what to do."""


def task_name(profile: str) -> str:
    """The scheduled task name `Hal\\remote-<profile>`."""
    return f"Hal\\remote-{profile}"


def startup_file(profile: str) -> Path:
    """The Startup-folder fallback `.cmd` of the logon task."""
    base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    folder = base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return folder / f"Hal remote tunnel ({profile}).cmd"


def _popen_detached(argv: list[str]) -> int:
    flags = getattr(subprocess, "DETACHED_PROCESS", DETACHED_PROCESS) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", CREATE_NEW_PROCESS_GROUP
    )
    child = subprocess.Popen(  # noqa: S603 - argv is the hal-remote GUI executable and fixed words
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    return child.pid


def image_name(pid: int, runner: Runner) -> str | None:
    """The image name of `pid` from `tasklist`, or `None` when no such process exists."""
    argv = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
    try:
        done = runner(argv, timeout=TASKLIST_TIMEOUT_S, check=False)
    except (OSError, proc.ProcTimeout):
        return None
    rows = list(csv.reader(io.StringIO(done.stdout.decode("utf-8", "replace"))))
    return rows[0][0] if rows and len(rows[0]) >= 2 and rows[0][1] == str(pid) else None


def _is_hal_image(name: str | None) -> bool:
    lowered = (name or "").lower()
    return lowered == GUI_IMAGE or (lowered.startswith("python") and lowered.endswith(".exe"))


def _read_pid(record: Path) -> int | None:
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("pid") if isinstance(data, dict) and isinstance(data.get("pid"), int) else None


def up(
    profile: str,
    pp: ProfilePaths,
    gui_exe: Path,
    *,
    install_task: bool = False,
    runner: Runner = proc.run,
    spawn: Spawn = _popen_detached,
) -> str:
    """Starts the supervisor of `profile` detached; with `install_task` also at logon."""
    record = pp.state / PID_RECORD
    pid = _read_pid(record)
    if pid is not None and _is_hal_image(image_name(pid, runner)):
        raise WindowsControlError(f"the tunnel for {profile} is already running; hal remote down first")
    (pp.state / STOP_FILE).unlink(missing_ok=True)  # a stale request would stop the new supervisor at once
    pid = spawn([str(gui_exe), "tunnel-run", "--profile", profile, "--mode", "detached"])
    body = {"pid": pid, "mode": "detached", "time": timeutil.iso_z()}
    files.write_atomic(record, json.dumps(body) + "\n", mode=0o600)
    if install_task:
        install_logon(profile, gui_exe, runner)
    return "detached"


def install_logon(profile: str, exe: Path, runner: Runner = proc.run) -> str:
    """Creates the logon task, else the Startup `.cmd`; returns `task` or `startup`."""
    command = f'\\"{exe}\\" tunnel-run --profile {profile}'
    argv = [
        "schtasks",
        "/Create",
        "/F",
        "/TN",
        task_name(profile),
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/TR",
        command,
    ]
    try:
        done = runner(argv, timeout=SCHTASKS_TIMEOUT_S, check=False)
        if done.returncode == 0:
            return "task"
        log.warning(
            "schtasks failed (%s); using the Startup folder", done.stderr.decode("utf-8", "replace")[-200:]
        )
    except (OSError, proc.ProcTimeout) as exc:
        log.warning("schtasks unavailable (%s); using the Startup folder", exc)
    files.write_atomic(
        startup_file(profile), f'@start "" "{exe}" tunnel-run --profile {profile}\r\n', mode=0o644
    )
    return "startup"


def down(
    profile: str,
    pp: ProfilePaths,
    *,
    disable: bool = False,
    runner: Runner = proc.run,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Stops the tunnel of `profile`; with `disable` removes the logon task and Startup file."""
    record = pp.state / PID_RECORD
    pid = _read_pid(record)
    result = "not running"
    if pid is not None and _is_hal_image(image_name(pid, runner)):
        files.write_atomic(pp.state / STOP_FILE, timeutil.iso_z() + "\n", mode=0o600)
        sleep(STOP_WAIT_S)
        if image_name(pid, runner) is not None:
            try:
                runner(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=TASKKILL_TIMEOUT_S, check=True)
            except (OSError, proc.ProcTimeout, subprocess.CalledProcessError) as exc:
                raise WindowsControlError(f"taskkill failed for pid {pid}: {exc}") from None
        result = "detached"
    record.unlink(missing_ok=True)
    (pp.state / STOP_FILE).unlink(missing_ok=True)
    if disable:
        runner(
            ["schtasks", "/Delete", "/F", "/TN", task_name(profile)], timeout=SCHTASKS_TIMEOUT_S, check=False
        )
        startup_file(profile).unlink(missing_ok=True)
    return result
