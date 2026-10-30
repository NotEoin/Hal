"""gpu-light v2 client: JSON-RPC over the daemon's unix socket (REQ-GW-50, 51, 53, 59).

Decisions: one long-lived connection with a reader thread that routes answers by `id` and
`claim_granted` notifications by claim name; a claim has lifetime `connection`, so it ends
with the connection and `maintain` re-sends the claims still wanted after a reconnect (backoff
1 s doubling to 30 s). A timed-out claim is released at once, so a pending claim never pauses
background work for a request that already proceeded. Every call has a timeout.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import AcquireResult, ClaimClass

log = logging.getLogger(__name__)

PROTOCOL = 2
CALL_TIMEOUT_S = 2.0  # REQ-GW design: every call but the grant wait
MAX_LINE_BYTES = 1_048_576  # daemon.max_message_bytes
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 30.0
NOT_FOUND = -32001
SOCKET_NAME = "daemon.sock"
RECLAIM_TIMEOUT_S = 60  # unloading two presets (05, "gpu-light v2")
IDLE_AFTER_S = 120  # equals idle_release_s
MATCH_UNITS = ("hal-gateway.service", "ollama.service")
MATCH_EXE = ("llama-server",)


class V2Error(RuntimeError):
    """The daemon is unreachable, or speaks another protocol."""


class RpcError(V2Error):
    """The daemon answered with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"gpu-light error {code}: {message}")
        self.code = code


def socket_path(configured: str = "") -> Path:
    """`gpu.v2_socket`, else `$GPU_LIGHT_SOCKET`, else the runtime directory, else the state directory."""
    if configured:
        return Path(configured).expanduser()
    if os.environ.get("GPU_LIGHT_SOCKET"):
        return Path(os.environ["GPU_LIGHT_SOCKET"])
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "gpu-light" / SOCKET_NAME
    from . import lightv1

    return lightv1.state_dir() / SOCKET_NAME


def reclaimer_spec(name: str, argv0: str | None = None) -> dict[str, object]:
    """The `command` reclaimer spec of 05 "gpu-light v2" (REQ-GW-59)."""
    exe = str(Path(argv0 or sys.argv[0]).resolve())
    return {
        "name": name, "type": "command", "match_unit": list(MATCH_UNITS), "match_exe": list(MATCH_EXE),
        "class": "service", "idle_after_s": IDLE_AFTER_S, "reclaim_for": ["interactive", "batch"],
        "timeout_s": RECLAIM_TIMEOUT_S, "reclaim": [exe, "release-gpu", "--reclaim"],
    }  # fmt: skip


