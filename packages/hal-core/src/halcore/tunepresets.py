"""The Quick setup presets of `hal tune`: a VRAM budget to tune every role against.

A preset is one row of `PRESETS`, so adding a size is one line. `hardware_for` turns a preset
into the `Hardware` the tuning maths plans against; nothing here touches the real machine.

Design decisions:

- A GPU preset gives a card whose total and free memory are both the preset size, so the plan
  is the same whatever else is running.
- RAM and CPU threads always come from the real machine; a preset only swaps the GPUs.
- Presets pick from whatever discovery found. `RECOMMENDED` only names what to pull when
  nothing found fits, so a new user gets a working tag instead of "pull a smaller model".
  The 24 GB set is what this project runs day to day; the smaller sets are chosen by
  weight size from the Ollama library on 2026-11-03 and are not quality-tested.
- The guardian (`GUARDIAN_MODEL`) is the same for every preset: it runs on the CPU, in RAM.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from halkit.hardware import Gpu, Hardware

MIB_PER_GB = 1024  # the tune screen counts a "GB" as 1024 MiB, like the rest of the screen
NOMINAL_SHARE = 0.95  # a "24 GB" card reports a little less (an RTX 4090: 24,564 MiB)


@dataclass(frozen=True)
class Preset:
    """One Quick setup choice; `vram_mib` is None for this machine and 0 for no GPU."""

    key: str
    label: str
    vram_mib: int | None


PRESETS = (
    Preset("auto", "This machine (auto)", None),
    Preset("gpu-8", "8 GB GPU", 8 * MIB_PER_GB),
    Preset("gpu-12", "12 GB GPU", 12 * MIB_PER_GB),
    Preset("gpu-16", "16 GB GPU", 16 * MIB_PER_GB),
    Preset("gpu-24", "24 GB GPU", 24 * MIB_PER_GB),
    Preset("cpu", "CPU only", 0),
)

GUARDIAN_MODEL = "granite4.1-guardian:8b"  # Q6_K; Q4_K_M missed dangerous calls (docs/user/security.md)
_EMBED = "embeddinggemma"
# preset key -> role -> Ollama tag; vision and writer share the chat model (Gemma 4 sees images)
_SETS = {
    "cpu": ("gemma4:e4b-it-qat", "qwen3.5:4b"),
    "gpu-8": ("gemma4:e4b-it-qat", "qwen3.5:9b"),
    "gpu-12": ("gemma4:12b-it-qat", "qwen3.5:9b"),
    "gpu-16": ("gemma4:12b-it-qat", "gpt-oss:20b"),
    "gpu-24": ("gemma4:26b-a4b-it-qat", "qwen3.8:27b"),
}
RECOMMENDED: Mapping[str, Mapping[str, str]] = {
    key: {"chat": chat, "vision": chat, "writer": chat, "classifier": GUARDIAN_MODEL, "code": code, "embed": _EMBED}
    for key, (chat, code) in _SETS.items()
}


def hardware_for(preset: Preset, real: Hardware) -> Hardware:
    """The machine to plan for: `real` itself, or `real` with its GPUs swapped for the preset's."""
    if preset.vram_mib is None:
        return real
    if preset.vram_mib == 0:
        return replace(real, gpus=())
    card = Gpu(0, "preset", preset.label, preset.vram_mib, preset.vram_mib)
    return replace(real, gpus=(card,))


def describe(preset: Preset, real: Hardware) -> str:
    """A short note for the menu row: what the preset plans against."""
    if preset.vram_mib is None:
        total = sum(g.total_mib for g in real.gpus)
        return f"{total / MIB_PER_GB:.0f} GB of VRAM" if total else "no GPU found"
    return "no GPU layers" if preset.vram_mib == 0 else f"plans for {preset.vram_mib // MIB_PER_GB} GB of VRAM"


def tier_for(preset: Preset, real: Hardware) -> str:
    """The `RECOMMENDED` key for `preset`; "auto" takes the largest GPU tier the real VRAM covers."""
    if preset.key in RECOMMENDED:
        return preset.key
    vram = preset.vram_mib if preset.vram_mib is not None else sum(g.total_mib for g in real.gpus)
    fitting = [
        p for p in PRESETS if p.key in RECOMMENDED and p.vram_mib and p.vram_mib * NOMINAL_SHARE <= vram
    ]
    return max(fitting, key=lambda p: p.vram_mib or 0).key if fitting else "cpu"


def recommended(preset: Preset, role: str, real: Hardware) -> str | None:
    """The Ollama tag to pull for `role` under `preset`, or None for a role with no recommendation."""
    return RECOMMENDED[tier_for(preset, real)].get(role)
