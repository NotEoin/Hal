"""Process launcher for the managed llama-server (REQ-GW-35, REQ-GW-37).

Design decisions:

- A pid alone is not an identity: pids are reused. Every record keeps `pid_start` (field 22 of
  `/proc/<pid>/stat`), and a signal is sent only when the live process still has that start time.
- The child runs in its own session with its output in a log file, so it survives the gateway
  and can be adopted by the next one (REQ-GW-36).
- A zombie counts as dead: the gateway is the parent of a router it started, and `poll` reaps it.
- The docker launcher is a later plan step (30); `launcher = "docker"` is refused until then.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

STOP_POLL_S = 0.1
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
LEGACY_COMM = "llama-server"  # `/proc/<pid>/comm` of the legacy router


class LaunchError(RuntimeError):
    """llama-server could not be started."""


def pid_start(pid: int) -> int | None:
    """Start time (clock ticks since boot) of `pid`, or None when it does not exist."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    # the command name is in parentheses and may hold spaces, so split after the last one
    fields = stat.rsplit(")", 1)[-1].split()
    return int(fields[19]) if len(fields) > 19 else None


def boot_id() -> str:
    """The kernel boot id; a record from another boot never names a live process."""
    try:
        return BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def is_alive(pid: int, started: int | None = None) -> bool:
    """True when `pid` runs (not a zombie) and, if given, still has start time `started`."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    fields = stat.rsplit(")", 1)[-1].split()
    if len(fields) < 20 or fields[0] == "Z":
        return False
    return started is None or int(fields[19]) == started


def comm(pid: int) -> str:
    """The command name of `pid`, or an empty string."""
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return ""


def legacy_router_pid(pid_file: Path) -> int | None:
    """The pid in the legacy `hal-serve` pid file when it names a live `llama-server`, else None."""
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return pid if is_alive(pid) and comm(pid).startswith(LEGACY_COMM) else None


class ProcessLauncher:
    """Starts and stops llama-server as a detached process."""

    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    def start(self, argv: Sequence[str], env: Mapping[str, str], log_path: Path) -> tuple[int, int]:
        """Spawns `argv` with exactly `env`; returns `(pid, pid_start)`; raises `LaunchError`."""
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is built by llama_server_command
                list(argv),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=fd,
                start_new_session=True,
            )
        except OSError as exc:
            raise LaunchError(f"cannot start {argv[0]}: {exc}") from exc
        finally:
            os.close(fd)
        started = pid_start(proc.pid)
        if started is None:
            raise LaunchError(f"{argv[0]} exited at once; see {log_path}")
        self._children[proc.pid] = proc
        return proc.pid, started

    def alive(self, pid: int, started: int) -> bool:
        """True while the process runs; reaps a child that exited."""
        child = self._children.get(pid)
        if child is not None and child.poll() is not None:
            del self._children[pid]
        return is_alive(pid, started)

    def stop(self, pid: int, started: int, timeout_s: float) -> None:
        """SIGTERM, wait `timeout_s`, then SIGKILL; signals only a pid whose start time matches."""
        if not self.alive(pid, started):
            return
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.alive(pid, started):
                return
            time.sleep(STOP_POLL_S)
        if self.alive(pid, started):
            log.warning("llama-server %d ignored SIGTERM for %.0f s; sending SIGKILL", pid, timeout_s)
            os.kill(pid, signal.SIGKILL)
            kill_deadline = time.monotonic() + timeout_s
            while self.alive(pid, started) and time.monotonic() < kill_deadline:
                time.sleep(STOP_POLL_S)
