"""Run a subprocess with a mandatory timeout, no shell, and cleanup of the whole process tree.

Every Hal subprocess call goes through `run` (REQ-SYS-15), so a hung child can never hang Hal.
The child starts in its own session; on timeout the whole process group is killed, so shell
wrappers and their children do not outlive the call.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

REAP_TIMEOUT_S = 5.0  # time allowed to collect a killed child's exit status


class ProcTimeout(Exception):
    """The command did not finish in time and its process group was killed."""

    def __init__(self, argv: Sequence[str], elapsed: float) -> None:
        self.argv = list(argv)
        self.elapsed = elapsed
        super().__init__(f"command timed out after {elapsed:.1f} s: {self.argv}")


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        proc.kill()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def run(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,  # same name as subprocess.run, so callers can swap it in
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Runs `argv` without a shell and returns the completed process (stdout, stderr as bytes).

    `timeout` is required so every call site states one. Raises `ProcTimeout` after killing the
    process group, `subprocess.CalledProcessError` when `check` and the exit status is not 0,
    `TypeError` for a string argv (a string invites shell use), `ValueError` for an empty one.
    """
    if isinstance(argv, str | bytes):
        raise TypeError("argv must be a list of arguments, not a string; there is no shell")
    args = [os.fspath(a) for a in argv]
    if not args:
        raise ValueError("argv is empty")
    started = time.monotonic()
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=REAP_TIMEOUT_S)
        raise ProcTimeout(args, time.monotonic() - started) from None
    except BaseException:
        _kill_group(proc)
        proc.wait()
        raise
    result = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result
