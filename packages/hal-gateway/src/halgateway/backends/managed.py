"""A `managed-openai` backend: an OpenAI-compatible server the gateway starts and stops (REQ-GW-90..96).

It runs any server that takes its port from `PORT` and its API key from an environment
variable, such as a vLLM launch script. Modelled on the managed llama-server router. Decisions:

- State lives in `<state>/gateway/<name>.json` (pid, pid_start, boot id, config hash, address).
  A gateway restart adopts a server whose argv, cwd, env and port hash the same and stops any
  other (REQ-GW-93), so restarting the gateway does not pay a cold start of a minute or more.
- The API key is generated into `<state>/gateway/<name>.key` (0600) and reaches the child only in
  the variable named by `key_env`, never in argv, where any local user could read it.
- The child gets the 01-security environment allowlist, the configured `env` (refused at config
  load when a name looks like a secret), `HOST=127.0.0.1`, `PORT` and the key; nothing else from
  the gateway's environment (REQ-GW-91).
- Ready means `health_path` answers 200 and `/v1/models` lists a served model: vLLM answers
  `/health` before the model list is wired, and the first request would fail in between.
- Model listing reads `served_models` from the config, never the server, so `GET /v1/models`
  does not depend on (or trigger) a cold start.
- A deliberate stop is not a crash: after `stop()` requests get 503 until `start()`. `yield_gpu()`
  and `free_vram()` stop the process without that mark, so the next request starts it again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

from halkit import secrets

from .. import launchers, llamacmd, park, relay
from ..errors import BadGatewayError, Unavailable
from .llama_router import CRASH_WINDOW_S, POLL_S  # the router's rules, so both backends behave alike
from .openai import OpenAIBackend

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 5.0  # a local health probe; a server slower than this is not ready
HTTP_OK = 200


class ManagedError(Unavailable):
    """The managed server could not start or stay up; the message names its log."""

    code = "backend_failed"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ManagedServer(OpenAIBackend):
    """A `managed-openai` backend that owns one server process."""

    managed = True

    def __init__(self, name: str, entry: Mapping[str, object], *, ollama_url: str) -> None:
        super().__init__(name, entry, ollama_url=ollama_url)
        self.launcher = launchers.ProcessLauncher()
        self.poll_s = POLL_S
        self.exclusive_gpu = bool(entry["exclusive_gpu"])
        self.idle_free = bool(entry["idle_stop"])  # read by the GPU manager's idle unload
        self.parking = park.Parking(bool(entry["park"]))
        self._lock: threading.RLock = threading.RLock()
        self._idle = threading.Condition()
        self._inflight = 0
        self._record: dict[str, object] = {}
        self._crashes: list[float] = []
        self._stopped = False
        self._state = Path()
        # set by the gateway: frees the card (other servers, Ollama) before a start (REQ-GW-95)
        self.before_start: Callable[[ManagedServer], None] = lambda server: None

    def configure(self, state: Path, lock: threading.RLock | None = None) -> None:
        """Gives the server the gateway's state directory (`<state>/gateway`) and its lifecycle lock.

        The gateway passes one lock to every backend with a process, so two starts that each stop
        the other cannot deadlock.
        """
        self._state = state
        if lock is not None:
            self._lock = lock

    # --- files and identity -----------------------------------------------------------------

    def _path(self, suffix: str) -> Path:
        return self._state / f"{self.name}{suffix}"

    def log_path(self) -> Path:
        """Where the server writes its output."""
        return self._path(".log")

    def key_path(self) -> Path:
        """The generated API key, mode 0600."""
        return self._path(".key")

    def auth_headers(self) -> dict[str, str]:
        """The generated key when `key_env` is set; the configured key otherwise."""
        if self.entry["key_env"]:
            try:
                return {"Authorization": f"Bearer {secrets.read_secret(self.key_path())}"}
            except FileNotFoundError:
                return {}  # not started yet: nothing listens that needs it
            except (OSError, ValueError, secrets.UnsafeSecretError) as exc:
                raise ManagedError(f"backend {self.name}: cannot read {self.key_path()}: {exc}") from exc
        return super().auth_headers()

    def _argv(self) -> list[str]:
        argv = [str(item) for item in self.entry["command"]]  # type: ignore[attr-defined]
        argv[0] = str(Path(argv[0]).expanduser())
        return argv

    def _cwd(self) -> Path:
        configured = str(self.entry["cwd"])
        return Path(configured).expanduser() if configured else Path(self._argv()[0]).parent

    def fingerprint(self) -> str:
        """sha256 of what decides the running server: argv, cwd, env, port and the key variable."""
        doc = {
            "argv": self._argv(),
            "cwd": str(self._cwd()),
            "env": dict(self.entry["env"]),  # type: ignore[call-overload]
            "port": self.entry["port"],
            "key_env": self.entry["key_env"],
        }
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode("utf-8")).hexdigest()

    def child_env(self) -> dict[str, str]:
        """The child's whole environment (REQ-GW-91)."""
        env = {name: os.environ[name] for name in llamacmd.ENV_ALLOWLIST if name in os.environ}
        env.update({str(k): str(v) for k, v in dict(self.entry["env"]).items()})  # type: ignore[call-overload]
        env.update(HOST=llamacmd.LOOPBACK, PORT=str(self.entry["port"]))
        if self.entry["key_env"]:
            env[str(self.entry["key_env"])] = secrets.generate_token(self.key_path())
        return env

    # --- process state ----------------------------------------------------------------------

    def _read_record(self) -> dict[str, object]:
        try:
            doc = json.loads(self._path(".json").read_text(encoding="ascii"))
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _write_record(self, record: dict[str, object]) -> None:
        self._state.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self._path(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            json.dump(record, fh)
        os.replace(tmp, self._path(".json"))

    def _drop_record(self) -> None:
        self._record = {}
        with contextlib.suppress(OSError):
            self._path(".json").unlink()

    def running(self) -> bool:
        """True while the process named in the record is alive."""
        pid, started = self._record.get("pid"), self._record.get("pid_start")
        return isinstance(pid, int) and isinstance(started, int) and self.launcher.alive(pid, started)

    def static_models(self) -> list[str]:
        """The configured model ids; listing them never contacts or starts the server."""
        return [str(model) for model in self.entry["served_models"]]  # type: ignore[attr-defined]

    # --- lifecycle --------------------------------------------------------------------------

    def adopt(self) -> str:
        """On gateway start: adopt a running server with the same config hash, stop any other.

        Returns `adopted`, `stopped` or `none`.
        """
        with self._lock:
            record = self._read_record()
            pid, started = record.get("pid"), record.get("pid_start")
            if (
                not (isinstance(pid, int) and isinstance(started, int))
                or record.get("boot_id") != launchers.boot_id()
            ):
                self._drop_record()
                return "none"
            if record.get("config_sha256") == self.fingerprint() and launchers.is_alive(pid, started):
                self._record = record
                log.info("adopted %s server %d", self.name, pid)
                return "adopted"
            log.info("stopping %s server %d: it exited or its config changed", self.name, pid)
            self.launcher.stop(pid, started, float(self.entry["stop_timeout_s"]))  # type: ignore[arg-type]
            self._drop_record()
            return "stopped"

    def start(self) -> None:
        """Starts the server (after `before_start` frees the card) and waits until it is ready."""
        with self._lock:
            if self.running() and self._record.get("config_sha256") == self.fingerprint():
                self._stopped = False
                return
            self._stop_locked()
            self.before_start(self)
            self._spawn()
            self._stopped = False
            self._wait_ready()

    def _spawn(self) -> None:
        argv = self._argv()
        if not (Path(argv[0]).is_file() and os.access(argv[0], os.X_OK)):
            raise ManagedError(f"backend {self.name}: {argv[0]} is not an executable file")
        if not self._cwd().is_dir():
            raise ManagedError(f"backend {self.name}: cwd {self._cwd()} is not a directory")
        try:
            pid, started = self.launcher.start(argv, self.child_env(), self.log_path(), cwd=self._cwd())
        except launchers.LaunchError as exc:
            raise ManagedError(f"backend {self.name}: {exc}") from exc
        self._record = {
            "pid": pid,
            "pid_start": started,
            "boot_id": launchers.boot_id(),
            "config_sha256": self.fingerprint(),
            "address": f"{llamacmd.LOOPBACK}:{self.entry['port']}",
            "started": _now(),
        }
        self._write_record(self._record)
        log.info("started %s server %d", self.name, pid)

    def ready(self) -> bool:
        """True when the health path answers 200 and `/v1/models` lists a served model."""
        status, _ = relay.probe(self, str(self.entry["health_path"]), PROBE_TIMEOUT_S)
        if status != HTTP_OK:
            return False
        status, raw = relay.probe(self, "/v1/models", PROBE_TIMEOUT_S)
        try:
            ids = {str(m.get("id")) for m in json.loads(raw)["data"] if isinstance(m, dict)}
        except (ValueError, KeyError, TypeError):
            return False
        return status == HTTP_OK and bool(ids & set(self.static_models()))

    def _wait_ready(self) -> None:
        """REQ-GW-92: poll until ready; stop the process group and fail with the log path otherwise."""
        timeout = float(self.entry["startup_timeout_s"])  # type: ignore[arg-type]
        deadline = time.monotonic() + timeout
        while not self.ready():
            if not self.running():
                self._stop_locked()  # the leader is gone; its children may still hold VRAM
                raise ManagedError(f"backend {self.name} exited during startup; see {self.log_path()}")
            if time.monotonic() >= deadline:
                self._stop_locked()
                raise ManagedError(
                    f"backend {self.name} not ready after {timeout:g} s; see {self.log_path()}"
                )
            time.sleep(self.poll_s)
        log.info("%s is ready", self.name)

    def _stop_locked(self) -> None:
        self.parking.forget()
        pid, started = self._record.get("pid"), self._record.get("pid_start")
        if isinstance(pid, int) and isinstance(started, int):
            self.launcher.stop(pid, started, float(self.entry["stop_timeout_s"]))  # type: ignore[arg-type]
        self._drop_record()

    def stop(self) -> None:
        """Stops the server (admin `backend/stop`); requests get 503 until the next `start()`."""
        with self._lock:
            self._stop_locked()
            self._stopped = True

    def yield_gpu(self) -> None:
        """Frees the card for another backend: parks the server with `park`, else stops it.

        The next request resumes or starts it again.
        """
        with self._lock:
            if self.parking.parked:
                return
            pid = self._record.get("pid")
            if isinstance(pid, int) and self.running() and self.parking.try_park(self.name, pid):
                return
            if self._record:
                log.info("stopping %s to free the GPU", self.name)
            self._stop_locked()

    def ensure(self) -> None:
        """Called before a request is relayed: starts on demand, restarts after a crash within the budget."""
        with self._lock:
            if self.parking.parked and self.running():
                self.before_start(self)
                if self.parking.try_resume(self.name):
                    return
                self._stop_locked()
            if self.running():
                return
            if self._stopped:
                raise ManagedError(
                    f"backend {self.name} was stopped; start it with `hal-gateway backend start {self.name}`"
                )
            if not self._record:
                self.start()
                return
            now = time.monotonic()
            self._crashes = [t for t in self._crashes if now - t < CRASH_WINDOW_S]
            limit = int(self.entry["max_restarts"])  # type: ignore[call-overload]
            if len(self._crashes) >= limit:
                raise ManagedError(
                    f"backend {self.name} crashed {limit} times in {CRASH_WINDOW_S:.0f} s; "
                    f"see {self.log_path()}"
                )
            self._crashes.append(now)
            log.warning("%s exited unexpectedly; restarting it", self.name)
            self.start()

    @contextlib.contextmanager
    def request(self) -> Iterator[None]:
        """Counts a request to the server, so another backend does not stop it mid-reply."""
        with self._idle:
            self._inflight += 1
        try:
            yield
        finally:
            with self._idle:
                self._inflight -= 1
                self._idle.notify_all()

    def in_flight(self) -> int:
        """Requests running on the server now."""
        return self._inflight

    def free_vram(self, served: object = ()) -> list[str]:  # noqa: ARG002 - the server serves its own list
        """Stops the server for a GPU release (REQ-GW-53, 55); returns its models, or nothing when down."""
        if not self.running():
            return []
        if self._inflight:
            raise BadGatewayError(f"backend {self.name} has {self._inflight} request(s) running")
        self.yield_gpu()
        return self.static_models()

    def status(self) -> dict[str, object]:
        """The `backends.<name>` object of `GET /hal/v1/status`."""
        running = self.running()
        state = "running" if running else "stopped" if self._stopped or not self._record else "crashed"
        return {
            "type": self.type, "managed": True, "remote": self.remote, "gpu": self.gpu,
            "state": state, "pid": self._record.get("pid"), "address": self._record.get("address"),
            "restarts": len(self._crashes), "models": self.static_models(), "log": str(self.log_path()),
        }  # fmt: skip
