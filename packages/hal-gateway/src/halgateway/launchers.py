"""Process launcher for the managed llama-server and managed servers (REQ-GW-35, 37, 92).

Design decisions:

- A pid alone is not an identity: pids are reused. Every record keeps `pid_start` (field 22 of
  `/proc/<pid>/stat`), and a signal is sent only when the live process still has that start time.
- The child runs in its own session with its output in a log file, so it survives the gateway
  and can be adopted by the next one (REQ-GW-36).
- A zombie counts as dead: the gateway is the parent of a router it started, and `poll` reaps it.
- A stop signals the whole process group and waits until it is empty: vLLM's `EngineCore` is a
  child of the API server and holds the VRAM, so a stop that ends only the leader leaves the card
  full. The child is a session leader, so its group id is its pid; a group that still holds a
  process with that pid but another start time is someone else's and is never signalled (a pid
  cannot be reused while a group of that id has members, so the check has no race).
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


class LaunchError(RuntimeError):
    """A managed server could not be started."""


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


def group_members(pgid: int) -> list[int]:
    """Pids of the live (not zombie) processes in process group `pgid`."""
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue  # the process exited between the listing and the read: not a member
        fields = stat.rsplit(")", 1)[-1].split()
        # after the command name: state, ppid, pgrp (proc(5))
        if len(fields) > 2 and fields[0] != "Z" and fields[2] == str(pgid):
            members.append(int(entry.name))
    return members


def comm(pid: int) -> str:
    """The command name of `pid`, or an empty string."""
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return ""


class ProcessLauncher:
    """Starts and stops a server as a detached process in its own session."""

    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    def start(
        self, argv: Sequence[str], env: Mapping[str, str], log_path: Path, cwd: Path | None = None
    ) -> tuple[int, int]:
        """Spawns `argv` with exactly `env` in `cwd`; returns `(pid, pid_start)`; raises `LaunchError`."""
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv comes from llama_server_command or the owner's config
                list(argv),
                env=dict(env),
                cwd=cwd,
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

    def _group(self, pid: int, started: int) -> list[int]:
        """Members of the group led by our child `pid`; empty when the group is gone or foreign."""
        leader = self.alive(pid, started)
        members = group_members(pid)
        if pid in members and not leader:
            return []  # the pid was reused by another group leader
        if leader and pid not in members:
            return [pid]  # not a group leader (started by an older gateway); signal it alone
        return members

    def _signal(self, pid: int, started: int, sig: signal.Signals) -> None:
        members = self._group(pid, started)
        if not members:
            return
        try:
            if members == [pid] and os.getpgid(pid) != pid:
                os.kill(pid, sig)
            else:
                os.killpg(pid, sig)
        except ProcessLookupError:
            log.debug("process group %d ended before signal %s", pid, sig.name)

    def _wait_gone(self, pid: int, started: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while self._group(pid, started):
            if time.monotonic() >= deadline:
                return False
            time.sleep(STOP_POLL_S)
        return True

    def stop(self, pid: int, started: int, timeout_s: float) -> None:
        """SIGTERM to the process group, wait `timeout_s` for all of it, then SIGKILL and wait again.

        Signals only a group whose leader still has start time `started`, or whose leader has
        exited and left children behind (the pid cannot have been reused while they live).
        """
        if not self._group(pid, started):
            return
        self._signal(pid, started, signal.SIGTERM)
        if self._wait_gone(pid, started, timeout_s):
            return
        log.warning("process group %d ignored SIGTERM for %.0f s; sending SIGKILL", pid, timeout_s)
        self._signal(pid, started, signal.SIGKILL)
        if not self._wait_gone(pid, started, timeout_s):
            log.error("process group %d still has members after SIGKILL", pid)