class Connection:
    """One daemon connection with a reader thread."""

    def __init__(self, path: Path, version: str, connect_timeout_s: float = CALL_TIMEOUT_S) -> None:
        self.path, self.version, self.connect_timeout_s = path, version, connect_timeout_s
        self.alive = False
        self._sock: socket.socket | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._waiting: dict[int, tuple[threading.Event, list[dict]]] = {}  # type: ignore[type-arg]
        self._granted: dict[str, threading.Event] = {}

    def open(self) -> dict:  # type: ignore[type-arg]
        """Connects and sends `hello`; returns the daemon's answer; raises `V2Error`."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout_s)
        try:
            sock.connect(str(self.path))
        except OSError as exc:
            sock.close()
            raise V2Error(f"cannot connect to gpu-light at {self.path}: {exc}") from exc
        sock.settimeout(None)
        self._sock, self.alive = sock, True
        threading.Thread(target=self._read, args=(sock,), name="hal-gpulight-v2", daemon=True).start()
        try:
            hello = self.call(
                "hello", {"client": "hal-gateway", "version": self.version, "protocol": PROTOCOL}
            )
        except V2Error:
            self.close()
            raise
        if hello.get("protocol") != PROTOCOL:
            self.close()
            raise V2Error(f"gpu-light speaks protocol {hello.get('protocol')}, this gateway needs {PROTOCOL}")
        return hello  # type: ignore[no-any-return]

    def _read(self, sock: socket.socket) -> None:
        stream = sock.makefile("rb")
        try:
            while line := stream.readline(MAX_LINE_BYTES + 1):
                if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    break
                self._route(msg)
        except OSError:
            pass
        finally:
            self.alive = False
            with self._lock:
                for event, _ in self._waiting.values():
                    event.set()
                for event in self._granted.values():
                    event.set()

    def _route(self, msg: object) -> None:
        if not isinstance(msg, dict):
            return
        if "id" in msg:
            with self._lock:
                slot = self._waiting.get(msg["id"])
            if slot is not None:
                slot[1].append(msg)
                slot[0].set()
        elif msg.get("method") == "claim_granted":
            params = msg.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            with self._lock:
                event = self._granted.get(str(name))
            if event is not None:
                event.set()

    def call(
        self, method: str, params: Mapping[str, object] | None = None, timeout_s: float = CALL_TIMEOUT_S
    ) -> dict:  # type: ignore[type-arg]
        """Sends one request and waits for its answer; raises `RpcError` or `V2Error`."""
        if not self.alive or self._sock is None:
            raise V2Error("not connected to gpu-light")
        req_id = next(self._ids)
        event, box = threading.Event(), []  # type: ignore[var-annotated]
        with self._lock:
            self._waiting[req_id] = (event, box)
        doc = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": dict(params or {})}
        try:
            self._sock.sendall(json.dumps(doc).encode("utf-8") + b"\n")
            if not event.wait(timeout_s) or not box:
                raise V2Error(f"gpu-light did not answer {method} within {timeout_s:.0f} s")
        except OSError as exc:
            raise V2Error(f"cannot send {method} to gpu-light: {exc}") from exc
        finally:
            with self._lock:
                self._waiting.pop(req_id, None)
        error = box[0].get("error")
        if error:
            raise RpcError(int(error.get("code", 0)), str(error.get("message", "")))
        result = box[0].get("result")
        return result if isinstance(result, dict) else {}

    def expect_grant(self, name: str) -> threading.Event:
        """An event set by the `claim_granted` notification for `name` (or by a lost connection)."""
        with self._lock:
            return self._granted.setdefault(name, threading.Event())

    def forget_grant(self, name: str) -> None:
        """Drops the grant event of `name`."""
        with self._lock:
            self._granted.pop(name, None)

    def close(self) -> None:
        """Closes the socket; claims of lifetime `connection` end with it."""
        self.alive = False
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()


def probe(path: Path, timeout_s: float = CALL_TIMEOUT_S) -> bool:
    """True when the daemon at `path` answers `hello` with protocol 2; another major is logged (REQ-GW-50)."""
    conn = Connection(path, "probe", timeout_s)
    try:
        conn.open()
    except V2Error as exc:
        log.info("gpu-light v2 is not usable at %s: %s", path, exc)
        return False
    finally:
        conn.close()
    return True


class V2Coordinator:
    """Per-class connection claims on gpu-light v2."""

    kind = "gpu-light-v2"

    def __init__(
        self,
        path: Path,
        *,
        reclaimer: Mapping[str, object],
        version: str = "0",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path, self.reclaimer, self.version, self.clock = path, dict(reclaimer), version, clock
        self._conn: Connection | None = None
        self._claims: dict[str, dict[str, object]] = {}  # claim name -> params, to re-send after a reconnect
        self._backoff = BACKOFF_START_S
        self._next_try = 0.0
        self._warned_uses: set[str] = set()
        self._lock = threading.RLock()

    # --- connection ---------------------------------------------------------------------

    def connected(self) -> bool:
        """True while the connection is up."""
        return self._conn is not None and self._conn.alive

    def connect(self) -> bool:
        """Connects, registers the reclaimer, and re-sends held claims; False while backing off."""
        with self._lock:
            if self.connected():
                return True
            if self.clock() < self._next_try:
                return False
            conn = Connection(self.path, self.version)
            try:
                conn.open()
                self._conn = conn
                self._ensure_reclaimer()
                for params in self._claims.values():
                    conn.call("claim", params)
            except V2Error as exc:
                conn.close()
                self._conn = None
                self._next_try = self.clock() + self._backoff
                log.warning("gpu-light v2 unavailable (retry in %.0f s): %s", self._backoff, exc)
                self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
                return False
            self._backoff = BACKOFF_START_S
            return True

    def maintain(self) -> None:
        """Called by the 10 s timer: reconnects after a lost connection."""
        if self._claims or self._conn is not None:
            self.connect()

    def _ensure_reclaimer(self) -> None:
        """REQ-GW-59: add the reclaimer when absent, replace a stale CLI one, leave a config one alone."""
        assert self._conn is not None  # noqa: S101 - set just before
        name = str(self.reclaimer["name"])
        listed = self._conn.call("reclaimers.list").get("reclaimers", [])
        have = next((r for r in listed if isinstance(r, dict) and r.get("name") == name), None)
        if have is not None:
            spec = have.get("spec") if isinstance(have.get("spec"), dict) else {}
            if all(spec.get(k) == v for k, v in self.reclaimer.items()):
                return
            if have.get("source") == "config":
                log.warning("reclaimer %s is defined in the gpu-light config and differs; left alone", name)
                return
            self._conn.call("reclaimers.remove", {"name": name})
        self._conn.call("reclaimers.add", {"spec": self.reclaimer})

    # --- coordinator --------------------------------------------------------------------

    def acquire(
        self,
        name: str,
        *,
        cls: ClaimClass,
        wait_s: float,
        vram_mib: int | None,
        uses: Sequence[str],
        reason: str,
    ) -> AcquireResult:
        """Sends `claim` and waits up to `wait_s` for the grant (REQ-GW-51)."""
        start = self.clock()
        if not self.connect():
            return AcquireResult(False, 0.0, "gpu-light v2 is not reachable")
        params: dict[str, object] = {
            "name": name, "class": cls, "gpu": 0, "exclusive": True, "uses": list(uses),
            "reason": reason, "lifetime": "connection",
        }  # fmt: skip
        if vram_mib:
            params["vram_mib"] = vram_mib
        conn = self._conn
        assert conn is not None  # noqa: S101 - connect() returned True
        granted_event = conn.expect_grant(name)
        try:
            claim = self._claim(conn, params)
            self._claims[name] = params
            if not claim.get("granted") and not granted_event.wait(wait_s):
                self.release(name)
                warning = f"gpu-light did not grant {name} within {wait_s:.0f} s"
                return AcquireResult(False, self.clock() - start, warning)
        except V2Error as exc:
            return AcquireResult(False, self.clock() - start, str(exc))
        finally:
            conn.forget_grant(name)
        return AcquireResult(conn.alive, self.clock() - start, "")

    def _claim(self, conn: Connection, params: dict[str, object]) -> dict:  # type: ignore[type-arg]
        try:
            return dict(conn.call("claim", params).get("claim") or {})
        except RpcError as exc:
            if exc.code != NOT_FOUND or not params["uses"]:
                raise
            key = ",".join(params["uses"])  # type: ignore[arg-type]
            if key not in self._warned_uses:
                self._warned_uses.add(key)
                log.warning("gpu-light has no reclaimer for %s; claiming without `uses`", key)
            params = {k: v for k, v in params.items() if k != "uses"}
            return dict(conn.call("claim", params).get("claim") or {})

    def release(self, name: str) -> None:
        """Sends `claim.release`; errors are logged, the claim ends with the connection anyway."""
        self._claims.pop(name, None)
        if self.connected():
            try:
                self._conn.call("claim.release", {"name": name})  # type: ignore[union-attr]
            except V2Error as exc:
                log.warning("cannot release gpu-light claim %s: %s", name, exc)

    def status(self) -> dict[str, object]:
        """Kind, connection and held claims."""
        return {
            "kind": self.kind, "connected": self.connected(), "socket": str(self.path),
            "claims": sorted(self._claims), "reclaimer": self.reclaimer["name"],
        }  # fmt: skip

    def close(self) -> None:
        """Closes the connection (claims end with it)."""
        if self._conn is not None:
            self._conn.close()
