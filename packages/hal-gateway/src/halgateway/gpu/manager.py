"""The GPU manager: takes a claim before GPU work, gives it back when idle (REQ-GW-51 to 57).

Decisions: the manager counts requests per GPU backend and per claim class itself, so it does
not depend on the router's own counter; a request is counted from the gate to its end, which
covers a swap wait (a reclaim then answers `busy`). The off flag is a `stat` per request with
no cache, so `hal on` takes effect at once. Peers are checked by the peer guard (step 17).
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from ..errors import GatewayError, Unavailable
from . import AcquireResult, ClaimClass, GpuCoordinator, LeaseInfo, ReleaseResult
from .peers import PeerGuard

log = logging.getLogger(__name__)

OFF_FLAG = "off"
OFF_MESSAGE = "Hal is off; run `hal on`"
CLASSES: tuple[ClaimClass, ...] = ("interactive", "service")


def make_coordinator(cfg: Mapping[str, object]) -> GpuCoordinator | None:
    """The coordinator for `[gateway.gpu].coordinator`; None means no coordination (REQ-GW-50)."""
    from . import lightv1

    mode = str(cfg["coordinator"])
    directory = lightv1.state_dir(str(cfg["v1_dir"]))
    if mode == "gpu-light-v2":
        log.warning("gpu-light v2 is not available in this build; no GPU coordination")
        return None
    if mode in ("auto", "gpu-light-v1") and lightv1.directory_ok(directory):
        return lightv1.V1Coordinator(str(cfg["v1_claim_name"]), directory)
    if mode == "gpu-light-v1":
        log.warning("gpu-light v1 directory %s is missing or not private; no GPU coordination", directory)
    return None


class GpuBusy(Unavailable):
    """The GPU claim timed out and `on_busy` is `fail`."""

    code = "gpu_busy"
    retry_after = 30  # seconds; a foreground job rarely ends sooner


class GpuManager:
    """Takes one claim per class, releases it when idle, and answers release requests."""

    def __init__(
        self,
        cfg: Mapping[str, object],
        backends: Mapping[str, object],
        coordinator: GpuCoordinator | None,
        *,
        state_dir: Path,
        clock: Callable[[], float] = time.monotonic,
        guard: PeerGuard | None = None,
    ) -> None:
        self.cfg = cfg
        self.guard = guard
        self.backends = backends
        self.coordinator = coordinator
        self.off_flag = state_dir.parent / OFF_FLAG
        self.clock = clock
        self._lock = threading.RLock()
        self._held: set[str] = set()  # claim classes held now
        self._inflight: dict[str, int] = {}  # backend name -> requests
        self._class_inflight: dict[str, int] = {c: 0 for c in CLASSES}
        self._last: dict[str, float] = {c: clock() for c in CLASSES}
        self._last_any = clock()
        self._served: dict[str, set[str]] = {}  # backend name -> upstream models served
        self._dirty = False  # a GPU request ran since the last unload
        self._leases: dict[str, LeaseInfo] = {}
        self.router_stop: Callable[[], None] | None = None

    # --- leases (filled by sessions, step 19) ---------------------------------------------

    def add_lease(self, lease: LeaseInfo) -> None:
        """Registers a lease; it keeps the GPU on a plain release."""
        with self._lock:
            self._leases[lease.session_id] = lease

    def drop_lease(self, session_id: str) -> None:
        """Forgets a lease."""
        with self._lock:
            self._leases.pop(session_id, None)

    def leases(self) -> list[LeaseInfo]:
        """The leases held now."""
        with self._lock:
            return list(self._leases.values())

    # --- the request path -----------------------------------------------------------------

    def claim_class(self, principal: object) -> ClaimClass:
        """The class of the principal (05 "GPU coordination")."""
        own = getattr(principal, "cls", "")
        if own in CLASSES:
            return own  # type: ignore[return-value]
        key = "owner_unix_class" if getattr(principal, "listener", "unix") == "unix" else "owner_tcp_class"
        return "service" if self.cfg[key] == "service" else "interactive"

    @contextlib.contextmanager
    def request(self, target: object, principal: object) -> Iterator[None]:
        """Gates a request, counts it while it runs, and notes the model it used."""
        backend = target.backend  # type: ignore[attr-defined]
        if not backend.gpu:
            yield
            return
        cls = self.claim_class(principal)
        self.gate(target, principal)
        with self._lock:
            self._inflight[backend.name] = self._inflight.get(backend.name, 0) + 1
            self._class_inflight[cls] += 1
            self._served.setdefault(backend.name, set()).add(target.model)  # type: ignore[attr-defined]
            self._dirty = True
        try:
            yield
        finally:
            with self._lock:
                self._inflight[backend.name] -= 1
                self._class_inflight[cls] -= 1
                self._last[cls] = self._last_any = self.clock()

    def gate(self, target: object, principal: object) -> None:
        """Refuses while Hal is off, then takes the claim for the principal's class if none is held."""
        if not target.backend.gpu:  # type: ignore[attr-defined]
            return
        if self.cfg["respect_off_flag"] and not getattr(principal, "lease", False) and self.off_flag.exists():
            raise Unavailable(OFF_MESSAGE)
        cls = self.claim_class(principal)
        with self._lock:
            if cls in self._held or (self.coordinator is None and self.in_flight()):
                return
            self.check_peers()
            if self.coordinator is None:
                return
            result = self._acquire(cls, target)
            if result.held:
                self._held.add(cls)
        self._unload_offloaded()

    def check_peers(self) -> None:
        """REQ-GW-56: raises `PeerBusy` when a peer holds the GPU; call before a router start or load."""
        if self.guard is not None:
            self.guard.check()

    def _acquire(self, cls: ClaimClass, target: object) -> AcquireResult:
        assert self.coordinator is not None  # noqa: S101 - callers check
        v1 = self.coordinator.kind == "gpu-light-v1"
        wait_s = float(self.cfg["v1_claim_wait_s" if v1 else "claim_wait_s"])  # type: ignore[arg-type]
        model = target.model  # type: ignore[attr-defined]
        result = self.coordinator.acquire(
            f"{self.cfg['reclaimer_name']}-{cls}" if not v1 else str(self.cfg["v1_claim_name"]),
            cls=cls,
            wait_s=wait_s,
            vram_mib=self.vram_for(model),
            uses=list(target.backend.entry.get("gpu_light_uses") or []),  # type: ignore[attr-defined]
            reason=f"hal gateway: {target.alias} on {target.backend.name}",  # type: ignore[attr-defined]
        )
        if not result.held and self.cfg["on_busy"] == "fail":
            raise GpuBusy(result.warning or "the GPU is busy; try again later")
        if not result.held:
            log.warning("proceeding without a GPU claim: %s", result.warning)
        return result

    def vram_for(self, model: str) -> int | None:
        """MiB configured for `model` in `[gateway.gpu.vram_mib]`, or None."""
        table = self.cfg["vram_mib"]
        return int(table[model]) if isinstance(table, Mapping) and model in table else None  # type: ignore[call-overload]

    def _unload_offloaded(self) -> None:
        """REQ-GW-57: after a claim, free Ollama models that sit partly in system RAM."""
        for backend in self.backends.values():
            if getattr(backend, "type", "") != "ollama" or not backend.entry["unload_offloaded_on_claim"]:  # type: ignore[attr-defined]
                continue
            try:
                for name in backend.unload_offloaded():  # type: ignore[attr-defined]
                    log.info("unloaded partly offloaded Ollama model %s", name)
            except (GatewayError, OSError) as exc:
                log.warning("cannot unload offloaded Ollama models: %s", exc)

    # --- idle timers and release ----------------------------------------------------------

    def in_flight(self) -> int:
        """GPU requests running now."""
        with self._lock:
            return sum(self._inflight.values())

    def tick(self, now: float) -> None:
        """Called every 10 s: unloads and releases after `idle_unload_s` of quiet (REQ-GW-53)."""
        idle_unload = float(self.cfg["idle_unload_s"])  # type: ignore[arg-type]
        with self._lock:
            quiet = now - self._last_any >= idle_unload
            if idle_unload > 0 and self._dirty and not self.in_flight() and quiet:
                log.info("GPU idle for %.0f s: unloading", now - self._last_any)
                self._free(stop_router=False)

    def _free(self, *, stop_router: bool) -> tuple[tuple[str, ...], bool]:
        unloaded: list[str] = []
        with self._lock:
            for name, backend in self.backends.items():
                if not getattr(backend, "gpu", False) or not hasattr(backend, "free_vram"):
                    continue
                try:
                    unloaded += backend.free_vram(self._served.get(name, ()))
                except (GatewayError, OSError) as exc:
                    log.warning("cannot free the GPU memory of %s: %s", name, exc)
            stopped = False
            if stop_router and self.router_stop is not None:
                self.router_stop()
                stopped = True
            self._served.clear()
            self._dirty = False
            self._release_claims()
        return tuple(unloaded), stopped

    def _release_claims(self) -> None:
        if self.coordinator is not None:
            for cls in sorted(self._held):
                self.coordinator.release(f"{self.cfg['reclaimer_name']}-{cls}")
        self._held.clear()

    def release(self, *, force: bool, stop_router: bool, reclaim: bool) -> ReleaseResult:
        """REQ-GW-55: unload and release, unless leases or running requests say otherwise."""
        with self._lock:
            busy = self.in_flight()
            if reclaim and busy:
                return ReleaseResult(False, (), False, (), busy)
            kept = tuple(self._leases.values())
            if not reclaim and not force and kept:
                return ReleaseResult(False, (), False, kept, 0)
            unloaded, stopped = self._free(stop_router=stop_router)
            return ReleaseResult(True, unloaded, stopped, (), 0)

    def close(self) -> None:
        """Releases every claim at gateway stop; loaded models stay."""
        with self._lock:
            self._release_claims()

    def status(self) -> dict[str, object]:
        """The `gpu` object of the status document."""
        with self._lock:
            doc: dict[str, object] = {
                "coordinator": self.coordinator.status() if self.coordinator else {"kind": "none"},
                "held": sorted(self._held),
                "in_flight": self.in_flight(),
                "leases": len(self._leases),
                "off": self.off_flag.exists(),
                "peers": self.guard.status() if self.guard else [],
            }
        return doc
