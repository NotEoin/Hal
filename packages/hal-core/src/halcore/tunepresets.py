"""The Quick setup presets of `hal tune`: a VRAM budget to tune every role against.

A preset is one row of `PRESETS`, so adding a size is one line. `hardware_for` turns a preset
into the `Hardware` the tuning maths plans against; nothing here touches the real machine.

Design decisions:

- A GPU preset gives a card whose total and free memory are both the preset size, so the plan
  is the same whatever else is running.
- RAM and CPU threads always come from the real machine; a preset only swaps the GPUs.
- No model names live here: presets pick from whatever discovery found.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from halkit.hardware import Gpu, Hardware

MIB_PER_GB = 1024  # the tune screen counts a "GB" as 1024 MiB, like the rest of the screen


@dataclass(frozen=True)
class Preset:
    """One Quick setup choice; `vram_mib` is None for this machine and 0 for no GPU."""

    key: str
    label: str
    vram_mib: int | None


PRESETS = (
    Preset("auto", "This machine (auto)", None),
    Preset("gpu-12", "12 GB GPU", 12 * MIB_PER_GB),
    Preset("gpu-24", "24 GB GPU", 24 * MIB_PER_GB),
    Preset("cpu", "CPU only", 0),
)


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
