"""Deprecated hardware profiles: maps VRAM and RAM to `max`, `24gb`, `12gb` or `cloud` presets.

`hal tune` (halkit.tuning) now sizes models from the real hardware; these names stay as presets
for the tuning limits so old commands and scripts keep working. The suggestion below is
informational only.

The thresholds keep a margin: a nominal 24 GB card reports 24564 MiB and the desktop holds
about 1 GB of it, so the cut-offs sit below the nominal sizes (06-core-cli, "Configure,
profiles and migration"). Detection never raises: a missing `nvidia-smi` means no GPU.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from halkit import proc

log = logging.getLogger(__name__)

MAX_VRAM_MIB = 22000  # a 24 GB card minus the desktop's share
MAX_RAM_GIB = 60  # room for the large model's CPU offload
MID_VRAM_MIB = 11000  # a 12 GB card minus the desktop's share
NVIDIA_SMI_TIMEOUT_S = 10
MEMINFO = Path("/proc/meminfo")
KIB_PER_GIB = 1024 * 1024


@dataclass(frozen=True)
class Profile:
    """A deprecated profile kept as a preset for the tuning limits (`[models]` in the config)."""

    name: str
    context_cap: int  # `[models] context_cap`
    kv_type: str  # `[models] kv_type`
    note: str


# The old profiles named fixed models; those are gone (`hal tune` picks from the models you have).
# Each name now maps to limits that suit its hardware class, so old scripts keep a sensible result.
PROFILES: dict[str, Profile] = {
    "max": Profile("max", 131072, "auto", "a 24 GB card and 64 GB of RAM: the model's full context"),
    "24gb": Profile("24gb", 65536, "q8_0", "a 24 GB card"),
    "12gb": Profile("12gb", 32768, "q8_0", "a 12 GB card"),
    "cloud": Profile("cloud", 8192, "q8_0", "no useful GPU: a small local context"),
}
DEFAULT_PRESET = Profile("auto", 131072, "auto", "the tuning default")


def profile_for_hardware(vram_mib: int, ram_gib: float) -> str:
    """The profile name for a GPU with `vram_mib` MiB and a host with `ram_gib` GiB of RAM."""
    if vram_mib >= MAX_VRAM_MIB and ram_gib >= MAX_RAM_GIB:
        return "max"
    if vram_mib >= MAX_VRAM_MIB:
        return "24gb"
    if vram_mib >= MID_VRAM_MIB:
        return "12gb"
    return "cloud"


def detect_vram_mib() -> int:
    """The largest GPU's memory in MiB, or 0 when `nvidia-smi` is missing or fails."""
    try:
        done = proc.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            timeout=NVIDIA_SMI_TIMEOUT_S,
            check=False,
        )
        return max((int(x) for x in done.stdout.decode("ascii", "replace").split()), default=0)
    except (OSError, proc.ProcTimeout, ValueError) as exc:
        log.info("no GPU detected: %s", exc)
        return 0


def detect_ram_gib() -> float:
    """Total RAM in GiB from `/proc/meminfo`, or 0.0 when it cannot be read."""
    try:
        for line in MEMINFO.read_text(encoding="ascii", errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / KIB_PER_GIB
    except (OSError, ValueError, IndexError) as exc:
        log.info("cannot read RAM size: %s", exc)
    return 0.0


def detect_profile() -> str:
    """The profile for this machine."""
    return profile_for_hardware(detect_vram_mib(), detect_ram_gib())
