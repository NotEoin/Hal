"""Tuning maths: how much context a model gets on this machine, and where its memory goes.

Pure functions over `Hardware` and `ModelInfo`; no I/O. The formulas, a worked example and the
measurements behind the constants are in docs/dev/tuning.md. In short:

    on the GPU = weights on the GPU + KV cache + runtime overhead
    overhead   = CUDA_CONTEXT_MIB per GPU + the output buffer (UBATCH x vocab x 4 bytes)
                 + the attention mask (UBATCH x context x 2 bytes)
                 + one layer's cache as f16 over the context, when the cache is quantised
    kv/token   = 2 x layers x kv_heads x head_dim x bytes(kv type)
                 (only for the full-attention layers; sliding-window layers stop at the window,
                 linear-attention layers of hybrid models cost nothing here)
    a setting fits the GPU when "on the GPU" is at most the cards' total memory

Two limits, one for each way a setting is made:

- `plan_model` (auto-tune) stays within the guidance budget: total memory less a headroom per
  card (`headroom_mib`), so a desktop sharing the card keeps some room.
- `place` checks settings the user made by hand against the whole card: a headless GPU needs no
  headroom, and the user may take the risk. Past the guidance a reasoning line says so; past the
  card, layers (or experts) spill to RAM, which works but is slower.

Design decisions:

- Plans use total memory, VRAM and RAM alike, never a momentary free reading: Hal's own gateway
  or another program holding memory today would otherwise shrink every role to nothing.
  `busy_warning` tells the user when the card is not free right now.
- What spills to RAM: a dense model moves whole layers (weights and their share of the cache,
  llama.cpp `--n-gpu-layers`). A mixture-of-experts model keeps attention, the cache and the
  shared weights on the GPU and moves the routed experts of some layers (`--n-cpu-moe`), which
  decodes far faster than moving whole layers because only a few experts run per token.
- The input embedding of a model with a separate output matrix stays in RAM (llama.cpp keeps
  `token_embd` on the host), so it is not counted on the GPU (`ModelInfo.host_bytes`); nor is a
  multi-token-prediction layer, which llama.cpp loads only for a draft (`ModelInfo.idle_bytes`).
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
SPILL_CONTEXT = 16384  # auto context when a dense model spills layers: prompt processing on the CPU is slow
# auto context when a mixture-of-experts model spills experts: its cache stays on the GPU, so prompt
# processing stays fast and a coding-sized context is worth a few more experts in RAM
MOE_SPILL_CONTEXT = 32768
# memory held by other programs counts as "busy" above 2 GiB and 10 percent of the card; below that
# it is the desktop compositor and the like, which the headroom already covers
BUSY_MIN_MIB = 2048
BUSY_FRACTION = 0.10
HEADROOM_MIN_MIB = 1024
HEADROOM_FRACTION = 0.08
# CUDA context, cuBLAS workspace and llama.cpp's graph buffers besides the output buffer, per GPU.
# Measured on an RTX 3090 (driver 580, llama.cpp b11512, flash attention, default batch sizes):
# see docs/dev/tuning.md "Measurements".
CUDA_CONTEXT_MIB = 180
UBATCH = 512  # llama.cpp's default `--ubatch-size`; the output buffer holds this many rows of logits
LOGIT_BYTES = 4  # f32
MASK_BYTES = 2  # f16 attention mask, UBATCH rows of the whole context
F16_BYTES = 2  # a quantised cache is dequantised to f16, one layer at a time, for flash attention
DEFAULT_VOCAB = 152064  # a vocabulary size when the model does not say (Qwen 2.5); errs large
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
    weights_mib: int = 0  # the whole model, wherever it runs
    kv_mib: int = 0
    budget_mib: int = 0  # the guidance auto-tune stays within: total less headroom
    estimated: bool = False  # True when the architecture was unknown
    reasoning: tuple[str, ...] = field(default_factory=tuple)
    total_mib: int = 0  # the cards' total memory: the limit of a setting made by hand
    overhead_mib: int = 0  # runtime memory on the GPU besides weights and cache
    vram_mib: int = 0  # what the model takes on the GPUs: weights there, cache and overhead
    ram_mib: int = 0  # weights (and cache) held in system RAM
    cpu_moe: int = 0  # layers whose routed experts stay in RAM (llama.cpp `--n-cpu-moe`)

    def to_dict(self) -> dict[str, object]:
        """A JSON-ready view."""
        data = dict(self.__dict__)
        data["tensor_split"] = list(self.tensor_split)
        data["reasoning"] = list(self.reasoning)
        return data


def headroom_mib(total_mib: int, override_mib: int = 0) -> int:
    """VRAM auto-tune keeps free per GPU: `override_mib` when set, else the larger of 1 GiB and 8 percent."""
    if override_mib > 0:
        return override_mib
    return max(HEADROOM_MIN_MIB, math.ceil(total_mib * HEADROOM_FRACTION))


def usable_mib(gpu: Gpu, override_mib: int = 0) -> int:
    """Memory of one GPU auto-tune may fill: total memory less headroom.

    Total, not free: free memory is a momentary reading and the plan must not change with
    whatever else runs on the card (see `busy_warning`).
    """
    return max(gpu.total_mib - headroom_mib(gpu.total_mib, override_mib), 0)


def ram_usable_mib(hw: Hardware) -> int:
    """System RAM a model may use: total RAM less a headroom for the OS, the desktop and Hal."""
    keep = max(RAM_HEADROOM_MIN_MIB, math.ceil(hw.ram_total_mib * RAM_HEADROOM_FRACTION))
    return max(hw.ram_total_mib - keep, 0)


def gpu_busy_mib(gpu: Gpu) -> int:
    """Memory of `gpu` that something else holds, when that is more than `BUSY_MIN_MIB`; else 0."""
    held = gpu.total_mib - gpu.free_mib
    return held if held > max(BUSY_MIN_MIB, gpu.total_mib * BUSY_FRACTION) else 0


def busy_warning(hw: Hardware) -> str:
    """One line saying something holds GPU memory now and the plan counts the whole card; "" if not."""
    held = sum(gpu_busy_mib(g) for g in hw.gpus)
    if not held:
        return ""
    return (
        f"{held / 1024:.1f} GB of the GPU is in use right now (Hal's own models count); "
        "plans count the whole card, so free it before loading a model that needs it"
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


def _overhead_per_token(model: ModelInfo, kv_type: str) -> float:
    """Bytes of runtime overhead per token of context: the mask, and the f16 copy of a quantised cache."""
    width = (model.kv_heads or 0) * (model.head_dim or 0)
    return UBATCH * MASK_BYTES + (2 * width * F16_BYTES if kv_type != "f16" else 0)


def overhead_mib(model: ModelInfo, gpus: int = 1, context: int = 0, kv_type: str = "f16") -> int:
    """Runtime memory on the GPUs besides weights and cache.

    CUDA context per GPU, the output buffer, the attention mask and, for a quantised cache, one
    layer's keys and values as f16 over the whole context.
    """
    if gpus < 1:
        return 0
    logits = UBATCH * (model.vocab or DEFAULT_VOCAB) * LOGIT_BYTES
    return CUDA_CONTEXT_MIB * gpus + math.ceil((logits + context * _overhead_per_token(model, kv_type)) / MIB)


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
    if kv_type not in KV_BYTES:
        raise ValueError(f"unknown KV cache type {kv_type!r}; use one of {KV_TYPES}")
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


def ceiling(model: ModelInfo, cap: int = 0) -> int:
    """The largest context the model takes: its native maximum, lowered to `cap` when `cap` is set."""
    native = model.context_native or cap or DEFAULT_CONTEXT_CAP
    return min(cap, native) if cap else native


@dataclass(frozen=True)
class _Shape:
    """The memory facts of one model at one context and cache type, in MiB."""

    weights: int  # the whole file
    host: int  # stays in RAM whatever the offload
    gpu_weights: int  # weights on the GPU with every layer there
    kv: int
    overhead: int
    layers: int
    experts: int  # routed expert weights over all layers; 0 for a dense model
    estimated: bool
    notes: tuple[str, ...]

    @property
    def full(self) -> int:
        """GPU memory with everything on the GPU."""
        return self.gpu_weights + self.kv + self.overhead


def _shape(model: ModelInfo, kv_type: str, context: int, gpus: int) -> _Shape:
    weights = _weights_mib(model)
    per_tok, fixed, layers, estimated = _kv_per_token(model, kv_type, weights)
    host = min(math.ceil((model.host_bytes or 0) / MIB), weights)
    idle = min(math.ceil((model.idle_bytes or 0) / MIB), weights - host)
    notes = ()
    if estimated:
        notes = (
            f"architecture unknown: KV estimated at {per_tok / 1024:.0f} KiB/token "
            f"({UNKNOWN_KV_KIB_PER_GIB} KiB per GiB of weights at f16), layers assumed {layers}",
        )
    experts = math.ceil(model.expert_bytes / MIB) if model.is_moe and model.expert_bytes else 0
    return _Shape(weights, host, weights - host - idle, _kv_mib(context, per_tok, fixed),
                  overhead_mib(model, gpus, context, kv_type), layers, experts, estimated, notes)  # fmt: skip


def place(
    model: ModelInfo,
    hw: Hardware,
    *,
    kv_type: str,
    context: int,
    gpu_layers: int | None = None,
    cpu_moe: int | None = None,
    headroom_override_mib: int = 0,
    limit_mib: int | None = None,
) -> Plan:
    """Where `model` runs with `context` tokens of `kv_type` cache; the check behind every setting.

    `gpu_layers` (a count, or `ALL_LAYERS`) and `cpu_moe` (layers whose experts stay in RAM) are
    the user's when given; when both are None the offload is chosen to fit `limit_mib` of VRAM
    (default: the whole card). The status is `gpu` with everything on the GPU, `hybrid` when
    layers or experts are in RAM, `cpu` with no layer on a GPU, and `does-not-fit` when the GPU
    part passes the card or the rest passes usable RAM. Raises `ValueError` for an unknown cache
    type or weight size, a context below `MIN_CONTEXT`, layers set on a model whose layer count
    is unknown, or experts set on a model that has none.
    """
    if context < MIN_CONTEXT:
        raise ValueError(f"a context below {MIN_CONTEXT} tokens cannot hold a useful chat")
    if gpu_layers is not None and gpu_layers != ALL_LAYERS and not model.layers:
        raise ValueError(f"{model.name}: the layer count is unknown, so GPU layers cannot be set by hand")
    if cpu_moe is not None and not model.is_moe:
        raise ValueError(f"{model.name} is not a mixture-of-experts model, so experts cannot be moved to RAM")
    gpus = [g for g in hw.gpus if g.total_mib > 0]
    s = _shape(model, kv_type, context, len(gpus))
    total = sum(g.total_mib for g in gpus)
    budget = sum(usable_mib(g, headroom_override_mib) for g in gpus)
    limit = total if limit_mib is None else limit_mib
    ram = ram_usable_mib(hw)
    why = list(s.notes)
    why.append(f"weights {s.weights} MiB + KV {s.kv} MiB for {context} tokens + runtime {s.overhead} MiB")
    layers, moe = ALL_LAYERS, 0
    if not gpus:
        layers, vram, in_ram = NO_GPU_LAYERS, 0, s.weights + s.kv
        why.append("no GPU: every layer runs on the CPU")
    elif cpu_moe is not None or (gpu_layers is None and s.experts and s.full > limit):
        moe = _moe_layers(s, limit) if cpu_moe is None else min(max(cpu_moe, 0), s.layers)
        moved = math.ceil(s.experts * moe / s.layers)
        vram, in_ram = s.full - moved, s.host + moved
        if moe:
            why.append(
                f"the experts of {moe} of {s.layers} layers ({moved} MiB) stay in RAM; "
                "attention and cache stay on the GPU"
            )
    else:
        if gpu_layers is None:
            per_layer = (s.gpu_weights + s.kv) / s.layers
            on_gpu = (
                s.layers
                if s.full <= limit
                else min(max(int((limit - s.overhead) // per_layer), 0), s.layers - 1)
            )
        else:
            on_gpu = s.layers if gpu_layers == ALL_LAYERS else min(max(gpu_layers, 0), s.layers)
        per_layer = (s.gpu_weights + s.kv) / s.layers
        vram = math.ceil(on_gpu * per_layer) + (s.overhead if on_gpu else 0)
        in_ram = s.host + math.ceil((s.layers - on_gpu) * per_layer)
        layers = ALL_LAYERS if on_gpu == s.layers else on_gpu
        if on_gpu < s.layers:
            why.append(
                f"{on_gpu} of {s.layers} layers on the GPU; the other {s.layers - on_gpu} run from RAM"
            )
    if gpus:
        why.append(
            f"{vram} MiB of the {total} MiB GPU"
            + (f" (auto-tune keeps to {budget})" if budget < total else "")
        )
        if budget < vram <= total:
            why.append(f"above the auto-tune guidance, within the card: {ABOVE_NOTE}")
    if layers == NO_GPU_LAYERS:
        status, vram = "cpu", 0
    elif layers == ALL_LAYERS and not moe:
        status = "gpu"
    else:
        status = "hybrid"
    if in_ram:
        why.append(f"{in_ram} MiB in RAM ({ram} MiB usable of {hw.ram_total_mib})")
    if vram > total:
        status = "does-not-fit"
        why.append(f"VRAM is {vram - total} MiB short: put fewer layers on the GPU, or a smaller context")
    elif in_ram > ram:
        status = "does-not-fit"
        why.append(f"RAM is {in_ram - ram} MiB short: use a smaller context or model")
    split = _split(gpus, headroom_override_mib) if status in ("gpu", "hybrid") else ()
    return Plan(
        model.name, status, context, kv_type, layers, split, s.weights, s.kv, budget, s.estimated, tuple(why),
        total, s.overhead if vram else 0, vram, in_ram, moe,
    )  # fmt: skip


def _moe_layers(s: _Shape, limit: int) -> int:
    """The fewest layers whose experts must leave the GPU for `s` to fit `limit`."""
    per_layer = s.experts / s.layers
    if s.full <= limit or per_layer <= 0:
        return 0
    return min(math.ceil((s.full - limit) / per_layer), s.layers)


def _plan_once(model: ModelInfo, hw: Hardware, kv_type: str, cap: int, headroom: int) -> Plan:
    """The automatic plan for one cache type: the most context within the guidance budget."""
    weights = _weights_mib(model)
    per_tok, fixed, _, _ = _kv_per_token(model, kv_type, weights)
    top = ceiling(model, cap)
    gpus = [g for g in hw.gpus if g.total_mib > 0]
    budget = sum(usable_mib(g, headroom) for g in gpus)
    if gpus:
        on_gpu = _shape(model, kv_type, MIN_CONTEXT, len(gpus)).gpu_weights
        spare = budget - on_gpu - overhead_mib(model, len(gpus), 0, kv_type)
        per_token = per_tok + _overhead_per_token(model, kv_type)
        ctx = min(_context_for(spare, per_token, fixed), top) if spare > 0 else 0
        if ctx >= MIN_CONTEXT:
            plan = place(
                model, hw, kv_type=kv_type, context=ctx, headroom_override_mib=headroom, limit_mib=budget
            )
            return _with(plan, f"the most context within the {budget} MiB guidance: {ctx} tokens (cap {top})")
        goal = MOE_SPILL_CONTEXT if model.is_moe else SPILL_CONTEXT
        ctx = max(round_context(min(goal, top)), MIN_CONTEXT)
        plan = place(
            model, hw, kv_type=kv_type, context=ctx, headroom_override_mib=headroom, limit_mib=budget
        )
        return _with(
            plan, f"the weights do not fit the {budget} MiB guidance: {plan.context} tokens, the rest in RAM"
        )
    ctx = round_context(min(SPILL_CONTEXT, top))
    ram = ram_usable_mib(hw)
    if weights + _kv_mib(ctx, per_tok, fixed) > ram and ram > weights:
        ctx = min(ctx, _context_for(ram - weights, per_tok, fixed))
    if ctx < MIN_CONTEXT:
        return place(model, hw, kv_type=kv_type, context=MIN_CONTEXT, headroom_override_mib=headroom)
    return place(model, hw, kv_type=kv_type, context=ctx, headroom_override_mib=headroom)


def _with(plan: Plan, line: str) -> Plan:
    return Plan(**{**plan.__dict__, "reasoning": (*plan.reasoning, line)})


def plan_model(
    model: ModelInfo,
    hw: Hardware,
    *,
    kv_type: str = "auto",
    context_cap: int = DEFAULT_CONTEXT_CAP,
    headroom_override_mib: int = 0,
) -> Plan:
    """The automatic plan for `model` on `hw`: the most context that fits the guidance budget.

    `kv_type` is `f16`, `q8_0`, `q4_0` or `auto`. `auto` takes the most precise type that gives
    `AUTO_CONTEXT_GOAL` tokens (or the model's ceiling, if lower) and otherwise the type that
    gives the most context. Raises `ValueError` for an unknown cache type or unknown weight size.
    """
    if kv_type != "auto":
        return _plan_once(model, hw, kv_type, context_cap, headroom_override_mib)
    goal = min(AUTO_CONTEXT_GOAL, ceiling(model, context_cap))
    best: Plan | None = None
    for kind in KV_TYPES:
        plan = _plan_once(model, hw, kind, context_cap, headroom_override_mib)
        if plan.status == "gpu" and plan.context >= goal:
            return plan
        if best is None or _better(plan, best):
            best = plan
    assert best is not None  # KV_TYPES is not empty
    return best


def _better(plan: Plan, than: Plan) -> bool:
    """True when `plan` fits where `than` does not, or gives more context.

    A tie keeps `than`, the earlier and so more precise cache type.
    """
    return (plan.status != "does-not-fit", plan.context) > (than.status != "does-not-fit", than.context)
