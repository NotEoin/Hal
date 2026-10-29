"""Run tools outside the hub process: warm worker pools with enforced timeouts.

04-hub.md, "Runner". Each module gets a pool of up to `max_workers` warm worker processes
(`_worker/hal_worker.py`, or the module's own `exec` program for `protocol = "jsonl"`), each
running one call at a time. Design decisions:

- Workers are per module so packs are isolated from the hub and from each other, and warm so
  a call does not pay an interpreter start (D5).
- Every worker leads its own process group; a timeout or a cancel kills the whole group
  (SIGTERM, then SIGKILL after `kill_grace`), so a tool's children die with it (REQ-HUB-25,
  REQ-HUB-32). A call is never retried: it may already have had effects (REQ-HUB-28).
- The worker environment is built from an allowlist, never inherited, so a gateway token or
  cloud key in the hub's environment never reaches tool code (REQ-HUB-27).
- A worker that fails to start makes its module fail fast for `FAILED_START_BACKOFF_S`, so a
  broken module cannot cause a spawn storm (REQ-HUB-29). Backoff and idle bookkeeping use the
  injectable `clock`; waits on pipes use real time.
- A protocol line over `MAX_MESSAGE_BYTES` kills the worker (`output_too_large`); results are
  cut to the tool's `max_output` by `cut_output`, which the call pipeline applies.
- Secrets travel only inside the `call` message to a worker of the declaring module, and every
  injected value is replaced with `[redacted]` in the result, the error message and the
  worker's logged stderr (REQ-HUB-69, 01 REQ-SEC-50, REQ-SEC-51).
- Workers are pooled per `(module, network)`. When bubblewrap works, workers for tools with
  `network = false` run without a network (REQ-HUB-70, D23); the drop is defence in depth, so
  without bubblewrap they run as before and `doctor` says so. bwrap gets `--unshare-pid` on
  top of the spec's flags: `--new-session` puts the tool in its own session, out of reach of
  the process-group kill, and a pid namespace makes killing bwrap end every process inside
  [verified 2026-10-29: a grandchild `sleep` survived `killpg` without it and died with it].
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import itertools
import json
import logging
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from halkit import config as halconfig
from halkit import paths, proc
from halkit.secrets import UnsafeSecretError, read_secret

from .manifest import ModuleSpec, ToolSpec

log = logging.getLogger(__name__)

WORKER_SCRIPT = Path(__file__).resolve().parent / "_worker" / "hal_worker.py"
WORKER_FLAGS = ("-P", "-X", "utf8")  # -P: nothing beside the script can shadow an import (D6)
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 8 * 2**20  # bounds hub memory per call (04-hub "Worker protocol")
TICK_S = 0.2  # how often a waiting call checks its deadline and cancel event
FAILED_START_BACKOFF_S = 30.0  # REQ-HUB-29
STDERR_TAIL_LINES = 50  # kept for `doctor` and crash messages
CRASH_LINE_CHARS = 200  # the last stderr line quoted in a `worker_crashed` message
REAP_TIMEOUT_S = 5.0  # time allowed to collect a killed worker's exit status
REAPER_INTERVAL_S = 30.0  # idle workers are checked this often; idle_timeout is minutes
REDACTED = "[redacted]"  # 01-security's replacement text for a secret value
BWRAP = "bwrap"
BWRAP_COMMON = ("--dev-bind", "/", "/", "--unshare-pid", "--die-with-parent", "--new-session")
BWRAP_OFFLINE = ("--unshare-net",)
BWRAP_PROBE_TIMEOUT_S = 5.0  # 04-hub "Network drop with bubblewrap"

ENV_ALLOWLIST = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY", "WAYLAND_DISPLAY", "PULSE_SERVER", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
        "HAL_CONFIG", "HAL_POLICY", "HAL_STATE_DIR", "HAL_DATA_DIR", "HAL_RUNTIME_DIR", "HAL_LOG_LEVEL",
    }
)  # fmt: skip
ENV_PREFIXES = ("LC_", "XDG_")

ERROR_TYPES = {
    "ArgumentsMismatch": "manifest_mismatch",
    "UnserialisableResult": "tool_error",
    "ProtocolError": "protocol_error",
}


@dataclass(frozen=True)
class RunnerConfig:
    """`[hub.runner]` settings (04-hub "Configuration" gives each default's reason)."""

    max_workers_per_module: int = 2
    idle_timeout: float = 600.0
    startup_timeout: float = 20.0
    queue_timeout: float = 30.0
    kill_grace: float = 2.0
    max_calls_per_worker: int = 500
    max_timeout: float = 7200.0
    max_result_chars: int = 20_000
    inprocess_modules: frozenset[str] = frozenset()
    bwrap: Literal["auto", "off"] = "auto"


@dataclass(frozen=True)
class RunOutcome:
    """What one run produced: a result, or an error type and message (never a traceback)."""

    ok: bool
    result: object = None
    error_type: str | None = None
    message: str | None = None
    worker_pid: int | None = None
    seconds: float = 0.0


@dataclass(frozen=True)
class NestedCall:
    """A `tool_call` a worker sent while running `parent_tool`."""

    module: ModuleSpec
    parent_tool: ToolSpec
    parent_id: str
    tool: str
    args: Mapping[str, object]


@dataclass(frozen=True)
class WorkerInfo:
    """A worker's `ready` message and recent stderr, for `doctor`."""

    pid: int
    python: str
    module: str
    functions: Mapping[str, object]
    missing: tuple[str, ...]
    stderr_tail: tuple[str, ...] = ()


class WorkerError(Exception):
    """A worker could not be started or broke the protocol; `error_type` is the caller's."""

    def __init__(self, error_type: str, message: str) -> None:
        self.error_type = error_type
        self.message = message
        super().__init__(f"{error_type}: {message}")


class SecretError(Exception):
    """A declared secret cannot be read; the message names the secret and the fix."""


def resolve_secrets(module: ModuleSpec, *, config_path: Path | None = None) -> dict[str, str]:
    """Reads every secret `module` declares: the config value at `config_key`, else `default_path`.

    Raises `SecretError` when any one is missing, empty or loosely permissioned, so no worker
    ever sees a partial set (04-hub failure table).
    """
    if not module.secrets:
        return {}
    data = halconfig.read_toml(config_path or paths.config_file())
    values = {}
    for spec in module.secrets:
        configured = _dotted(data, spec.config_key)
        if configured is not None and not isinstance(configured, str):
            raise SecretError(
                f"secret {spec.name} of {module.name}: {spec.config_key} must be a file path string"
            )
        path = Path(configured or spec.default_path).expanduser()
        try:
            values[spec.name] = read_secret(path)
        except FileNotFoundError:
            raise SecretError(
                f"secret {spec.name} of {module.name} is not set: create {path} (mode 0600) "
                f"or set {spec.config_key} in config.toml"
            ) from None
        except (UnsafeSecretError, ValueError, OSError) as exc:
            raise SecretError(f"secret {spec.name} of {module.name}: {exc}") from None
    return values


def _dotted(data: Mapping[str, object], key: str) -> object:
    node: object = data
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def scrub(value: object, secrets: Mapping[str, str] | set[str] | frozenset[str]) -> object:
    """`value` with every exact secret value replaced by `[redacted]`, in strings at any depth."""
    needles = sorted(
        {v for v in (secrets.values() if isinstance(secrets, Mapping) else secrets) if v}, key=len
    )
    if not needles:
        return value
    return _scrub(value, needles[::-1])


def _scrub(value: object, needles: list[str]) -> object:
    if isinstance(value, str):
        for needle in needles:
            value = value.replace(needle, REDACTED)
        return value
    if isinstance(value, list):
        return [_scrub(item, needles) for item in value]
    if isinstance(value, dict):
        return {_scrub(k, needles): _scrub(v, needles) for k, v in value.items()}
    return value


def bwrap_available() -> bool:
    """True when bubblewrap can start an unprivileged sandbox with no network here."""
    try:
        result = proc.run(
            [BWRAP, *BWRAP_COMMON, *BWRAP_OFFLINE, "--", "true"], timeout=BWRAP_PROBE_TIMEOUT_S, check=False
        )
    except (OSError, proc.ProcTimeout):
        return False
    return result.returncode == 0


def cut_output(text: str, limit: int) -> str:
    """`text` cut to head and tail around a marker giving the characters removed (REQ-HUB-33)."""
    if len(text) <= limit:
        return text
    removed = len(text) - limit
    marker = f"\n[... {removed} characters cut ...]\n"
    head = (limit + 1) // 2
    tail = limit - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def worker_environment(module: ModuleSpec, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The worker's environment: allowlisted names, the module's `env` names, and the hub's own."""
    source = os.environ if environ is None else environ
    env = {
        name: value
        for name, value in source.items()
        if name in ENV_ALLOWLIST or name.startswith(ENV_PREFIXES) or name in module.env
    }
    env.update(
        {
            "HAL_MODULE": module.name,
            "HAL_MODULE_ROOT": str(module.root),
            "HAL_MODULE_STATE": str(module.state_dir()),
            "HAL_HUB_WORKER": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    if module.python and module.source == "path":
        env["PYTHONPATH"] = str(module.root)  # a module found on disk is not installed anywhere
    return env


def worker_argv(module: ModuleSpec) -> list[str]:
    """The command that starts a worker for `module` (before any bwrap prefix)."""
    if module.python:
        interpreter = list(module.interpreter or (sys.executable,))
        functions = ",".join(sorted({t.function for t in module.tools}))
        script = [str(WORKER_SCRIPT), "--module", module.python, "--functions", functions]
        return [*interpreter, *WORKER_FLAGS, *script]
    if module.exec and module.protocol == "jsonl":
        return list(module.exec)
    raise ValueError(f"module {module.name} has no worker command (protocol {module.protocol})")


def worker_cwd(module: ModuleSpec) -> Path:
    """v2: the module's private state directory (D16); v1: the module root, as before."""
    if module.schema == 1:
        return module.root
    return paths.ensure_private_dir(module.state_dir())


class _Worker:
    """One worker process and its protocol pipes."""

    def __init__(self, module: ModuleSpec, proc: subprocess.Popen[bytes], kill_grace: float) -> None:
        self.module = module
        self.proc = proc
        self.kill_grace = kill_grace
        self.calls = 0
        self.last_used = 0.0
        self.info: WorkerInfo | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        self.known_secrets: frozenset[str] = (
            frozenset()
        )  # values given to this worker; rebound, never mutated
        self._buffer = b""
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name=f"hal-worker-{module.name}", daemon=True
        )
        self._stderr_thread.start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in iter(self.proc.stderr.readline, b""):
            line = str(scrub(raw.decode("utf-8", "replace").rstrip("\n"), self.known_secrets))
            self.stderr_tail.append(line)
            log.debug("[%s %d] %s", self.module.name, self.proc.pid, line)

    def send(self, message: Mapping[str, object]) -> None:
        line = json.dumps({"v": PROTOCOL_VERSION, **message}, ensure_ascii=True, allow_nan=False) + "\n"
        assert self.proc.stdin is not None
        self.proc.stdin.write(line.encode("ascii"))
        self.proc.stdin.flush()

    def receive(self, deadline: float, cancel: threading.Event | None = None) -> dict | None:
        """The next message; None at end of stream. Raises `TimeoutError` or `_Cancelled`."""
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                if len(self._buffer) > MAX_MESSAGE_BYTES:
                    raise WorkerError("output_too_large", f"a protocol line passed {MAX_MESSAGE_BYTES} bytes")
                if cancel is not None and cancel.is_set():
                    raise _Cancelled
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if not selector.select(min(TICK_S, remaining)):
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    return None
                self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        if len(line) > MAX_MESSAGE_BYTES:
            raise WorkerError("output_too_large", f"a protocol line passed {MAX_MESSAGE_BYTES} bytes")
        try:
            message = json.loads(line)
        except ValueError:
            raise WorkerError("protocol_error", "the worker sent a line that is not JSON") from None
        if not isinstance(message, dict) or message.get("v") != PROTOCOL_VERSION:
            raise WorkerError("protocol_error", "the worker sent a message that is not protocol v1")
        return message

    def last_stderr_line(self) -> str:
        self._stderr_thread.join(timeout=TICK_S)
        for line in reversed(self.stderr_tail):
            if line.strip():
                return line.strip()[:CRASH_LINE_CHARS]
        return ""

    def kill(self) -> None:
        """SIGTERM to the process group, SIGKILL after `kill_grace`, then reap."""
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=self.kill_grace)
            except subprocess.TimeoutExpired:
                pass
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)  # children may outlive a leader that obeyed SIGTERM
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=REAP_TIMEOUT_S)
        self._close_pipes()
        self._stderr_thread.join(timeout=REAP_TIMEOUT_S)  # the group is dead, so stderr reaches EOF
        if self.proc.stderr is not None and not self._stderr_thread.is_alive():
            self.proc.stderr.close()

    def shutdown(self) -> None:
        """Asks the worker to exit; kills the group if it has not after `kill_grace`."""
        with contextlib.suppress(OSError, ValueError):
            self.send({"op": "shutdown"})
        try:
            self.proc.wait(timeout=self.kill_grace)
        except subprocess.TimeoutExpired:
            pass
        self.kill()

    def _close_pipes(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


class _Cancelled(Exception):
    """The call's cancel event was set."""


@dataclass
class _Pool:
    """Workers of one module (and one network setting): idle ones and a count of all."""

    idle: list[_Worker] = field(default_factory=list)
    total: int = 0
    failed_until: float = 0.0
    failure: str = ""
    condition: threading.Condition = field(default_factory=threading.Condition)


class Runner:
    """Runs tool calls in worker pools; safe to call from several threads."""

    def __init__(
        self,
        config: RunnerConfig | None = None,
        *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or RunnerConfig()
        self._popen = popen
        self._clock = clock
        self._pools: dict[tuple[str, bool], _Pool] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._closed = threading.Event()
        self._reaper: threading.Thread | None = None
        self._bwrap: bool | None = None  # probed on first use

    # -- public API --

    def run(
        self,
        module: ModuleSpec,
        tool: ToolSpec,
        args: Mapping[str, object],
        *,
        timeout: float,
        secrets: Mapping[str, str] | None = None,
        cancel: threading.Event | None = None,
        on_tool_call: Callable[[NestedCall], RunOutcome] | None = None,
    ) -> RunOutcome:
        """Runs `tool` with `args` (already split by `call_arguments`) and returns the outcome.

        `secrets` must name only secrets `module` declares (from `resolve_secrets`); raises
        `ValueError` otherwise, because sending one module's secret to another is a bug.
        """
        started = time.monotonic()
        secrets = dict(secrets or {})
        undeclared = set(secrets) - {s.name for s in module.secrets}
        if undeclared:
            raise ValueError(f"{module.name} does not declare the secrets {sorted(undeclared)}")
        if self._closed.is_set():
            return RunOutcome(False, error_type="worker_failed", message="the runner is closed")
        pool_key = (module.name, self._needs_network(module, tool))
        try:
            worker = self._acquire(module, pool_key)
        except WorkerError as exc:
            return RunOutcome(
                False, error_type=exc.error_type, message=exc.message, seconds=self._since(started)
            )
        outcome = self._call(worker, module, tool, args, secrets, started, timeout, cancel, on_tool_call)
        if not secrets:
            return outcome
        return dataclasses.replace(
            outcome, result=scrub(outcome.result, secrets), message=scrub(outcome.message, secrets)
        )

    def probe(self, module: ModuleSpec) -> WorkerInfo:
        """Starts a worker, reads its `ready`, and shuts it down; raises `WorkerError` (for `doctor`)."""
        worker = self._start(module, network=any(t.network for t in module.tools))
        try:
            assert worker.info is not None
            return dataclasses.replace(worker.info, stderr_tail=tuple(worker.stderr_tail))
        finally:
            worker.shutdown()

    def close(self) -> None:
        """Shuts every worker down; front ends call it on exit."""
        self._closed.set()
        with self._lock:
            pools = list(self._pools.values())
        for pool in pools:
            with pool.condition:
                workers, pool.idle = pool.idle, []
                pool.total -= len(workers)
                pool.condition.notify_all()
            for worker in workers:
                worker.shutdown()

    # -- pools --

    def _needs_network(self, module: ModuleSpec, tool: ToolSpec) -> bool:
        return tool.network

    def sandbox_status(self) -> str:
        """One line for `doctor`: whether workers without network get it dropped."""
        if self.config.bwrap == "off":
            return 'network drop off ([hub.runner].bwrap = "off"); tools without network keep it'
        if self._uses_bwrap():
            return "network drop on: workers of tools without network run under bubblewrap"
        return "network drop unavailable: bubblewrap cannot start a sandbox here; tools keep the network"

    def _uses_bwrap(self) -> bool:
        if self.config.bwrap == "off":
            return False
        with self._lock:
            if self._bwrap is None:
                self._bwrap = bwrap_available()
                if not self._bwrap:
                    log.warning("bubblewrap is unavailable; tools without network run with it (REQ-HUB-70)")
            return self._bwrap

    def _pool(self, key: tuple[str, bool]) -> _Pool:
        with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                pool = self._pools[key] = _Pool()
            if self._reaper is None:
                self._reaper = threading.Thread(target=self._reap_loop, name="hal-runner-reaper", daemon=True)
                self._reaper.start()
            return pool

    def _acquire(self, module: ModuleSpec, key: tuple[str, bool]) -> _Worker:
        pool = self._pool(key)
        deadline = time.monotonic() + self.config.queue_timeout
        with pool.condition:
            while True:
                if self._clock() < pool.failed_until:
                    raise WorkerError(
                        "worker_failed", f"{module.name} failed to start recently: {pool.failure}"
                    )
                while pool.idle:
                    worker = pool.idle.pop()
                    if worker.alive():
                        return worker
                    pool.total -= 1
                    worker.kill()
                if pool.total < module.max_workers:
                    pool.total += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError(
                        "busy", f"all {module.max_workers} workers of {module.name} stayed busy"
                    )
                pool.condition.wait(remaining)
        try:
            return self._start(module, network=key[1])
        except WorkerError as exc:
            with pool.condition:
                pool.total -= 1
                pool.failed_until = self._clock() + FAILED_START_BACKOFF_S
                pool.failure = exc.message
                pool.condition.notify()
            raise

    def _release(self, key: tuple[str, bool], worker: _Worker, *, keep: bool) -> None:
        pool = self._pool(key)
        recycle = worker.calls >= self.config.max_calls_per_worker
        if keep and not recycle and worker.alive() and not self._closed.is_set():
            worker.last_used = self._clock()
            with pool.condition:
                pool.idle.append(worker)
                pool.condition.notify()
            return
        with pool.condition:
            pool.total -= 1
            pool.condition.notify()
        if keep:
            worker.shutdown()
        else:
            worker.kill()

    def _reap_loop(self) -> None:
        while not self._closed.wait(REAPER_INTERVAL_S):
            self.reap_idle()

    def reap_idle(self) -> None:
        """Shuts down workers idle longer than `idle_timeout`."""
        now = self._clock()
        with self._lock:
            pools = list(self._pools.values())
        for pool in pools:
            with pool.condition:
                stale = [w for w in pool.idle if now - w.last_used >= self.config.idle_timeout]
                pool.idle = [w for w in pool.idle if w not in stale]
                pool.total -= len(stale)
            for worker in stale:
                worker.shutdown()

    # -- workers --

    def _start(self, module: ModuleSpec, *, network: bool) -> _Worker:
        try:
            argv = worker_argv(module)
            if self._uses_bwrap():
                argv = [BWRAP, *BWRAP_COMMON, *(() if network else BWRAP_OFFLINE), "--", *argv]
            proc = self._popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=worker_environment(module),
                cwd=worker_cwd(module),
                start_new_session=True,
            )
        except (OSError, ValueError, paths.UnsafePathError) as exc:
            raise WorkerError("worker_failed", f"cannot start a worker for {module.name}: {exc}") from None
        worker = _Worker(module, proc, self.config.kill_grace)
        deadline = time.monotonic() + module.startup_timeout
        try:
            message = worker.receive(deadline)
        except TimeoutError:
            worker.kill()
            raise WorkerError(
                "worker_failed", f"{module.name} sent no ready within {module.startup_timeout:g} s"
            ) from None
        except WorkerError as exc:
            worker.kill()
            raise WorkerError("worker_failed", f"{module.name}: {exc.message}") from None
        if message is None or message.get("op") != "ready":
            worker.kill()
            detail = worker.last_stderr_line() or f"exit status {worker.proc.returncode}"
            raise WorkerError("worker_failed", f"{module.name} exited before ready: {detail}")
        worker.info = WorkerInfo(
            pid=int(message.get("pid") or proc.pid),
            python=str(message.get("python", "")),
            module=str(message.get("module", "")),
            functions=dict(message.get("functions") or {}),
            missing=tuple(message.get("missing") or ()),
        )
        log.debug("started worker %d for %s", proc.pid, module.name)
        return worker

    def _call(
        self,
        worker: _Worker,
        module: ModuleSpec,
        tool: ToolSpec,
        args: Mapping[str, object],
        secrets: Mapping[str, str],
        started: float,
        timeout: float,
        cancel: threading.Event | None,
        on_tool_call: Callable[[NestedCall], RunOutcome] | None,
    ) -> RunOutcome:
        key = (module.name, self._needs_network(module, tool))
        call_id = f"c-{next(self._ids):06d}"
        deadline = started + timeout
        pid = worker.pid

        def done(outcome: RunOutcome, *, keep: bool) -> RunOutcome:
            self._release(key, worker, keep=keep)
            return outcome

        def failed(error_type: str, message: str, *, keep: bool = False) -> RunOutcome:
            outcome = RunOutcome(False, error_type=error_type, message=message, worker_pid=pid)
            return done(_timed(outcome, started), keep=keep)

        worker.known_secrets = worker.known_secrets | {v for v in secrets.values() if v}
        try:
            worker.send(
                {
                    "op": "call",
                    "id": call_id,
                    "function": tool.function,
                    "args": dict(args),
                    "secrets": dict(secrets),
                    "timeout_s": timeout,
                }
            )
            worker.calls += 1
            while True:
                message = worker.receive(deadline, cancel)
                if message is None:
                    return failed("worker_crashed", self._crash_message(worker))
                op = message.get("op")
                if op == "tool_call":
                    reply = self._nested(worker, module, tool, call_id, message, on_tool_call)
                    worker.send(reply)
                    continue
                if op == "pong":
                    continue
                if op != "result" or message.get("id") != call_id:
                    return failed("protocol_error", f"expected the result of {call_id}, got {op!r}")
                if message.get("ok"):
                    return done(
                        _timed(RunOutcome(True, result=message.get("result"), worker_pid=pid), started),
                        keep=True,
                    )
                error_type, text = _map_error(message.get("error"))
                return failed(error_type, text, keep=True)
        except TimeoutError:
            return failed("timeout", f"{tool.name} took longer than {timeout:g} s and was stopped")
        except _Cancelled:
            return failed("cancelled", f"{tool.name} was cancelled")
        except WorkerError as exc:
            return failed(exc.error_type, exc.message)
        except (BrokenPipeError, OSError, ValueError) as exc:
            if not worker.alive():
                return failed("worker_crashed", self._crash_message(worker))
            return failed("protocol_error", f"cannot talk to the worker: {exc}")

    def _nested(
        self,
        worker: _Worker,
        module: ModuleSpec,
        tool: ToolSpec,
        call_id: str,
        message: Mapping[str, object],
        on_tool_call: Callable[[NestedCall], RunOutcome] | None,
    ) -> dict[str, object]:
        nested_id = message.get("id")
        if message.get("parent") != call_id:
            raise WorkerError(
                "protocol_error", f"tool_call names parent {message.get('parent')!r}, not {call_id}"
            )
        if on_tool_call is None:
            error = {"type": "policy_denied", "message": "nested tool calls are not available for this call"}
            return {"op": "tool_result", "id": nested_id, "ok": False, "error": error}
        args = message.get("args")
        request = NestedCall(
            module=module,
            parent_tool=tool,
            parent_id=call_id,
            tool=str(message.get("tool", "")),
            args=args if isinstance(args, dict) else {},
        )
        outcome = on_tool_call(request)
        if outcome.ok:
            return {"op": "tool_result", "id": nested_id, "ok": True, "result": outcome.result}
        error = {"type": outcome.error_type or "tool_error", "message": outcome.message or ""}
        return {"op": "tool_result", "id": nested_id, "ok": False, "error": error}

    def _crash_message(self, worker: _Worker) -> str:
        with contextlib.suppress(subprocess.TimeoutExpired):
            worker.proc.wait(timeout=worker.kill_grace)
        code = worker.proc.returncode
        line = worker.last_stderr_line()
        text = f"the {worker.module.name} worker exited with status {code} during the call"
        return f"{text}: {line}" if line else text

    @staticmethod
    def _since(started: float) -> float:
        return round(time.monotonic() - started, 3)


def _timed(outcome: RunOutcome, started: float) -> RunOutcome:
    return dataclasses.replace(outcome, seconds=round(time.monotonic() - started, 3))


def _map_error(error: object) -> tuple[str, str]:
    """Maps a worker `error` object to the caller's `(error_type, message)`."""
    if not isinstance(error, dict):
        return "protocol_error", "the worker sent a malformed error"
    kind = str(error.get("type", "")) or "Error"
    message = str(error.get("message", ""))
    if kind in ERROR_TYPES:
        return ERROR_TYPES[kind], message
    if kind == "ToolError":
        return "tool_error", message
    return "tool_error", f"{kind}: {message}" if message else kind
