"""What the tune screens show: hardware lines, fit words, model labels and role rows, as plain text.

Shared by the full-screen tune app (`halcore.tuneapp`) and the line screen (`halcore.tuneui`),
so both say the same thing. Nothing here imports a terminal library. `open_state` builds the
`TuneSession` both screens edit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from halhub.policy.load import load_policy
from halkit import config as halconfig
from halkit import hardware as hwmod
from halkit import modelconf, tuning

from . import tunestate
from .tui.status import context_bar
from .tui.theme import Look

MIB_PER_GIB = 1024
# a cache that takes more than this share of the VRAM budget leaves little slack for long prompts;
# a judgement, not a measured limit
TIGHT_FRACTION = 0.85
TOKENS = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*$")


def parse_tokens(text: str) -> int:
    """`32768`, `32k` or `1.5m` as a token count; raises `ValueError` for anything else."""
    found = TOKENS.match(text)
    if not found:
        raise ValueError(f"not a number of tokens: {text!r} (try 32768 or 32k)")
    factor = {"": 1, "k": 1024, "m": 1024 * 1024}[found.group(2).lower()]
    return int(float(found.group(1)) * factor)


def gib(mib: int) -> str:
    """MiB as GiB with one decimal."""
    return f"{mib / MIB_PER_GIB:.1f}"


def hardware_lines(hw: hwmod.Hardware) -> list[str]:
    """One line per fact about the machine: each GPU, memory, CPU, WSL2 and detection notes."""
    lines = [f"GPU {g.index}  {g.name}  {g.free_mib} / {g.total_mib} MiB free" for g in hw.gpus]
    lines = lines or ["GPU  none: models run on the CPU"]
    lines.append(f"RAM  {hw.ram_free_mib} / {hw.ram_total_mib} MiB free   CPU  {hw.cpu_threads} threads")
    warning = tuning.busy_warning(hw)
    if warning:
        lines.append(f"note: {warning}")
    if hw.wsl2:
        lines.append("WSL2: VRAM and RAM are what Windows lends this distro")
    return lines + [f"note: {n}" for n in hw.notes]


def fit_line(fit: tunestate.Fit, layers: int | None, look: Look) -> tuple[str, str]:
    """`(style role, text)` of the fit line of one role."""
    g = look.glyphs
    if fit.status == "empty":
        return "muted", "no model: choose one, or auto-tune the role"
    if fit.status in ("missing", "server"):
        return "bad" if fit.status == "missing" else "muted", fit.notes[0] if fit.notes else fit.status
    bar = context_bar(fit.used_mib, fit.total_mib or fit.budget_mib, g)
    if fit.above_guidance:
        return "muted", f"{bar} {gib(fit.used_mib)} GiB VRAM, above the {gib(fit.budget_mib)} GiB guidance, all layers on the GPU"
    if fit.status == "gpu":
        return "ok", f"{bar} {gib(fit.used_mib)} of {gib(fit.budget_mib)} GiB VRAM, all layers on the GPU"
    if fit.status == "hybrid":
        total = f"of {layers} " if layers else ""
        return "muted", f"{bar} {gib(fit.used_mib)} GiB needed, {fit.gpu_layers} {total}layers on the GPU, the rest in RAM"
    if fit.status == "cpu":
        return "muted", f"{gib(fit.used_mib)} GiB in system RAM, CPU only"
    return "bad", "does not fit: " + (fit.notes[-1] if fit.notes else "use a smaller model or context")


def fit_word(fit: tunestate.Fit) -> str:
    """`fits`, `tight`, `too big` and the like: one word for how well a draft fits."""
    if fit.above_guidance:
        return "above guidance"
    if fit.status == "gpu":
        return "tight" if fit.budget_mib and fit.used_mib > TIGHT_FRACTION * fit.budget_mib else "fits"
    return {"hybrid": "tight, uses RAM", "cpu": "CPU only", "does-not-fit": "too big"}.get(fit.status, "")


def fit_text(fit: tunestate.Fit, look: Look) -> str:
    """`9.4 GB  fits` for a sized fit; the fit line text otherwise."""
    if fit.status in ("gpu", "hybrid", "cpu", "does-not-fit") and fit.used_mib:
        return f"{gib(fit.used_mib)} GB  {fit_word(fit)}"
    return fit_line(fit, None, look)[1]


def short(text: str, width: int) -> str:
    """`text` cut to `width` characters, ending in `...` when it was cut."""
    return text if len(text) <= width else text[: width - 3] + "..."


def model_label(state: tunestate.TuneSession, source: str, name: str) -> str:
    """`source/name`, or `source/base (tag)` when `name` is a tag built from another model.

    Hal's role tags (`hal-chat:latest` and the like) say nothing about the weights behind them,
    so the model they were built from leads and the tag follows in brackets.
    """
    info = state.find(source, name)
    base = info.base if info else ""
    if not base or base == name:
        return f"{source}/{name}"
    return f"{source}/{base} ({name.removesuffix(':latest')})"


def hal_models() -> dict[str, str]:
    """Role -> gateway model set straight in `[hal]` (`chat_model` and the like), read once."""
    hal = halconfig.hal()
    return {role: str(hal.get(f"{role}_model") or "") for role in modelconf.ROLES if hal.get(f"{role}_model")}


def role_line(
    state: tunestate.TuneSession, role: str, look: Look, direct: Mapping[str, str] | None = None
) -> tuple[str, str, str]:
    """`(model, context, fit text)` of one role, for the summary and the role menu.

    `direct` is `hal_models()` read once by a screen that redraws often; without it `[hal]` is read here.
    """
    d = state.drafts.get(role)
    if d is None or not d.model:
        direct = str((hal_models() if direct is None else direct).get(role) or "")
        if direct:
            # a gateway `backend/model` (such as a llama.cpp router preset) is set in `[hal]`, and
            # `[models.roles]` cannot describe it, so the screen shows it without tuning it
            return direct, "", "set in [hal] (not tuned)"
        return "-", "", "falls back to chat" if role == "writer" else "no model yet"
    fit = state.fit(role)
    ctx = f"{fit.context // 1024}K" if fit.context else ""
    return model_label(state, d.source, d.model), ctx, fit_text(fit, look)


def guard_line() -> tuple[str, str]:
    """`(model, note)` of the tool-call risk classifier, which `policy.toml` sets, not this screen.

    Shown so the summary names every model Hal runs; `hal tune` does not change it.
    """
    try:
        settings = load_policy().classifier
    except (halconfig.ConfigError, ValueError, OSError) as exc:
        return "?", f"policy.toml unreadable ({type(exc).__name__})"
    if settings.paranoia == "off":
        return "-", "safety check off (paranoia off)"
    where = "CPU" if not settings.gpu else "GPU when free"
    return settings.model, f"safety check, {where}"


open_state = tunestate.open_state  # one place builds the session; kept here for the screens
