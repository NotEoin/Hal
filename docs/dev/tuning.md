# Tuning: how Hal sizes context and offload

Code: `halkit.tuning` (pure maths), `halkit.hardware` (detection), `halkit.gguf` and
`halkit.discovery` (model facts).

## The formula

All sizes are MiB unless stated. For each GPU:

```text
headroom = max(1024, ceil(0.08 x total))        # [models] headroom_mib overrides it
usable   = max(free - headroom - 600, 0)        # 600 = RUNTIME_OVERHEAD_MIB
budget   = sum of usable over all GPUs
kv/token = 2 x layers x kv_heads x head_dim x bytes(kv type)     # bytes
context  = floor((budget - weights) x 1048576 / kv/token), rounded down to 1024,
           clamped to the model's native maximum and to [models] context_cap
```

`bytes(kv type)`: f16 = 2, q8_0 = 34/32, q4_0 = 18/32 (ggml block sizes). `weights` is the
size of the file on disk (Ollama: the blob size).

### Sliding-window and hybrid attention

Not every layer keeps a cache for the whole context. Discovery reads three optional GGUF keys
(Ollama `model_info` has the same names) into `ModelInfo`:

| Key | Field | Meaning |
| --- | --- | --- |
| `<arch>.attention.sliding_window` | `sliding_window` | tokens a local layer attends to |
| `<arch>.attention.sliding_window_pattern` | `attention_interval` | every Nth layer is global (Gemma) |
| `<arch>.full_attention_interval` | `attention_interval` | every Nth layer is full attention (Qwen3.5 hybrids) |

With an interval, `layers // interval` layers are full attention. The rest are sliding-window
layers (cost: `min(context, window)` tokens) or, with no window, linear-attention layers that
hold a small fixed state, which Hal does not count. A Gemma GGUF with a window but no pattern
key is taken as 5 local layers to 1 global. When the keys are missing, every layer is costed
at the full context, as before.

`kv_type = auto` tries f16, q8_0, q4_0 in that order and keeps the first that gives
`min(32768, ceiling)` tokens; otherwise it keeps the type with the most context.

### Worked example: 24 GB card, 12B Q4 model

Card: 24564 MiB total, 24000 MiB free. Model: 7000 MiB of weights, 48 layers, 8 KV heads,
head size 128, native context 131072.

```text
headroom = max(1024, ceil(0.08 x 24564) = 1966) = 1966
usable   = 24000 - 1966 - 600 = 21434          # the budget, one card
spare    = 21434 - 7000 = 14434
f16      : kv/token = 2 x 48 x 8 x 128 x 2 = 196608 B (192 KiB)
           14434 x 1048576 / 196608 = 76981 tokens -> 76800
q8_0     : 104448 B per token -> 144899 -> clamped to 131072
q4_0     : 55296 B per token  -> clamped to 131072
```

With `kv_type = auto` the plan is f16 with 76800 tokens (14400 MiB of KV).

### Weights that do not fit

If `spare` gives under 2048 tokens, the plan is hybrid. The context is set first
(16384, or the model's ceiling if lower), then layers are counted:

```text
per_layer = (weights + context x kv/token) / layers
gpu_layers = floor(budget / per_layer)           # at most layers - 1
```

The remaining layers must fit in free RAM minus `max(2048, 10 %)` of total RAM, else the
plan is `does-not-fit`. Zero GPU layers, or no GPU, gives `cpu`.

### Several GPUs

The budget is the sum of the usable memory of every device. llama.cpp is given a tensor
split proportional to each device's usable memory. A device with no usable memory is left out.

## A chosen context

The tune screen (`halcore.tuneui`, state in `halcore.tunestate`) lets the user fix the context.
`tuning.fit_context` then checks that exact number: weights plus `context x kv/token` against the
VRAM budget gives `gpu`; otherwise layers move to RAM as in the hybrid case; with no GPU it is
`cpu`; and `does-not-fit` when RAM is short too. The screen calls it for every preset it lists, so
the fit shown is the same arithmetic as the automatic plan.

## Limits

- The per-layer cost is the average. Embedding and output tensors are larger than a layer, so
  the real GPU share is a little smaller than computed.
- Sliding-window and hybrid-attention models need less KV than the formula says; the plan is
  conservative for them.
- `RUNTIME_OVERHEAD_MIB` is an estimate from llama.cpp logs of 7B to 30B models, not a
  measurement on your card. Unverified: compare `llama-server`'s logged buffer sizes.
- Each role is planned as if it were the only model loaded. The gateway and gpu-light swap
  roles, so footprints are not added up.
- An unknown architecture (a server that exposes no metadata) gets 64 KiB of f16 KV per
  token per GiB of weights, 32 layers, and the plan says "architecture unknown".
- Free memory is read once; another program using the GPU later is not seen. Re-run
  `hal tune` after changing what shares the GPU.
