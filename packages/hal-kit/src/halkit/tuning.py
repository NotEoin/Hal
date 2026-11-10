"""Tuning maths: how much context a model gets on this machine, and how much of it goes on the GPU.

Pure functions over `Hardware` and `ModelInfo`; no I/O. The formulas and their limits are in
docs/dev/tuning.md. In short, per GPU:

    usable   = total_mib - max(1024, 8 % of total_mib) - RUNTIME_OVERHEAD_MIB
    budget   = sum of usable over all GPUs
    kv/token = 2 x layers x kv_heads x head_dim x bytes(kv type)
               (only for the full-attention layers; sliding-window layers stop at the window,
               linear-attention layers of hybrid models cost nothing here)
    context  = floor((budget - weights) / kv/token), rounded down to 1024, clamped to the
               model's native maximum and to the configured cap

The plan uses the card's total memory, not its momentary free memory: another program that
holds the card today (a big model, a game) would otherwise shrink every role to nothing.
`gpu_busy_mib` and `busy_warning` tell the user when the card is not free.

When the weights alone do not fit, layers are offloaded one at a time (hybrid) and the rest of
the model lives in system RAM. Without a GPU the plan is CPU-only. An unknown architecture
gets a deliberately pessimistic KV estimate and the plan says so.

Design decisions:

- The budget (total minus headroom) is guidance: `plan_model`, which auto-tune uses, stays within
  it. A context or layer count the user sets (`fit_context`, `fit_layers`) may use the whole card
  less the runtime overhead (`card_mib`): a headless GPU needs no headroom, and the user may
  accept the risk. `Plan.card_mib` carries that limit and `Plan.total_mib` the card's size, so the
  screens can show both. A setting beyond the card still spills layers to RAM (hybrid).
- Each role is planned as if it were the only model loaded; the gateway and gpu-light swap
  roles in and out, so their footprints are not added up.
- Memory sizes are MiB throughout; `KV_BYTES` are bytes per element.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .hardware import Gpu, Hardware
from .modelinfo import ModelInfo

MIB = 1024 * 1024
KV_TYPES = ("f16", "q8_0", "q4_0")
# bytes per cache element; ggml packs q8_0 as 34 bytes per 32 values and q4_0 as 18 per 32
# (ggml `block_q8_0` and `block_q4_0`)
KV_BYTES = {"f16": 2.0, "q8_0": 34 / 32, "q4_0": 18 / 32}
CONTEXT_STEP = 1024
MIN_CONTEXT = 2048  # below this a model cannot hold a useful chat; the plan falls back to offload
DEFAULT_CONTEXT_CAP = 131072
AUTO_CONTEXT_GOAL = 32768  # `kv_type = auto` keeps the most precise cache that still gives this much
SPILL_CONTEXT = (
    16384  # context aimed at when weights spill to RAM or run on CPU: prompt processing there is slow
)
# memory held by other programs counts as "busy" above 2 GiB and 10 percent of the card; below that
# it is the desktop compositor and the like, which the headroom already covers
BUSY_MIN_MIB = 2048
BUSY_FRACTION = 0.10
HEADROOM_MIN_MIB = 1024
HEADROOM_FRACTION = 0.08
# CUDA/ROCm context (about 300 MiB) plus llama.cpp compute buffers at the default batch sizes
# (about 300 MiB), per GPU in use. Rounded up from llama.cpp logs seen with 7B to 30B models
# [unverified: measure with `llama-server` logs on your card]
RUNTIME_OVERHEAD_MIB = 600
ABOVE_NOTE = "fine on a headless GPU, but a desktop on the card may run out of memory"
RAM_HEADROOM_MIN_MIB = 2048  # the OS, the desktop and Hal itself
RAM_HEADROOM_FRACTION = 0.10
UNKNOWN_LAYERS = 32  # a typical 7B to 13B model
UNKNOWN_KV_KIB_PER_GIB = 64  # f16 KV per token per GiB of weights; about twice a Llama-3 8B (28 KiB)
NO_GPU_LAYERS = 0
ALL_LAYERS = -1  # the llama.cpp value for "everything on the GPU" is large; -1 here, mapped by the caller


@dataclass(frozen=True)
class Plan:
    """The settings for one model and the numbers behind them."""

    model: str
    status: str  # "gpu", "hybrid", "cpu" or "does-not-fit"
    context: int
    kv_type: str
    gpu_layers: int  # ALL_LAYERS, a count, or 0
    tensor_split: tuple[float, ...] = ()  # share per GPU, only with 2 or more GPUs
    weights_mib: int = 0
    kv_mib: int = 0
    budget_mib: int = 0
    estimated: bool = False  # True when the architecture was unknown
    reasoning: tuple[str, ...] = field(default_factory=tuple)
    card_mib: int = 0  # what a setting made by hand may use: the cards less runtime overhead
    total_mib: int = 0  # the cards' total memory, for the gauge's full scale

    def to_dict(self) -> dict[str, object]:
        """A JSON-ready view."""
        data = dict(self.__dict__)
        data["tensor_split"] = list(self.tensor_split)
        data["reasoning"] = list(self.reasoning)
        return data


def headroom_mib(total_mib: int, override_mib: int = 0) -> int:
    """VRAM kept free per GPU: `override_mib` when set, else the larger of 1 GiB and 8 percent."""
    if override_mib > 0:
        return override_mib
    return max(HEADROOM_MIN_MIB, math.ceil(total_mib * HEADROOM_FRACTION))


def usable_mib(gpu: Gpu, override_mib: int = 0) -> int:
    """Memory of one GPU a model may use: total memory minus headroom and runtime overhead.

    Total, not free: free memory is a momentary reading and the plan must not change with
    whatever else runs on the card (see `busy_warning`).
    """
    return max(gpu.total_mib - headroom_mib(gpu.total_mib, override_mib) - RUNTIME_OVERHEAD_MIB, 0)


def card_mib(gpu: Gpu) -> int:
    """Memory of one GPU a setting made by hand may use: total memory less runtime overhead, no headroom."""
    return max(gpu.total_mib - RUNTIME_OVERHEAD_MIB, 0)


def _cards(hw: Hardware) -> tuple[int, int]:
    """`(card limit, total memory)` summed over every GPU."""
    return sum(card_mib(g) for g in hw.gpus), sum(g.total_mib for g in hw.gpus)


def gpu_busy_mib(gpu: Gpu) -> int:
    """Memory of `gpu` that something else holds, when that is more than `BUSY_MIN_MIB`; else 0."""
    held = gpu.total_mib - gpu.free_mib
    return held if held > max(BUSY_MIN_MIB, gpu.total_mib * BUSY_FRACTION) else 0


def busy_warning(hw: Hardware) -> str:
    """One line saying other programs hold GPU memory and the plan assumes it is free; "" if none."""
    held = sum(gpu_busy_mib(g) for g in hw.gpus)
    if not held:
        return ""
    return (
        f"another program is using {held / 1024:.1f} GB of the GPU; "
        "the plan assumes it is free, so stop it before loading a model"
    )


def kv_bytes_per_token(layers: int, kv_heads: int, head_dim: int, kv_type: str) -> float:
    """`2 x layers x kv_heads x head_dim x bytes(kv_type)`: the key and the value of every layer."""
    if kv_type not in KV_BYTES:
        raise ValueError(f"unknown KV cache type {kv_type!r}; use one of {KV_TYPES}")
    return 2 * layers * kv_heads * head_dim * KV_BYTES[kv_type]


def attention_layers(layers: int, interval: int | None) -> tuple[int, int]:
    """`(full, other)` layer counts when every `interval`-th layer is global (or full) attention."""
    if not interval or interval < 2 or interval > layers:
        return layers, 0
    full = layers // interval
    return full, layers - full


def kv_bytes(
    layers: int,
    kv_heads: int,
    head_dim: int,
    kv_type: str,
    context: int,
    *,
    window: int | None = None,
    interval: int | None = None,
) -> float:
    """KV bytes for `context` tokens: full layers x context + sliding layers x min(context, window).

    Without `interval` every layer is full attention. With `interval` but no `window` the other
    layers are linear attention and hold no per-token cache (a hybrid such as Qwen3.5).
    """
    full, other = attention_layers(layers, interval)
    per_layer = kv_bytes_per_token(1, kv_heads, head_dim, kv_type)
    return per_layer * (full * context + (other * min(context, window) if window else 0))


def round_context(tokens: float) -> int:
    """Rounds down to a multiple of `CONTEXT_STEP`."""
    return int(tokens // CONTEXT_STEP) * CONTEXT_STEP


def _weights_mib(model: ModelInfo) -> int:
    if not model.size_bytes:
        raise ValueError(f"{model.name}: the size of the weights is unknown, so no plan can be made")
    return math.ceil(model.size_bytes / MIB)


def _kv_per_token(model: ModelInfo, kv_type: str, weights_mib: int) -> tuple[float, float, int, bool]:
    """`(bytes per token, fixed bytes, layers, estimated)`: KV is `per_token * context + fixed`.

    `fixed` is the full window of the sliding layers; it is exact once the context passes the
    window, and a slight over-estimate below it. The estimate for an unknown model is pessimistic
    and flagged.
    """
    if model.kv_full_width is not None and model.layers:
        size = KV_BYTES[kv_type]
        window = model.sliding_window or 0
        return model.kv_full_width * size, (model.kv_window_width or 0) * size * window, model.layers, False
    if model.has_architecture:
        layers, heads, dim = int(model.layers or 0), int(model.kv_heads or 0), int(model.head_dim or 0)
        full, other = attention_layers(layers, model.attention_interval)
        per_layer = kv_bytes_per_token(1, heads, dim, kv_type)
        fixed = per_layer * other * model.sliding_window if model.sliding_window else 0.0
        return per_layer * full, fixed, layers, False
    layers = int(model.layers or UNKNOWN_LAYERS)
    f16 = UNKNOWN_KV_KIB_PER_GIB * 1024 * (weights_mib / 1024)
    return f16 * KV_BYTES[kv_type] / KV_BYTES["f16"], 0.0, layers, True


def _kv_mib(context: int, per_tok: float, fixed: float) -> int:
    return math.ceil((context * per_tok + fixed) / MIB)


def _context_for(spare_mib: float, per_tok: float, fixed: float) -> int:
    """Tokens of KV that fit `spare_mib` once the fixed sliding-window part is paid."""
    return round_context(max(spare_mib * MIB - fixed, 0) / per_tok)


def _split(gpus: list[Gpu], override: int) -> tuple[float, ...]:
    shares = [usable_mib(g, override) for g in gpus]
    total = sum(shares)
    if len(gpus) < 2 or total == 0:
        return ()
    return tuple(round(s / total, 3) for s in shares)


def _ceiling(model: ModelInfo, cap: int) -> int:
    return min(cap, model.context_native or cap)


def _plan_once(model: ModelInfo, hw: Hardware, kv_type: str, cap: int, headroom_override: int) -> Plan:
    weights = _weights_mib(model)
    per_tok, fixed, layers, estimated = _kv_per_token(model, kv_type, weights)
    ceiling = _ceiling(model, cap)
    notes = []
    if estimated:
        notes.append(
            f"architecture unknown: KV estimated at {per_tok / 1024:.0f} KiB/token "
            f"({UNKNOWN_KV_KIB_PER_GIB} KiB per GiB of weights at f16), layers assumed {layers}"
        )
    gpus = [g for g in hw.gpus if usable_mib(g, headroom_override) > 0]
    budget = sum(usable_mib(g, headroom_override) for g in gpus)
    ram = max(
        hw.ram_free_mib - max(RAM_HEADROOM_MIN_MIB, math.ceil(hw.ram_total_mib * RAM_HEADROOM_FRACTION)), 0
    )
    split = _split(gpus, headroom_override)

    def make(status: str, ctx: int, gpu_layers: int, why: list[str]) -> Plan:
        kv = _kv_mib(ctx, per_tok, fixed)
        return Plan(
            model.name,
            status,
            ctx,
            kv_type,
            gpu_layers,
            split,
            weights,
            kv,
            budget,
            estimated,
            tuple(notes + why),
            *_cards(hw),
        )

    if gpus:
        spare = budget - weights
        if spare > 0:
            ctx = min(_context_for(spare, per_tok, fixed), ceiling)
            if ctx >= MIN_CONTEXT:
                return make(
                    "gpu",
                    ctx,
                    ALL_LAYERS,
                    [
                        f"budget {budget} MiB - weights {weights} MiB = {spare} MiB for the KV cache",
                        f"{spare} MiB / {per_tok / 1024:.1f} KiB per token = {ctx} tokens (cap {ceiling})",
                    ],
                )
        # hybrid: pick the context first, then as many layers as fit beside its KV cache
        ctx = round_context(min(SPILL_CONTEXT, ceiling))
        if ctx < MIN_CONTEXT:
            ctx = round_context(ceiling) or CONTEXT_STEP
        per_layer = (weights + _kv_mib(ctx, per_tok, fixed)) / layers
        n_gpu = min(int(budget // per_layer), layers - 1)
        cpu_part = (layers - n_gpu) * per_layer
        why = [
            f"weights {weights} MiB do not leave {MIN_CONTEXT} tokens of KV in {budget} MiB of VRAM",
            f"a layer costs {per_layer:.0f} MiB with {ctx} tokens of KV: {n_gpu} of {layers} layers fit",
            f"the other {layers - n_gpu} layers ({cpu_part:.0f} MiB) go to system RAM ({ram} MiB usable)",
        ]
        if cpu_part > ram:
            return make(
                "does-not-fit",
                0,
                0,
                why + [f"RAM is {cpu_part - ram:.0f} MiB short: use a smaller model or quant"],
            )
        if n_gpu < 1:
            why.append("no layer fits on the GPU: running on the CPU")
            return make("cpu", ctx, NO_GPU_LAYERS, why)
        return make("hybrid", ctx, n_gpu, why)
    ctx = round_context(min(SPILL_CONTEXT, ceiling))
    need = weights + _kv_mib(ctx, per_tok, fixed)
    why = [
        "no usable GPU: CPU only",
        f"weights {weights} MiB + KV {need - weights} MiB = {need} MiB of {ram} MiB usable RAM",
    ]
    if need > ram:
        spare = ram - weights
        ctx = _context_for(spare, per_tok, fixed) if spare > 0 else 0
        if ctx < MIN_CONTEXT:
            return make(
                "does-not-fit", 0, 0, why + [f"RAM is {need - ram} MiB short: use a smaller model or quant"]
            )
        why.append(f"context lowered to {ctx} tokens to fit")
    return make("cpu", ctx, NO_GPU_LAYERS, why)


def plan_model(
    model: ModelInfo,
    hw: Hardware,
    *,
    kv_type: str = "auto",
    context_cap: int = DEFAULT_CONTEXT_CAP,
    headroom_override_mib: int = 0,
) -> Plan:
    """Plans `model` on `hw`.

    `kv_type` is `f16`, `q8_0`, `q4_0` or `auto`. `auto` takes the most precise type that gives
    `AUTO_CONTEXT_GOAL` tokens (or the model's ceiling, if lower) and otherwise the type that
    gives the most context. Raises `ValueError` for an unknown cache type or unknown weight size.
    """
    if kv_type != "auto":
        return _plan_once(model, hw, kv_type, context_cap, headroom_override_mib)
    goal = min(AUTO_CONTEXT_GOAL, _ceiling(model, context_cap))
    best: Plan | None = None
    for kind in KV_TYPES:
        plan = _plan_once(model, hw, kind, context_cap, headroom_override_mib)
        if plan.status == "gpu" and plan.context >= goal:
            return plan
        if best is None or plan.context > best.context:
            best = plan
    assert best is not None  # KV_TYPES is not empty
    return best


def fit_context(
    model: ModelInfo, hw: Hardware, *, kv_type: str, context: int, headroom_override_mib: int = 0
) -> Plan:
    """Where `model` runs with exactly `context` tokens of `kv_type` cache: the check behind a chosen context.

    The status is `gpu` when weights and cache fit the VRAM budget, `hybrid` when layers move to
    RAM, `cpu` when no layer fits on a GPU or there is no GPU, and `does-not-fit` when RAM is short
    too. Raises `ValueError` for an unknown cache type, an unknown weight size or a context below
    `MIN_CONTEXT`.
    """
    if context < MIN_CONTEXT:
        raise ValueError(f"a context below {MIN_CONTEXT} tokens cannot hold a useful chat")
    weights = _weights_mib(model)
    per_tok, fixed, layers, estimated = _kv_per_token(model, kv_type, weights)
    kv = _kv_mib(context, per_tok, fixed)
    gpus = [g for g in hw.gpus if card_mib(g) > 0]
    budget = sum(usable_mib(g, headroom_override_mib) for g in gpus)
    card, total = _cards(hw)
    ram_keep = max(RAM_HEADROOM_MIN_MIB, math.ceil(hw.ram_total_mib * RAM_HEADROOM_FRACTION))
    ram = max(hw.ram_free_mib - ram_keep, 0)
    why = [f"weights {weights} MiB + KV {kv} MiB for {context} tokens = {weights + kv} MiB"]
    status, n_gpu, in_ram = "cpu", NO_GPU_LAYERS, weights + kv
    if gpus and weights + kv <= budget:
        status, n_gpu, in_ram = "gpu", ALL_LAYERS, 0
        why.append(f"fits the {budget} MiB VRAM guidance")
    elif gpus and weights + kv <= card:
        status, n_gpu, in_ram = "gpu", ALL_LAYERS, 0
        why.append(f"above the {budget} MiB guidance, within the {card} MiB the card holds: {ABOVE_NOTE}")
    elif gpus:
        per_layer = (weights + kv) / layers
        n_gpu = min(int(card // per_layer), layers - 1)
        in_ram = math.ceil((layers - n_gpu) * per_layer)
        why.append(f"{n_gpu} of {layers} layers fit the {card} MiB the card holds; the rest go to RAM")
        status = "hybrid" if n_gpu >= 1 else "cpu"
        n_gpu = max(n_gpu, NO_GPU_LAYERS)
    else:
        why.append("no usable GPU: CPU only")
    if status != "gpu" and in_ram > ram:
        status = "does-not-fit"
        why.append(f"RAM is {in_ram - ram} MiB short ({ram} MiB usable): use a smaller context or model")
    split = _split(gpus, headroom_override_mib) if status in ("gpu", "hybrid") else ()
    return Plan(
        model.name, status, context, kv_type, n_gpu, split, weights, kv, budget, estimated, tuple(why),
        card, total,
    )  # fmt: skip


def fit_layers(
    model: ModelInfo,
    hw: Hardware,
    *,
    kv_type: str,
    context: int,
    gpu_layers: int,
    headroom_override_mib: int = 0,
) -> Plan:
    """Where `model` runs with `context` tokens of cache and exactly `gpu_layers` layers on the GPU.

    The check behind a GPU layer count set by hand. `gpu_layers` is `ALL_LAYERS` or a count from 0
    to the model's layers. A layer holds its share of the weights and of the cache. The status is
    `gpu` with every layer on the GPU, `hybrid` with some, `cpu` with none, and `does-not-fit` when
    the layers asked for overrun the VRAM budget or the rest overruns RAM. Raises `ValueError` for
    an unknown layer count, weight size or cache type, or a context below `MIN_CONTEXT`.
    """
    if context < MIN_CONTEXT:
        raise ValueError(f"a context below {MIN_CONTEXT} tokens cannot hold a useful chat")
    if not model.layers:
        raise ValueError(f"{model.name}: the layer count is unknown, so GPU layers cannot be set by hand")
    weights = _weights_mib(model)
    per_tok, fixed, layers, estimated = _kv_per_token(model, kv_type, weights)
    layers = int(model.layers)
    kv = _kv_mib(context, per_tok, fixed)
    gpus = [g for g in hw.gpus if card_mib(g) > 0]
    budget = sum(usable_mib(g, headroom_override_mib) for g in gpus)
    card, total = _cards(hw)
    ram = max(
        hw.ram_free_mib - max(RAM_HEADROOM_MIN_MIB, math.ceil(hw.ram_total_mib * RAM_HEADROOM_FRACTION)), 0
    )
    on_gpu = layers if gpu_layers < 0 or gpu_layers >= layers else gpu_layers
    if not gpus:
        on_gpu = NO_GPU_LAYERS
    per_layer = (weights + kv) / layers
    in_vram, in_ram = math.ceil(on_gpu * per_layer), math.ceil((layers - on_gpu) * per_layer)
    total = weights + kv
    why = [
        f"weights {weights} MiB + KV {kv} MiB for {context} tokens = {total} MiB, {per_layer:.0f} MiB a layer"
    ]
    if not gpus:
        why.append("no usable GPU: every layer runs on the CPU")
    else:
        why.append(f"{on_gpu} of {layers} layers on the GPU use {in_vram} MiB of VRAM (guidance {budget})")
        if budget < in_vram <= card:
            why.append(f"above the guidance, within the {card} MiB the card holds: {ABOVE_NOTE}")
    if on_gpu == layers:
        status = "gpu"
    else:
        status = "hybrid" if on_gpu else "cpu"
        why.append(f"the other {layers - on_gpu} layers use {in_ram} MiB of RAM ({ram} MiB usable)")
    if in_vram > card:
        status = "does-not-fit"
        why.append(f"VRAM is {in_vram - card} MiB short: put fewer layers on the GPU")
    elif in_ram > ram:
        status = "does-not-fit"
        why.append(f"RAM is {in_ram - ram} MiB short: put more layers on the GPU or use a smaller model")
    shown_layers = ALL_LAYERS if on_gpu == layers else on_gpu
    split = _split(gpus, headroom_override_mib) if status in ("gpu", "hybrid") else ()
    return Plan(
        model.name, status, context, kv_type, shown_layers, split, weights, kv, budget, estimated, tuple(why),
        card, total,
    )  # fmt: skip
