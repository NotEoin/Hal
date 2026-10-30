"""The tunnel supervisor: one `ssh -N` kept alive with backoff and a health probe (REQ-RMT-28 to 30).

The same code runs on every OS. It owns three things: the state machine (`starting`,
`connecting`, `up`, `backoff`, `failed`, `stopped`), the retry policy, and the status file that
`hal remote status` reads. Everything that touches the world sits behind a small interface
(`Spawner`, `Prober`, `Clock`), so the tests drive the loop with scripted fakes and never
sleep. Permanent errors (auth refused, host key mismatch, local port in use, bad config) stop the
loop with exit 3: retrying them only trips sshd's `PerSourcePenalties`.

`ssh` is a long-running child here, so it is started with `subprocess.Popen` and watched, not run
with a timeout; every wait inside the loop is bounded by the readiness and health intervals.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from halkit import files, timeutil

from . import sshconf
from .model import Profile, ProfilePaths

log = logging.getLogger(__name__)

EXIT_STOPPED = 0
EXIT_PERMANENT = 3
STATUS_SCHEMA = "hal.remote.status/1"
STDERR_TAIL_BYTES = 4096  # design 07, supervisor loop step 1
READY_POLL_S = 0.25  # design 07, step 2
READY_GRACE_S = 5  # added to connect_timeout_s: ssh opens listeners after authentication
LOOP_POLL_S = 1.0  # how often the `up` loop looks at the child and the stop request
PROBE_TIMEOUT_S = 5  # design 07, step 3
FAILURES_BEFORE_RESTART = 2  # two connection-level failures in a row, REQ-RMT-29
STOP_GRACE_S = 5  # SIGTERM to kill, design 07, step 6
JITTER = (0.8, 1.2)
STOP_FILE = "stop"  # the Windows stop request (no signals there)
PID_FILE = "supervisor.pid"
STATUS_FILE = "status.json"

PERMANENT_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"Permission denied"),
        "the key is not authorised, revoked, expired or from= does not match",
        "hal remote setup {host} --rotate",
    ),
    (
        re.compile(r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED"),
        "the desktop host key does not match the pinned one",
        "check the desktop, then hal remote setup {host} --rotate",
    ),
    (
        re.compile(r"Address already in use|cannot listen to port|Could not request local forwarding"),
        "a local port is already in use",
        "stop the program using the gateway port, then hal remote up",
    ),
    (
        re.compile(r"Bad configuration option|Load key|no such identity|bad permissions"),
        "the ssh configuration or a key file is unusable",
        "hal remote setup {host} --rotate",
    ),
)

State = Literal["starting", "connecting", "up", "backoff", "failed", "stopped"]
Mode = Literal["systemd", "detached", "foreground"]


@dataclass(frozen=True)
class ProbeResult:
    """One `GET /v1/models`: `status` is `None` for a connection-level failure."""

    status: int | None
    detail: str = ""


class Child(Protocol):
    """A running `ssh`."""

    pid: int

    def poll(self) -> int | None: ...
    def stderr_tail(self) -> str: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout_s: float) -> int | None: ...


class Spawner(Protocol):
    """Starts `ssh`."""

    def spawn(self, argv: Sequence[str], env: Mapping[str, str]) -> Child: ...


class Prober(Protocol):
    """Looks at the tunnel from the outside."""

    def models(self, port: int, token: str, timeout_s: float) -> ProbeResult: ...
    def listening(self, port: int) -> bool: ...


class Clock(Protocol):
    """Time, injectable so tests do not sleep."""

    def monotonic(self) -> float: ...
    def wait(self, stop: threading.Event, seconds: float) -> bool: ...  # True when stop was set


def next_delay(attempt: int, *, initial: float, maximum: float, rng: random.Random) -> float:
    """Backoff before retry number `attempt` (1-based): doubling from `initial`, capped, with jitter."""
    return min(maximum, initial * 2 ** (attempt - 1)) * rng.uniform(*JITTER)


def classify_exit(code: int, stderr_tail: str, lifetime_s: float) -> Literal["permanent", "transient"]:
    """Decides if an ssh exit may be retried: the stderr text decides; `code` and `lifetime_s` are logged."""
    log.info("ssh exited with %s after %.0f s", code, lifetime_s)
    return "permanent" if _permanent(stderr_tail) else "transient"


def _permanent(stderr_tail: str) -> tuple[str, str] | None:
    for pattern, reason, fix in PERMANENT_PATTERNS:
        if pattern.search(stderr_tail):
            return reason, fix
    return None


class RealClock:
    """The wall clock."""

    def monotonic(self) -> float:
        """Seconds from a monotonic clock."""
        return time.monotonic()

    def wait(self, stop: threading.Event, seconds: float) -> bool:
        """Sleeps up to `seconds` or until `stop` is set; True when it was set."""
        return stop.wait(seconds)


class _PopenChild:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self.pid = proc.pid
        self._tail: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self._proc.stderr is not None
        for chunk in iter(lambda: self._proc.stderr.read1(1024), b""):  # type: ignore[union-attr]
            with self._lock:
                self._tail.append(chunk)
                self._size += len(chunk)
                while self._size > STDERR_TAIL_BYTES and len(self._tail) > 1:
                    self._size -= len(self._tail.popleft())

    def poll(self) -> int | None:
        return self._proc.poll()

    def stderr_tail(self) -> str:
        with self._lock:
            return b"".join(self._tail)[-STDERR_TAIL_BYTES:].decode("utf-8", "replace")

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    def wait(self, timeout_s: float) -> int | None:
        try:
            return self._proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None


class SubprocessSpawner:
    """Starts the real `ssh` with a piped stderr and no stdin."""

    def spawn(self, argv: Sequence[str], env: Mapping[str, str]) -> Child:
        """Starts `argv`; no console window on Windows."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(  # noqa: S603 - argv comes from sshconf.tunnel_argv, no shell
            list(argv),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=flags,
        )
        return _PopenChild(proc)


