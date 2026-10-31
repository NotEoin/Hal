"""Hardware profiles for `hal configure`: maps VRAM and RAM to `max`, `24gb`, `12gb` or `cloud`.

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
    """The settings a profile writes."""

    name: str
    chat_model: str
    code_model: str  # informational: the coding role the gateway default uses


PROFILES: dict[str, Profile] = {
    "max": Profile("max", "gemma4:12b-it-qat", "hal-code (llama-server, mode gpu)"),
    "24gb": Profile("24gb", "gemma4:12b-it-qat", "qwen3.8:27b"),
    "12gb": Profile("12gb", "qwen3.5:4b", "gemma4:12b-it-qat"),
    "cloud": Profile("cloud", "qwen3.5:2b", "none local"),
}


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
