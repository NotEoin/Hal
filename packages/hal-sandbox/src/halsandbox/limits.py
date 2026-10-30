"""Resource limits for a box: memory, cpus, pids, /tmp size (REQ-BOX-20).

Limits are always on under `strict` and `standard`. A hostile model that forks or allocates
without bound must hit a cgroup wall inside the box, not take the desktop down with it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .config import DEFAULT_PIDS, GIB

MAX_MEMORY_BYTES = 8 * GIB  # REQ-BOX-20: enough for a compile plus a test run
MAX_CPUS = 4
OOM_SCORE_ADJ = 500  # the box dies before the desktop under host memory pressure
_UNITS = {"k": 1024, "m": 1024**2, "g": GIB}


def parse_size(text: str) -> int:
    """Bytes in a size such as `512m`; raises `ValueError` for anything else."""
    if text[-1:] not in _UNITS or not text[:-1].isdigit():
        raise ValueError(f"bad size {text!r}; use e.g. 512m or 8g")
    return int(text[:-1]) * _UNITS[text[-1]]


@dataclass(frozen=True)
class Limits:
    """Resolved limits for one box."""

    memory_bytes: int
    cpus: float
    pids: int = DEFAULT_PIDS
    tmp_bytes: int = GIB

    @classmethod
    def from_config(cls, cfg: Mapping[str, object] | None = None) -> Limits:
        """Resolves `[sandbox]` settings; `auto` memory is `min(8 GiB, RAM/4)`, cpus `min(4, nproc/2)`.

        Raises `ValueError` when the `/tmp` tmpfs would not fit below the memory limit, because
        tmpfs pages count against the memory cgroup.
        """
        cfg = cfg or {}
        mem_text = str(cfg.get("memory", "auto"))
        if mem_text == "auto":
            ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            memory = min(MAX_MEMORY_BYTES, ram // 4)
        else:
            memory = parse_size(mem_text)
        cpus = float(cfg.get("cpus", 0) or 0) or float(min(MAX_CPUS, max(1, (os.cpu_count() or 2) // 2)))
        tmp = parse_size(str(cfg.get("tmp_size", "1g")))
        if tmp >= memory:
            raise ValueError("[sandbox].tmp_size must be below [sandbox].memory: tmpfs counts against memory")
        return cls(memory, cpus, int(cfg.get("pids", DEFAULT_PIDS)), tmp)

    def docker_flags(self) -> list[str]:
        """The `docker run` flags; equal memory and swap forbid swap."""
        return [
            "--memory",
            str(self.memory_bytes),
            "--memory-swap",
            str(self.memory_bytes),
            "--cpus",
            f"{self.cpus:g}",
            "--pids-limit",
            str(self.pids),
            "--oom-score-adj",
            str(OOM_SCORE_ADJ),
        ]
