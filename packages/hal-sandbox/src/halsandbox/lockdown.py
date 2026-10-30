"""Lockdown levels (RESOLUTIONS I2): what each level lets a box session do.

Every decision about runtime, backend, `--direct` and the dependency-prep egress box goes
through `Policy`, so a level is enforced in one place. A stricter level never allows more
than a looser one (tested), and `off` is never chosen automatically.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from .errors import LockdownError

STRICT, STANDARD, RELAXED, OFF = "strict", "standard", "relaxed", "off"
_RANK = {STRICT: 0, STANDARD: 1, RELAXED: 2, OFF: 3}


@dataclass(frozen=True)
class Policy:
    """The capabilities one lockdown level grants."""

    level: str
    require_runsc: bool
    allow_direct: bool
    allow_egress_prep: bool
    allow_bwrap: bool
    allow_backend_none: bool
    hub_only_upstreams: bool
    paranoia_floor: str  # "off" means no floor

    @classmethod
    def for_level(cls, level: str) -> Policy:
        """The policy of `level`; raises `LockdownError` for an unknown name."""
        if level not in _RANK:
            raise LockdownError(f"unknown lockdown level {level!r}; use strict, standard, relaxed or off")
        rank = _RANK[level]
        return cls(
            level=level,
            require_runsc=level == STRICT,
            allow_direct=rank >= _RANK[RELAXED],
            allow_egress_prep=rank >= _RANK[RELAXED],
            allow_bwrap=rank >= _RANK[RELAXED],  # bwrap shares the host kernel: relaxed at most
            allow_backend_none=level == OFF,
            hub_only_upstreams=level == STRICT,
            paranoia_floor="high" if level == STRICT else "off",
        )

    def choose_runtime(
        self, available: Collection[str], requested: str | None = None
    ) -> tuple[str, list[str]]:
        """Returns the docker runtime to use and any warnings for `hal doctor`.

        Raises `LockdownError` when `strict` has no `runsc`, or when the request is `runc` under
        `strict`.
        """
        warnings: list[str] = []
        if requested and requested not in available:
            raise LockdownError(f"runtime {requested} is not registered with docker")
        if self.require_runsc:
            if "runsc" not in available:
                raise LockdownError(
                    "lockdown strict needs the gVisor runtime runsc; see docs/howto/install-gvisor.md"
                )
            if requested == "runc":
                raise LockdownError("lockdown strict refuses runc; use runsc or lower the lockdown level")
            return "runsc", warnings
        if requested:
            return requested, warnings
        if "runsc" in available:
            return "runsc", warnings
        warnings.append("runsc is not installed: the box runs on runc and shares the host kernel")
        return "runc", warnings

    def check_direct(self) -> None:
        """Raises `LockdownError` unless `--direct` (the real project mounted rw) is allowed."""
        if not self.allow_direct:
            raise LockdownError(
                f"--direct needs lockdown relaxed or lower (now {self.level}); "
                "edit [sandbox].lockdown on the host"
            )
