"""The managed llama-server router: start, wait, adopt, restart on crash, stop (REQ-GW-30..39).

Ported from `hal-serve`. Design decisions:

- State lives in `<state>/gateway/router.json` (pid, pid_start, boot_id, mode, render hash,
  address). A gateway restart adopts a router with the same mode and render hash and stops any
  other (REQ-GW-36), so a restart of the gateway does not reload 50 GB of weights.
- The router listens on loopback TCP with a generated 0600 API key, or on a unix socket below
  `<state>/gateway/run/`. Nothing but this module holds the key.
- A deliberate stop is not a crash: requests after `router/stop` get 503 until a mode switch,
  while a crash restarts on the next request, at most `max_restarts` times in `CRASH_WINDOW_S`.
- Every load goes through `Verifier.verify_path` (REQ-GW-78): at render for the startup
  presets and again before an admin `load`.
- A start first calls `before_start`, which the gateway uses to stop managed servers that need
  the whole card (REQ-GW-95). `yield_gpu` is the other direction: it stops the router without the
  deliberate-stop mark, so the next request brings it back.
"""

from __future__ import annotations

import configparser
import contextlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

from .. import launchers, llamacmd, modes, park, relay
from ..errors import BadGatewayError, BadRequest, GatewayError, ModelUnverified, RouterBusy, Unavailable
from ..models.manifest import ManifestError, Verifier
from ..policies import route, swap
from .openai import OpenAIBackend, UpstreamAddress, parse_address

log = logging.getLogger(__name__)

RECORD_NAME = "router.json"
KEY_NAME = "router.key"
LOG_NAME = "router.log"
CRASH_WINDOW_S = 600.0  # REQ-GW-39
POLL_S = 1.0  # REQ-GW-34
STATUS_TIMEOUT_S = 5.0
SWAP_CALL_TIMEOUT_S = 120.0  # hal-route.py: a slot save of a large cache takes a while
ADMIN_TIMEOUT_S = 120.0  # a load or unload call answers when the model is in or out


class RouterError(Unavailable):
    """The router could not start or stay up; the message names the log."""

    code = "router_failed"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def startup_presets(rendered: Path) -> list[str]:
    """Preset names of a rendered mode that load at start (no `load-on-startup = false`).

    A preset without the key inherits it from `[*]`, as llama-server does; the `tuned` mode sets
    it only there, and waiting for a preset the router will not load hung the first request.
    """
    parser = configparser.ConfigParser(interpolation=None, default_section="")
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-sensitive
    parser.read_string(f"[{modes.TOP}]\n{rendered.read_text(encoding='utf-8')}", source=str(rendered))
    default = parser.get("*", "load-on-startup", fallback="true") if parser.has_section("*") else "true"
    return [
        name
        for name in parser.sections()
        if name not in ("*", modes.TOP)
        and parser.get(name, "load-on-startup", fallback=default).lower() != "false"
    ]


