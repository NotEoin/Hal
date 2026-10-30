"""gpu-light protocol 1: file claims and the light (REQ-GW-52).

The gateway writes its own claim file instead of calling the `gpu-light` binary, so it needs
nothing installed. Decisions: the claim is bound to this process (`lifetime: pid`), so a crash
voids it; the wait ends at once when no watcher keeps `light.json` fresh, because then nobody
reports background jobs (protocol.md, "Effective state").
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .. import launchers
from . import AcquireResult, ClaimClass

log = logging.getLogger(__name__)

PROTOCOL = 1
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
LIGHT_FILE = "light.json"
CLAIMS_DIR = "claims"
POLL_S = 1.0  # REQ-GW-52: read light.json every second
DEFAULT_STALE_S = 15.0  # `config.stale_after_s` of the watcher when light.json lacks it
PRIVATE_MASK = 0o077


class ClaimError(RuntimeError):
    """A v1 claim could not be written."""


def state_dir(configured: str = "") -> Path:
    """`$GPU_LIGHT_DIR`, else `$XDG_STATE_HOME/gpu-light`, else `~/.local/state/gpu-light`."""
    if configured:
        return Path(configured).expanduser()
    if os.environ.get("GPU_LIGHT_DIR"):
        return Path(os.environ["GPU_LIGHT_DIR"])
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "gpu-light"


def directory_ok(path: Path) -> bool:
    """True when `path` is a directory owned by this user with no group or other bits."""
    try:
        st = path.stat()
    except OSError:
        return False
    return path.is_dir() and st.st_uid == os.getuid() and not st.st_mode & PRIVATE_MASK


def _utc(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_light(path: Path) -> dict | None:  # type: ignore[type-arg]
    try:
        doc = json.loads((path / LIGHT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def effective_state(path: Path, mono: Callable[[], float] = time.monotonic) -> str:
    """The reader's state from `light.json`: red, amber, green, or `unknown` (missing or stale)."""
    doc = _read_light(path)
    if doc is None or doc.get("protocol") != PROTOCOL or doc.get("boot_id") != launchers.boot_id():
        return "unknown"
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    stale = float(config.get("stale_after_s", DEFAULT_STALE_S))  # type: ignore[union-attr]
    if not isinstance(doc.get("mono"), (int, float)) or mono() - float(doc["mono"]) > stale:
        return "unknown"
    state = doc.get("state")
    return state if state in ("red", "amber", "green") else "unknown"


class FileClaim:
    """A gpu-light protocol 1 claim file owned by this process."""

    def __init__(self, directory: Path, name: str) -> None:
        if not NAME_RE.fullmatch(name):
            raise ClaimError(f"claim name {name!r} must match {NAME_RE.pattern}")
        self.dir = directory
        self.name = name

    @property
    def path(self) -> Path:
        """Where the claim file lives."""
        return self.dir / CLAIMS_DIR / f"{self.name}.json"

    def held(self) -> bool:
        """True while the claim file exists."""
        return self.path.exists()

    def write(self, reason: str) -> None:
        """Writes the claim atomically with `fsync` of the file and the directory (REQ-GW-52)."""
        if not directory_ok(self.dir):
            raise ClaimError(f"{self.dir} is not a private directory owned by this user")
        claims = self.dir / CLAIMS_DIR
        claims.mkdir(mode=0o700, exist_ok=True)
        pid = os.getpid()
        doc = {
            "protocol": PROTOCOL, "name": self.name, "lifetime": "pid", "pid": pid,
            "pid_start": launchers.pid_start(pid), "boot_id": launchers.boot_id(),
            "created": _utc(), "expires": None, "reason": reason, "command": None,
        }  # fmt: skip
        tmp = claims / f".{self.name}.json.{pid}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(doc).encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, self.path)
        dir_fd = os.open(claims, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def remove(self) -> None:
        """Unlinks the claim; a missing file is fine."""
        self.path.unlink(missing_ok=True)


class V1Coordinator:
    """One gateway-wide gpu-light v1 claim (the class is ignored, protocol 1 has none)."""

    kind = "gpu-light-v1"

    def __init__(
        self,
        claim_name: str,
        directory: Path,
        *,
        sleep: Callable[[float], None] = time.sleep,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.claim = FileClaim(directory, claim_name)
        self._sleep, self._mono = sleep, mono

    def acquire(
        self,
        name: str,  # noqa: ARG002 - v1 has one claim whatever the caller names it
        *,
        cls: ClaimClass,  # noqa: ARG002 - protocol 1 has no classes
        wait_s: float,
        vram_mib: int | None,  # noqa: ARG002 - protocol 1 has no admission
        uses: Sequence[str],  # noqa: ARG002 - protocol 1 has no reclaimers
        reason: str,
    ) -> AcquireResult:
        """Writes the claim, then waits until no background job is on the GPU or `wait_s` passes."""
        start = self._mono()
        if not self.claim.held():
            self.claim.write(reason)
        while True:
            doc = _read_light(self.claim.dir)
            if effective_state(self.claim.dir, self._mono) == "unknown" or not (doc or {}).get("background"):
                return AcquireResult(True, self._mono() - start, "")
            if self._mono() - start >= wait_s:
                warning = f"gpu-light still shows background jobs after {wait_s:.0f} s"
                log.warning(warning)
                return AcquireResult(True, self._mono() - start, warning)
            self._sleep(POLL_S)

    def release(self, name: str) -> None:  # noqa: ARG002 - one claim only
        """Removes the claim file."""
        self.claim.remove()

    def status(self) -> dict[str, object]:
        """Kind, claim name, whether held and the light."""
        return {
            "kind": self.kind, "claim": self.claim.name, "held": self.claim.held(),
            "light": effective_state(self.claim.dir, self._mono),
        }  # fmt: skip