class HttpProber:
    """Probes `127.0.0.1:<port>` with a plain TCP connect and an authenticated `GET /v1/models`."""

    def listening(self, port: int) -> bool:
        """True when something accepts on the loopback port."""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False

    def models(self, port: int, token: str, timeout_s: float) -> ProbeResult:
        """`GET /v1/models` with the bearer token; never raises."""
        req = urllib.request.Request(  # noqa: S310 - fixed http://127.0.0.1 URL
            f"http://127.0.0.1:{port}/v1/models", headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - loopback URL
                body = resp.read(1 << 20)
                try:
                    count = len(json.loads(body).get("data", []))
                    return ProbeResult(resp.status, f"{count} models")
                except (ValueError, AttributeError):
                    return ProbeResult(resp.status, "")
        except urllib.error.HTTPError as exc:
            return ProbeResult(exc.code, exc.reason if isinstance(exc.reason, str) else "")
        except (urllib.error.URLError, OSError) as exc:
            return ProbeResult(None, str(exc))


class Supervisor:
    """Keeps the tunnel of one profile up; `run` returns 0 when stopped and 3 on a permanent error."""

    def __init__(
        self,
        profile: Profile,
        paths: ProfilePaths,
        *,
        ssh: Path,
        clock: Clock,
        spawner: Spawner,
        prober: Prober,
        rng: random.Random,
        mode: Mode = "foreground",
    ) -> None:
        self.profile = profile
        self.paths = paths
        self.ssh = ssh
        self.clock = clock
        self.spawner = spawner
        self.prober = prober
        self.rng = rng
        self.mode = mode
        self._status: dict[str, object] = {}
        self._port = profile.forwards[0].local_port

    # -- status file -------------------------------------------------------------------------

    def _write(self, state: State, **changes: object) -> None:
        if not self._status:
            self._status = {
                "schema": STATUS_SCHEMA,
                "profile": self.profile.name,
                "mode": self.mode,
                "supervisor_pid": os.getpid(),
                "ssh_pid": None,
                "since": timeutil.iso_z(),
                "attempt": 0,
                "next_retry_at": None,
                "last_error": None,
                "fix": None,
                "forwards": [
                    {
                        "name": f.name,
                        "local": f"127.0.0.1:{f.local_port}",
                        "remote": f"127.0.0.1:{f.remote_port}",
                    }
                    for f in self.profile.forwards
                ],
                "gateway": None,
            }
        if self._status.get("state") != state:
            self._status["since"] = timeutil.iso_z()
        self._status["state"] = state
        self._status.update(changes)
        files.write_atomic(
            self.paths.state / STATUS_FILE, json.dumps(self._status, indent=2) + "\n", mode=0o600
        )

    # -- the loop ----------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> int:
        """Runs until `stop` is set (returns 0) or a permanent error occurs (returns 3)."""
        files.write_atomic(self.paths.state / PID_FILE, f"{os.getpid()}\n", mode=0o600)
        try:
            return self._loop(stop)
        finally:
            (self.paths.state / PID_FILE).unlink(missing_ok=True)

    def _stop_requested(self, stop: threading.Event) -> bool:
        if (self.paths.state / STOP_FILE).exists():
            stop.set()
        return stop.is_set()

    def _loop(self, stop: threading.Event) -> int:
        self._write("starting")
        bad = self._config_problem()
        if bad:
            self._write("failed", last_error=bad, fix=f"hal remote setup {self.profile.host} --rotate")
            return EXIT_PERMANENT
        attempt = 0
        settings = self.profile.tunnel
        while not self._stop_requested(stop):
            attempt += 1
            self._write("connecting", attempt=attempt, next_retry_at=None)
            env = {**os.environ, "LANG": "C", "LC_ALL": "C"}
            child = self.spawner.spawn(sshconf.tunnel_argv(self.profile, self.paths, self.ssh), env)
            started = self.clock.monotonic()
            self._write("connecting", ssh_pid=child.pid)
            if self._wait_ready(child, stop):
                self._write("up", last_error=None, fix=None)
                self._watch(child, stop)
            self._end_child(child, stop)
            if stop.is_set():
                break
            lifetime = self.clock.monotonic() - started
            code = child.poll()
            tail = child.stderr_tail()
            kind = classify_exit(-1 if code is None else code, tail, lifetime)
            if kind == "permanent":
                reason, fix = _permanent(tail) or ("", "")
                self._write(
                    "failed",
                    ssh_pid=None,
                    last_error=f"{reason}: {_last_line(tail)}",
                    fix=fix.format(host=self.profile.host),
                )
                return EXIT_PERMANENT
            if lifetime >= settings.stable_after_s:
                attempt = 1
            delay = next_delay(
                attempt, initial=settings.backoff_initial_s, maximum=settings.backoff_max_s, rng=self.rng
            )
            retry_at = timeutil.iso_z()
            self._write("backoff", ssh_pid=None, attempt=attempt, next_retry_at=retry_at,
                        last_error=_last_line(tail) or "connection lost")  # fmt: skip
            self.clock.wait(stop, delay)
        self._write("stopped", ssh_pid=None, next_retry_at=None)
        return EXIT_STOPPED

    def _config_problem(self) -> str:
        for label, path in (("ssh_config", self.paths.ssh_config), ("tunnel key", self.paths.tunnel_key)):
            if not path.is_file():
                return f"{label} is missing: {path}"
        return ""

    def _wait_ready(self, child: Child, stop: threading.Event) -> bool:
        deadline = self.clock.monotonic() + self.profile.tunnel.connect_timeout_s + READY_GRACE_S
        while self.clock.monotonic() < deadline and not self._stop_requested(stop):
            if child.poll() is not None:
                return False
            if self.prober.listening(self._port):
                return True
            self.clock.wait(stop, READY_POLL_S)
        return False

    def _watch(self, child: Child, stop: threading.Event) -> None:
        interval = self.profile.tunnel.health_interval_s
        next_probe = self.clock.monotonic() + interval
        failures = 0
        while child.poll() is None and not self._stop_requested(stop):
            if self.clock.monotonic() >= next_probe:
                next_probe += interval
                result = self._probe()
                failures = failures + 1 if result is not None and result.status is None else 0
                if failures >= FAILURES_BEFORE_RESTART:
                    log.warning("health probe failed %d times, restarting ssh", failures)
                    return
            self.clock.wait(stop, LOOP_POLL_S)

    def _probe(self) -> ProbeResult | None:
        try:
            token = self.paths.token.read_text(encoding="utf-8").strip()
        except OSError as exc:
            self._write("up", gateway={"ok": False, "checked_at": timeutil.iso_z(), "status": None,
                                       "detail": f"token file unreadable: {exc.strerror}"})  # fmt: skip
            return None
        result = self.prober.models(self._port, token, PROBE_TIMEOUT_S)
        detail = {401: "token rejected: rotate it", 502: "gateway up, model backend down",
                  503: "gateway up, model backend down"}.get(result.status or 0, result.detail)  # fmt: skip
        self._write("up", gateway={"ok": result.status == 200, "checked_at": timeutil.iso_z(),
                                   "status": result.status, "detail": detail})  # fmt: skip
        return result

    def _end_child(self, child: Child, stop: threading.Event) -> None:
        """Makes sure `ssh` is gone: terminate, wait the grace period, then kill."""
        if child.poll() is None:
            child.terminate()
            if child.wait(STOP_GRACE_S) is None:
                child.kill()
                child.wait(STOP_GRACE_S)


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""