class LlamaRouter(OpenAIBackend):
    """A `llama-router` backend; managed entries own a llama-server process."""

    def __init__(self, name: str, entry: Mapping[str, object], *, ollama_url: str) -> None:
        super().__init__(name, entry, ollama_url=ollama_url)
        self.managed = bool(entry["managed"])
        self.parking = park.Parking(bool(entry["park"]))
        self.launcher = launchers.ProcessLauncher()
        self.poll_s = POLL_S
        self._lock = threading.RLock()
        self._idle = threading.Condition()
        self._inflight = 0
        self._record: dict[str, object] = {}
        self._crashes: list[float] = []
        self._stopped = False
        self._state = Path()
        self._models_dir = Path()
        self._verifier: Verifier | None = None
        self._ollama: Callable[[], list] = list  # type: ignore[type-arg]
        self.hal: modes.HalSettings | None = None  # the `[hal]` section of the active (or configured) mode
        self._swap: swap.SwapScheduler | None = None
        self.turns = route.TurnMemory()  # the route policy's held choice per conversation
        self.on_mode: Callable[[modes.HalSettings | None], None] = lambda hal: None
        self.before_start: Callable[[LlamaRouter], None] = lambda router: None

    def configure(
        self,
        state: Path,
        models_dir: Path,
        verifier: Verifier,
        ollama: Callable[[], list],  # type: ignore[type-arg]
        lock: threading.RLock | None = None,
    ) -> None:
        """Gives the router the gateway's state directory (`<state>/gateway`), models and Ollama backends.

        `lock` is the gateway's lifecycle lock, shared with managed servers so that two starts
        that each stop the other cannot deadlock.
        """
        self._state, self._models_dir, self._verifier, self._ollama = state, models_dir, verifier, ollama
        if lock is not None:
            self._lock = lock

    # --- addressing -------------------------------------------------------------------------

    def _socket(self) -> Path:
        return self._state / "run" / "router.sock"

    def address(self) -> UpstreamAddress:
        """Where the router listens now (a unix socket when `listen = "unix"`)."""
        if self.managed and self.entry["listen"] == "unix":
            return parse_address(f"unix:{self._socket()}")
        return super().address()

    def auth_headers(self) -> dict[str, str]:
        """The generated key of a managed TCP router; the configured key otherwise."""
        key_path = self._state / KEY_NAME
        if self.managed and key_path.exists():
            return {"Authorization": f"Bearer {key_path.read_text(encoding='ascii').strip()}"}
        return super().auth_headers()

    def _key_file(self) -> Path:
        path = self._state / KEY_NAME
        if not path.exists():
            self._state.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(secrets.token_urlsafe(32))
        return path

    # --- process state ----------------------------------------------------------------------

    def _read_record(self) -> dict[str, object]:
        try:
            doc = json.loads((self._state / RECORD_NAME).read_text(encoding="ascii"))
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _write_record(self, record: dict[str, object]) -> None:
        self._state.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self._state / f".{RECORD_NAME}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            json.dump(record, fh)
        os.replace(tmp, self._state / RECORD_NAME)

    def _drop_record(self) -> None:
        self._record = {}
        with contextlib.suppress(OSError):
            (self._state / RECORD_NAME).unlink()

    def running(self) -> bool:
        """True while the router process named in the record is alive."""
        pid, started = self._record.get("pid"), self._record.get("pid_start")
        return isinstance(pid, int) and isinstance(started, int) and self.launcher.alive(pid, started)

    def _set_hal(self, hal: modes.HalSettings | None) -> None:
        self.hal = hal
        self._swap = None  # a new mode means a new pair of models
        self.on_mode(hal)

    def peek(self) -> None:
        """Reads the configured mode's `[hal]` section without starting anything (for mode aliases)."""
        try:
            self._set_hal(self._render(self._entry_mode()).hal)
        except RouterError as exc:
            log.warning("cannot read mode %s: %s", self._entry_mode(), exc.message)

    def log_path(self) -> Path:
        """Where the router writes its output."""
        return self._state / LOG_NAME

    def _entry_mode(self) -> str:
        return str(self.entry["mode"])

    def _render(self, mode: str) -> modes.RenderedMode:
        if self._verifier is None:
            raise RouterError("the router is not configured")
        try:
            return modes.render_mode(
                mode,
                models_dir=self._models_dir,
                state_dir=self._state,
                verifier=self._verifier,
                user_dirs=list(self.entry["modes_dirs"]),  # type: ignore[call-overload]
            )
        except modes.ModeError as exc:
            raise RouterError(str(exc)) from exc

    # --- lifecycle --------------------------------------------------------------------------

    def adopt(self) -> str:
        """On gateway start: adopt a running router of the same mode and render, stop any other.

        Returns `adopted`, `stopped` or `none`.
        """
        if not self.managed:
            return "none"
        with self._lock:
            record = self._read_record()
            pid, started = record.get("pid"), record.get("pid_start")
            if (
                not (isinstance(pid, int) and isinstance(started, int))
                or record.get("boot_id") != launchers.boot_id()
            ):
                self._drop_record()
                return "none"
            if not launchers.is_alive(pid, started):
                self._drop_record()
                return "none"
            try:
                current = self._render(str(record.get("mode", "")))
            except RouterError:
                current = None
            if (
                current is not None
                and current.sha256 == record.get("render_sha256")
                and record.get("mode") == self._entry_mode()
            ):
                self._record = record
                self._set_hal(current.hal)
                log.info("adopted llama-server %d in mode %s", pid, record["mode"])
                return "adopted"
            log.info("stopping llama-server %d: its mode or render changed", pid)
            self.launcher.stop(pid, started, float(self.entry["stop_timeout_s"]))  # type: ignore[arg-type]
            self._drop_record()
            return "stopped"

    def start(self, mode: str | None = None) -> None:
        """Starts the router in `mode` (default: the configured one) and waits until it is ready."""
        if not self.managed:
            raise BadRequest(f"backend {self.name} is not managed by the gateway")
        mode = mode or self._entry_mode()
        with self._lock:
            rendered = self._render(mode)
            if (
                self.running()
                and self._record.get("mode") == mode
                and self._record.get("render_sha256") == rendered.sha256
            ):
                self._stopped = False
                return
            self._stop_locked()
            self.before_start(self)
            self._unload_ollama()
            self._spawn(rendered)
            self._stopped = False
            self._wait_loaded(rendered)

    def _spawn(self, rendered: modes.RenderedMode) -> None:
        server = Path(str(self.entry["server"])).expanduser()
        if not (server.is_file() and os.access(server, os.X_OK)):
            raise RouterError(f"llama-server not found at {server}; `hal setup` installs the pinned build")
        if str(self.entry["launcher"]) != "process":
            raise RouterError("the docker launcher is not available yet")
        if str(self.entry["listen"]) == "unix":
            listen, address = str(self._socket()), f"unix:{self._socket()}"
            (self._state / "run").mkdir(mode=0o700, parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                self._socket().unlink()
            key_file = None
        else:
            listen = f"{llamacmd.LOOPBACK}:{self.entry['port']}"
            address, key_file = listen, self._key_file()
        (self._state / "slots").mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            argv, env = llamacmd.llama_server_command(
                llamacmd.Preset(rendered.path, rendered.sha256),
                listen=listen,
                api_key_file=key_file,
                state=self._state.parent,
                llama_dir=server.parent,
                models_max=rendered.hal.models_max,
                autoload=rendered.hal.autoload,
            )
            argv[0] = str(server)
            pid, started = self.launcher.start(argv, env, self.log_path())
        except (llamacmd.CommandRefused, launchers.LaunchError) as exc:
            raise RouterError(str(exc)) from exc
        self._record = {
            "pid": pid,
            "pid_start": started,
            "boot_id": launchers.boot_id(),
            "mode": rendered.name,
            "render_sha256": rendered.sha256,
            "address": address,
            "started": _now(),
        }
        self._write_record(self._record)
        self._set_hal(rendered.hal)
        log.info("started llama-server %d in mode %s", pid, rendered.name)

    def model_status(self, name: str) -> str:
        """The router's status of preset `name`, `missing`, or `starting` while it does not answer."""
        try:
            doc = relay.call_json(self, "GET", "/models", timeout_s=STATUS_TIMEOUT_S)
        except (GatewayError, OSError):
            return "starting"
        for model in doc.get("data", []):
            if isinstance(model, dict) and model.get("id") == name:
                status = model.get("status")
                return str(status.get("value", "unknown")) if isinstance(status, dict) else "unknown"
        return "missing"

    def statuses(self) -> dict[str, str]:
        """Status of every preset the router lists."""
        try:
            doc = relay.call_json(self, "GET", "/models", timeout_s=STATUS_TIMEOUT_S)
        except (GatewayError, OSError):
            return {}
        out = {}
        for model in doc.get("data", []):
            if isinstance(model, dict) and "id" in model:
                status = model.get("status")
                out[str(model["id"])] = (
                    str(status.get("value", "unknown")) if isinstance(status, dict) else "unknown"
                )
        return out

    def _wait_loaded(self, rendered: modes.RenderedMode) -> None:
        """REQ-GW-34: poll every `poll_s` until each startup preset is loaded; stop and fail otherwise."""
        deadline = time.monotonic() + float(self.entry["load_timeout_s"])  # type: ignore[arg-type]
        self._wait_answering(deadline)
        for name in startup_presets(rendered.path):
            seen_loading = False
            while True:
                if not self.running():
                    self._drop_record()
                    raise RouterError(f"llama-server exited while loading {name}; see {self.log_path()}")
                status = self.model_status(name)
                if status == "loaded":
                    break
                seen_loading = seen_loading or status == "loading"
                if status == "failed" or (status == "unloaded" and seen_loading):
                    self._stop_locked()
                    raise RouterError(f"{name} failed to load; see {self.log_path()}")
                if time.monotonic() >= deadline:
                    self._stop_locked()
                    raise RouterError(
                        f"{name} still not loaded after {self.entry['load_timeout_s']} s; "
                        f"see {self.log_path()}"
                    )
                time.sleep(self.poll_s)

    def _wait_answering(self, deadline: float) -> None:
        """Polls until the router answers `/health`: with no startup preset nothing else waits,
        and a request relayed in the half second before it listens got a 502 (seen 2026-11-11)."""
        while True:
            if not self.running():
                self._drop_record()
                raise RouterError(f"llama-server exited while starting; see {self.log_path()}")
            try:
                relay.call_json(self, "GET", "/health", timeout_s=STATUS_TIMEOUT_S)
                return
            except (GatewayError, OSError):
                pass  # not listening yet
            if time.monotonic() >= deadline:
                self._stop_locked()
                raise RouterError(
                    f"llama-server did not answer after {self.entry['load_timeout_s']} s; "
                    f"see {self.log_path()}"
                )
            time.sleep(self.poll_s)

    def _unload_ollama(self) -> None:
        """REQ-GW-33: free the VRAM Ollama holds before the router loads."""
        for backend in self._ollama():
            try:
                held = backend.in_vram()
                for model in held:
                    log.info("unloading Ollama model %s to free VRAM", model)
                    backend.unload(model)
                if held and not backend.wait_unloaded(held):
                    log.warning("Ollama models %s still hold VRAM; starting anyway", ", ".join(held))
            except (GatewayError, OSError) as exc:
                log.warning("cannot unload Ollama models of %s: %s", backend.name, exc)

    def _stop_locked(self) -> None:
        self.parking.forget()
        pid, started = self._record.get("pid"), self._record.get("pid_start")
        if isinstance(pid, int) and isinstance(started, int):
            self.launcher.stop(pid, started, float(self.entry["stop_timeout_s"]))  # type: ignore[arg-type]
        self._drop_record()

    def stop(self) -> None:
        """Stops the router (admin `router/stop`); requests get 503 until the next mode switch."""
        with self._lock:
            self._stop_locked()
            self._stopped = True

    def yield_gpu(self) -> None:
        """Frees the card for another backend: parks llama-server with `park`, else stops it.

        The next request resumes or starts it again.
        """
        with self._lock:
            if self.parking.parked:
                return
            pid = self._record.get("pid")
            if isinstance(pid, int) and self.running() and self.parking.try_park(self.name, pid):
                return
            if self._record:
                log.info("stopping llama-server to free the GPU")
            self._stop_locked()
            self._swap = None  # a restarted router has nothing loaded

    def ensure(self) -> None:
        """Called before a request is relayed: starts on demand, restarts after a crash within the budget."""
        if not self.managed:
            return
        with self._lock:
            if self.parking.parked and self.running():
                self.before_start(self)
                self._unload_ollama()
                if self.parking.try_resume(self.name):
                    return
                self._stop_locked()
            if self.running():
                return
            if self._stopped:
                raise RouterError("the router was stopped; switch mode to start it (`hal-gateway mode`)")
            if not self._record:
                self.start()
                return
            now = time.monotonic()
            self._crashes = [t for t in self._crashes if now - t < CRASH_WINDOW_S]
            mode = str(self._record.get("mode") or self._entry_mode())
            self._drop_record()
            limit = int(self.entry["max_restarts"])  # type: ignore[call-overload]
            if len(self._crashes) >= limit:
                raise RouterError(
                    f"llama-server crashed {limit} times in {CRASH_WINDOW_S:.0f} s; see {self.log_path()}"
                )
            self._crashes.append(now)
            log.warning("llama-server exited unexpectedly; restarting in mode %s", mode)
            self.start(mode)

    def classify(self, classifier: str, text: str) -> tuple[str, float]:
        """Asks preset `classifier` through `/v1/systemone` which model should take `text` (REQ-GW-42)."""
        body = {"model": classifier, "state": text, "questions": route.ROUTE_QUESTION}
        doc = relay.call_json(self, "POST", "/v1/systemone", body, timeout_s=route.CLASSIFIER_TIMEOUT_S)
        return route.parse_answer(doc)

    def swap_call(self, path: str, body: dict, check: bool = True) -> dict:  # type: ignore[type-arg]
        """POSTs to the router for the swap scheduler; with `check`, an HTTP error raises `SwapError`."""
        try:
            return relay.call_json(self, "POST", path, body, timeout_s=SWAP_CALL_TIMEOUT_S)
        except BadGatewayError as exc:
            if check:
                raise swap.SwapError(f"{path}: {exc.message}") from exc
            return {}

    def scheduler(self) -> swap.SwapScheduler | None:
        """The swap scheduler of the active mode, or None when the mode does not swap."""
        hal = self.hal
        if hal is None or not hal.swap:
            return None
        if self._swap is None:
            self._swap = swap.SwapScheduler(
                self,
                hal.main,
                hal.agent,
                slots=hal.swap_slots,
                swap_wait_s=float(self.entry["swap_wait_s"]),  # type: ignore[arg-type]
            )
        return self._swap

    # --- requests in flight, mode switch, load, unload --------------------------------------

    @contextlib.contextmanager
    def request(self) -> Iterator[None]:
        """Counts a request to the router, so a mode switch can drain them."""
        with self._idle:
            self._inflight += 1
        try:
            yield
        finally:
            with self._idle:
                self._inflight -= 1
                self._idle.notify_all()

    def in_flight(self) -> int:
        """Requests running on the router now."""
        return self._inflight

    def switch(self, mode: str, *, drain_s: float, force: bool = False) -> None:
        """REQ-GW-38: drain in-flight requests for `drain_s`, 409 unless `force`, then start `mode`."""
        deadline = time.monotonic() + drain_s
        with self._idle:
            while self._inflight and (left := deadline - time.monotonic()) > 0:
                self._idle.wait(left)
            if self._inflight and not force:
                raise RouterBusy(f"{self._inflight} request(s) still running; retry later or set force")
        self.start(mode)

    def _preset_weights(self, preset: str) -> Path:
        rendered = self._state / "rendered" / f"{self._record.get('mode', '')}.ini"
        parser = configparser.ConfigParser(interpolation=None, default_section="")
        parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-sensitive
        try:
            parser.read_string(f"[{modes.TOP}]\n{rendered.read_text(encoding='utf-8')}")
        except OSError as exc:
            raise BadRequest("the router has no rendered mode; start one first") from exc
        if not parser.has_section(preset):
            raise BadRequest(f"unknown preset {preset!r}")
        value = parser.get(preset, "model", fallback=parser.get("*", "model", fallback=""))
        if not value:
            raise BadRequest(f"preset {preset!r} names no model file")
        return Path(value)

    def load(self, preset: str) -> None:
        """Verifies the weights of `preset` and asks the router to load it."""
        self.ensure()
        try:
            if self._verifier is not None:
                self._verifier.verify_path(self._preset_weights(preset))
        except ManifestError as exc:
            raise ModelUnverified(f"{exc}; run `hal models` to review and pin it") from exc
        relay.call_json(self, "POST", "/models/load", {"model": preset}, timeout_s=ADMIN_TIMEOUT_S)

    def unload(self, preset: str) -> None:
        """Asks the router to unload `preset`."""
        self.ensure()
        relay.call_json(self, "POST", "/models/unload", {"model": preset}, timeout_s=ADMIN_TIMEOUT_S)

    def free_vram(self, served: object = ()) -> list[str]:  # noqa: ARG002 - the router knows its own presets
        """Unloads every loaded preset and forgets the scheduler's view; returns the names (REQ-GW-53)."""
        if not self.running():
            return []
        freed = [name for name, state in self.statuses().items() if state == "loaded"]
        for name in freed:
            relay.call_json(self, "POST", "/models/unload", {"model": name}, timeout_s=ADMIN_TIMEOUT_S)
        if freed and self._swap is not None:
            self._swap.forget()  # the next request sees nothing loaded and loads the main model
        return freed

    def status(self) -> dict[str, object]:
        """The `backends.<name>` object of `GET /hal/v1/status`."""
        base: dict[str, object] = {
            "type": self.type, "managed": self.managed, "remote": self.remote, "gpu": self.gpu,
        }  # fmt: skip
        if not self.managed:
            return base
        running = self.running()
        state = "running" if running else "stopped" if self._stopped or not self._record else "crashed"
        base.update(state=state, mode=self._record.get("mode"), pid=self._record.get("pid"))
        base.update(address=self._record.get("address"), restarts=len(self._crashes))
        base["models"] = self.statuses() if running else {}
        return base
