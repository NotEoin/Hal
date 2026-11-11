"""Parking a GPU server: its VRAM moves to host RAM with `cuda-checkpoint`, and back.

A parked server stays alive with its model and caches, so it comes back in seconds instead of
a cold start. Design decisions, each with its reason:

- The gateway runs `cuda-checkpoint` itself. It acts on any process of the same user here:
  checked 2026-11-11 on driver 580 with `kernel.yama.ptrace_scope = 1`, by parking the
  gateway's llama-server from an unrelated shell. gpu-light's own park needs a rule and
  resumes on its own timer, which would make the alias that wants the server wait.
- The order follows gpu-light's ladder: lock every CUDA process of the server, then
  checkpoint each in ascending pid order; resume restores each, then unlocks each. A failed
  lock unlocks what was locked; any failure raises `ParkError`, and the caller stops the
  server instead, because NVIDIA says a process may be left in a bad state.
- The VRAM lands in the server's own memory, so a park is refused when it would leave less
  than `RAM_MARGIN_MIB` of available RAM.
- Measured on the RTX 3090 (2026-11-11): a 21.4 GB llama-server parked in 11.4 s and resumed
  in 5.3 s; gpu-light's notes give 9.3 s and 5.6 s for a 23.9 GiB vLLM.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from halkit import hardware, proc

log = logging.getLogger(__name__)

BINARY = "cuda-checkpoint"
LOCK_TIMEOUT_MS = 10_000  # a lock waits for running kernels; a decode step is milliseconds
ACTION_TIMEOUT_S = 120.0  # a checkpoint copies the whole VRAM share at a few GB/s
SMI_TIMEOUT_S = 10.0  # nvidia-smi can stall for seconds while a GPU wakes
RAM_MARGIN_MIB = 4096  # left for the desktop and Ollama's runner after the VRAM moves in
PROC = Path("/proc")

Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class ParkError(RuntimeError):
    """A park or resume failed; the server should be stopped instead."""


def gpu_processes(run: Runner = proc.run) -> dict[int, int]:
    """`{pid: MiB}` of every process using an NVIDIA GPU, from `nvidia-smi`; empty without one."""
    try:
        out = run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            timeout=SMI_TIMEOUT_S,
        ).stdout.decode("ascii", "replace")
    except (OSError, subprocess.SubprocessError, proc.ProcTimeout) as exc:
        log.warning("cannot list GPU processes: %s", exc)
        return {}
    apps: dict[int, int] = {}
    for line in out.splitlines():
        pid, _, mib = line.partition(",")
        if pid.strip().isdigit() and mib.strip().isdigit():
            apps[int(pid)] = apps.get(int(pid), 0) + int(mib)
    return apps


def descendants(root: int, proc_dir: Path = PROC) -> set[int]:
    """`root` and every process below it, from the parent pids in `/proc`."""
    children: dict[int, list[int]] = {}
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue  # the process exited while we looked
        fields = stat.rsplit(")", 1)[-1].split()  # the name in parentheses may hold spaces
        if len(fields) > 1 and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(entry.name))
    found, todo = {root}, [root]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


class Parker:
    """Parks and resumes the CUDA processes under one root pid."""

    def __init__(
        self,
        binary: str | None = None,
        *,
        run: Runner = proc.run,
        apps: Callable[[], dict[int, int]] | None = None,
        tree: Callable[[int], set[int]] = descendants,
        meminfo: Callable[[], tuple[int, int]] = hardware.read_meminfo,
    ) -> None:
        self.binary = binary if binary is not None else shutil.which(BINARY) or ""
        self.run, self.tree, self.meminfo = run, tree, meminfo
        self.apps = apps or (lambda: gpu_processes(run))

    @property
    def available(self) -> bool:
        """True when `cuda-checkpoint` is installed."""
        return bool(self.binary) and os.access(self.binary, os.X_OK)

    def _action(self, action: str, pid: int) -> None:
        argv = [self.binary, "--action", action, "--pid", str(pid)]
        if action == "lock":
            argv += ["--timeout", str(LOCK_TIMEOUT_MS)]
        try:
            self.run(argv, timeout=ACTION_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError, proc.ProcTimeout) as exc:
            raise ParkError(f"cuda-checkpoint {action} of {pid} failed: {exc}") from exc

    def park(self, root: int) -> tuple[int, ...]:
        """Moves the VRAM of every CUDA process under `root` to host RAM; returns their pids.

        Raises `ParkError` when nothing runs on the GPU, the RAM would run short, or a step
        fails (a failed lock is undone first).
        """
        if not self.available:
            raise ParkError("cuda-checkpoint is not installed")
        apps = self.apps()
        pids = tuple(sorted(apps.keys() & self.tree(root)))
        if not pids:
            raise ParkError(f"no GPU process under pid {root}")
        moved = sum(apps[p] for p in pids)
        _, available = self.meminfo()
        if available - moved < RAM_MARGIN_MIB:
            raise ParkError(f"parking needs {moved} MiB of RAM and {available} MiB is available")
        locked: list[int] = []
        try:
            for pid in pids:
                self._action("lock", pid)
                locked.append(pid)
        except ParkError:
            for pid in locked:
                try:
                    self._action("unlock", pid)
                except ParkError as exc:
                    log.warning("%s", exc)  # the next step stops the server anyway
            raise
        for pid in pids:
            self._action("checkpoint", pid)
        return pids

    def resume(self, pids: Iterable[int]) -> None:
        """Moves parked processes back onto the GPU; raises `ParkError` when a step fails."""
        ordered = sorted(pids)
        for pid in ordered:
            self._action("restore", pid)
        for pid in ordered:
            self._action("unlock", pid)


class Parking:
    """A backend's parked processes: empty while it runs on the GPU or when parking is off."""

    def __init__(self, enabled: bool, parker: Parker | None = None) -> None:
        self.enabled = enabled
        self.parker = parker or Parker()
        self.pids: tuple[int, ...] = ()

    @property
    def parked(self) -> bool:
        """True while the backend's VRAM sits in host RAM."""
        return bool(self.pids)

    def try_park(self, name: str, root: int) -> bool:
        """Parks the processes under `root`; False (logged) when parking is off or fails."""
        if not self.enabled:
            return False
        try:
            self.pids = self.parker.park(root)
        except ParkError as exc:
            log.warning("cannot park %s: %s; stopping it instead", name, exc)
            return False
        log.info("parked %s (pids %s) in host RAM to free the GPU", name, ", ".join(map(str, self.pids)))
        return True

    def try_resume(self, name: str) -> bool:
        """Puts a parked backend back on the GPU; False (logged) when that fails, and the
        backend must then be stopped and started again."""
        pids, self.pids = self.pids, ()
        try:
            self.parker.resume(pids)
        except ParkError as exc:
            log.warning("cannot resume %s: %s; restarting it", name, exc)
            return False
        log.info("resumed %s on the GPU", name)
        return True

    def forget(self) -> None:
        """Drops the record when the parked processes are stopped."""
        self.pids = ()
