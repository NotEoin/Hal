"""Linux tunnel control: a systemd user unit when usable, else a detached supervisor (design 07, Linux).

`up` and `down` are the only entry points. Every `systemctl` call has a timeout, and the
detached fallback checks the recorded pid against `/proc/<pid>/cmdline` and the process start
time before it signals anything, so a recycled pid is never killed.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

from halkit import files, proc, timeutil

from .model import ProfilePaths

log = logging.getLogger(__name__)

UNIT_TEMPLATE = "hal-remote-tunnel@.service"
SYSTEMCTL_TIMEOUT_S = 30.0  # design 07, Linux: each systemctl call
DETECT_TIMEOUT_S = 5.0
USABLE_STATES = frozenset({"running", "degraded"})
PID_RECORD = "supervisor.pid.json"
PERMANENT_EXIT = 3  # the supervisor's exit status for an error that retrying cannot fix
PROC_STAT_START_FIELD = 21  # field 22 of /proc/<pid>/stat, counted after the ") " that ends comm

Runner = Callable[..., subprocess.CompletedProcess[bytes]]
Spawn = Callable[[list[str]], int]


class TunnelControlError(RuntimeError):
    """Starting or stopping the tunnel failed; the message says what to do."""


def unit_dir() -> Path:
    """`$XDG_CONFIG_HOME/systemd/user`, defaulting to `~/.config/systemd/user`."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def render_unit(exe: Path) -> str:
    """The text of the template unit; `exe` is the absolute `hal-remote` path."""
    return (
        "[Unit]\n"
        "Description=Hal SSH tunnel (profile %i)\n"
        "StartLimitIntervalSec=0\n\n"
        "[Service]\n"
        "Type=exec\n"
        f"ExecStart={exe.as_posix()} tunnel-run --profile %i\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"RestartPreventExitStatus={PERMANENT_EXIT}\n"
        "NoNewPrivileges=yes\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def systemd_usable(runner: Runner = proc.run) -> bool:
    """True when the user manager reports `running` or `degraded`."""
    try:
        done = runner(["systemctl", "--user", "is-system-running"], timeout=DETECT_TIMEOUT_S, check=False)
    except (OSError, proc.ProcTimeout):
        return False
    return done.stdout.decode("utf-8", "replace").strip() in USABLE_STATES


def _systemctl(runner: Runner, *args: str) -> None:
    try:
        done = runner(["systemctl", "--user", *args], timeout=SYSTEMCTL_TIMEOUT_S, check=False)
    except (OSError, proc.ProcTimeout) as exc:
        raise TunnelControlError(f"systemctl {args[0]} failed: {exc}") from None
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip()[-300:]
        raise TunnelControlError(f"systemctl {' '.join(args)} failed: {tail or done.returncode}")


def install_unit(exe: Path, runner: Runner) -> bool:
    """Writes the template unit when it changed and reloads systemd; True when it wrote."""
    target = unit_dir() / UNIT_TEMPLATE
    text = render_unit(exe)
    if target.exists() and target.read_text(encoding="utf-8") == text:
        return False
    files.write_atomic(target, text, mode=0o644)
    _systemctl(runner, "daemon-reload")
    return True


def process_start_time(pid: int, proc_root: Path = Path("/proc")) -> str | None:
    """The start time ticks of `pid` from `/proc/<pid>/stat`, or `None` when it is gone."""
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    rest = stat.rpartition(") ")[2].split()  # comm may hold spaces and parentheses
    return rest[PROC_STAT_START_FIELD - 2] if len(rest) >= PROC_STAT_START_FIELD - 1 else None


def _cmdline(pid: int, proc_root: Path) -> str:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _popen_detached(argv: list[str]) -> int:
    child = subprocess.Popen(  # noqa: S603 - argv is the hal-remote executable and fixed words
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return child.pid


def up(
    profile: str,
    pp: ProfilePaths,
    exe: Path,
    *,
    enable: bool = False,
    runner: Runner = proc.run,
    spawn: Spawn = _popen_detached,
    proc_root: Path = Path("/proc"),
) -> str:
    """Starts the tunnel of `profile`; returns the mode used, `systemd` or `detached`."""
    if systemd_usable(runner):
        install_unit(exe, runner)
        unit = f"hal-remote-tunnel@{profile}.service"
        _systemctl(runner, "start", unit)
        if enable:
            _systemctl(runner, "enable", unit)
        return "systemd"
    if enable:
        raise TunnelControlError("--enable needs a working systemd user manager; none was found")
    record = pp.state / PID_RECORD
    if _recorded_process_alive(record, proc_root):
        raise TunnelControlError(f"the tunnel for {profile} is already running; hal remote down first")
    pid = spawn([str(exe), "tunnel-run", "--profile", profile, "--mode", "detached"])
    started = process_start_time(pid, proc_root) or ""
    body = {"pid": pid, "started": started, "mode": "detached", "time": timeutil.iso_z()}
    files.write_atomic(record, json.dumps(body) + "\n", mode=0o600)
    return "detached"


def _read_record(record: Path) -> dict | None:
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None


def _recorded_process_alive(record: Path, proc_root: Path) -> bool:
    data = _read_record(record)
    if data is None:
        return False
    pid = data["pid"]
    return "tunnel-run" in _cmdline(pid, proc_root) and process_start_time(pid, proc_root) == data.get(
        "started"
    )


def down(
    profile: str,
    pp: ProfilePaths,
    *,
    disable: bool = False,
    runner: Runner = proc.run,
    kill: Callable[[int, int], None] = os.kill,
    proc_root: Path = Path("/proc"),
) -> str:
    """Stops the tunnel of `profile`; returns the mode stopped, or `not running`."""
    record = pp.state / PID_RECORD
    if _recorded_process_alive(record, proc_root):
        data = _read_record(record) or {}
        kill(int(data["pid"]), signal.SIGTERM)
        record.unlink(missing_ok=True)
        return "detached"
    record.unlink(missing_ok=True)  # stale: the process is gone or the pid was reused
    if not systemd_usable(runner):
        return "not running"
    unit = f"hal-remote-tunnel@{profile}.service"
    _systemctl(runner, "stop", unit)
    if disable:
        _systemctl(runner, "disable", unit)
    return "systemd"
