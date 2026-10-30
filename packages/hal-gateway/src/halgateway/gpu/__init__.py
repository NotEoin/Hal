"""GPU coordination types and detection (05 "GPU coordination").

The gateway asks a coordinator for the GPU before it forwards to a GPU backend. Only the
protocol and the result types live here; `lightv1` writes file claims and `manager` decides
when to take, keep and give back a claim.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

Kind = Literal["gpu-light-v2", "gpu-light-v1", "none"]
ClaimClass = Literal["interactive", "service"]


@dataclass(frozen=True)
class AcquireResult:
    """The outcome of one claim attempt."""

    held: bool
    waited_s: float
    warning: str


@dataclass(frozen=True)
class LeaseInfo:
    """One session lease that keeps the GPU state on a plain release."""

    session_id: str
    label: str
    since: str  # UTC Z


@dataclass(frozen=True)
class ReleaseResult:
    """The answer of `POST /hal/v1/gpu/release`."""

    released: bool  # false when kept by leases or busy
    unloaded: tuple[str, ...]
    router_stopped: bool
    kept: tuple[LeaseInfo, ...]  # leases that blocked a release without force
    busy: int  # in-flight requests that blocked a reclaim

    def as_json(self) -> dict[str, object]:
        """The JSON document sent to the caller."""
        return {
            "released": self.released,
            "unloaded": list(self.unloaded),
            "router_stopped": self.router_stopped,
            "kept": [{"session_id": k.session_id, "label": k.label, "since": k.since} for k in self.kept],
            "busy": self.busy,
        }


class GpuCoordinator(Protocol):
    """What the manager needs from gpu-light, in whichever version is installed."""

    kind: Kind

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
        """Takes the claim `name` and waits up to `wait_s` for the GPU."""

    def release(self, name: str) -> None:
        """Gives the claim `name` back; unknown names are ignored."""

    def status(self) -> dict[str, object]:
        """A small document for `GET /hal/v1/status`."""


def detect(v1_dir: str = "", v2_socket: str = "", timeout_s: float = 2.0) -> Kind:
    """The coordinator kind available now: v2 when its socket answers, else v1, else none."""
    from . import lightv1, lightv2

    if lightv2.probe(lightv2.socket_path(v2_socket), timeout_s):
        return "gpu-light-v2"
    return "gpu-light-v1" if lightv1.directory_ok(lightv1.state_dir(v1_dir)) else "none"


def status_line(v1_dir: str = "") -> str:
    """`GPU light: green` or `gpu-light: absent`, for `hal status`."""
    from . import lightv1

    path = lightv1.state_dir(v1_dir)
    if not lightv1.directory_ok(path):
        return "gpu-light: absent"
    return f"GPU light: {lightv1.effective_state(path)}"
