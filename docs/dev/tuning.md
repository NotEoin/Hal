# Tuning: how Hal sizes context and offload

Code: `halkit.tuning` (pure maths), `halkit.hardware` (detection), `halkit.gguf` and
`halkit.discovery` (model facts).

## The formula

All sizes are MiB unless stated.

```text
on the GPU = weights on the GPU + KV cache + runtime overhead
weights on the GPU = file size - host - idle
    host = token_embd when the model has a separate output.weight (llama.cpp keeps it in RAM)
    idle = the multi-token-prediction (nextn) layers, loaded only for a draft
kv/token   = 2 x layers x kv_heads x head_dim x bytes(kv type)     # bytes
overhead   = 180 x GPUs                                  # CUDA context, cuBLAS, graph buffers
           + 512 x vocab x 4 bytes                       # the output (logits) buffer
           + context x (512 x 2 bytes                    # the attention mask
                        + 2 x kv_heads x head_dim x 2)   # f16 copy of one layer, quantised cache only
```

`bytes(kv type)`: f16 = 2, q8_0 = 34/32, q4_0 = 18/32 (ggml block sizes). 512 is llama.cpp's
default `--ubatch-size`. The file size, the `token_embd` and `output.weight` sizes, the routed
expert sizes and the vocabulary come from the GGUF header and tensor table (`halkit.gguf`); for
an Ollama model only the file size and `model_info` are known, so `host` and `idle` are 0 and
the estimate errs high.

Two limits:

```text
total    = sum of each GPU's total memory               # a setting made by hand may use all of it
headroom = max(1024, ceil(0.08 x total)) per GPU        # [models] headroom_mib overrides it
budget   = total - headroom                             # auto-tune stays within this
```

Auto-tune (`tuning.plan_model`) picks the most context whose need fits the budget, rounded down to
a multiple of 1024 and capped at the model's native maximum and at `[models] context_cap`. A
setting made by hand (`tuning.place`) is checked against `total`: past the budget a reasoning line
says so, past the card the model spills to RAM. `context_cap` does not limit a hand setting; the
model's native maximum does.

### Measurements

Measured on an RTX 3090 (24576 MiB, driver 580.178, CUDA 13.0) with llama.cpp b11512
(`llama-server -fa on -ngl 99 -np 1`, default batch sizes), on 2026-11-11. "Measured" is
`nvidia-smi` memory.used after the server loaded and ran a 3000-token prompt, less the 15 MiB the
idle desktop held. The estimate is `tuning.place` on the same files.

| Model | Context | Cache | Estimated | Measured | Difference |
| --- | --- | --- | --- | --- | --- |
| Llama 3.1 8B Instruct Q8_0 | 8192 | f16 | 9076 | 9070 | +6 |
| Llama 3.1 8B Instruct Q8_0 | 65536 | f16 | 16300 | 16294 | +6 |
| Llama 3.1 8B Instruct Q8_0 | 8192 | q8_0 | 8628 | 8592 | +36 |
| Qwen3.8 27B UD-Q4_K_XL | 32768 | q8_0 | 17641 | 17546 | +95 |
| Qwen3.8 27B UD-Q4_K_XL | 131072 | q8_0 | 21385 | 21290 | +95 |
| Gemma 4 12B QAT Q4_0 | 32768 | f16 | 8209 | 8102 | +107 |
| Gemma 4 12B QAT Q4_0 | 8192 | f16 | 7801 | 7694 | +107 |

The 180 MiB per GPU was fitted to the first row; the other rows check it. The estimate stays a
little high (at most 1.3 percent here), never low. `packages/hal-kit/tests/test_tuning.py` keeps
the Llama rows as a regression check. Other cards, drivers and llama.cpp builds are unverified:
to check one, load a model with `llama-server` as above and compare `nvidia-smi` with the
reasoning lines of `hal tune`.

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
at the full context.

`kv_type = auto` tries f16, q8_0, q4_0 in that order and keeps the first that gives
`min(32768, ceiling)` tokens on the GPU; otherwise it keeps the type with the most context.

### Worked example: 24 GB card, 12B Q4 model

Card: 24564 MiB total. Model: 7000 MiB of weights, 48 layers, 8 KV heads, head size 128,
native context 131072, vocabulary unknown (taken as 152064).

```text
headroom = max(1024, ceil(0.08 x 24564) = 1966) = 1966
budget   = 24564 - 1966 = 22598
fixed    = 180 + ceil(512 x 152064 x 4 / 1048576) = 180 + 297 = 477
spare    = 22598 - 7000 - 477 = 15121
f16      : per token = 196608 (cache) + 1024 (mask) = 197632 B
           15121 x 1048576 / 197632 = 80228 tokens -> 79872
on GPU   = 7000 + 14976 (cache) + 555 (overhead) = 22531 MiB of 24564
```

With `kv_type = auto` the plan is f16 with 79872 tokens.

### Total memory, not free memory

Both limits start from total memory, VRAM and RAM alike. Free memory is a momentary reading:
while Hal's own gateway, Ollama or a game holds the card, planning on it would give every role
zero GPU layers and a tiny context, and the same goes for RAM a running model holds. When more
than 2 GiB and 10 % of a card is in use, `hal tune` shows one note (`tuning.busy_warning`)
saying how much, and that plans count the whole card.

### What spills to RAM

When the model does not fit, auto-tune sets the context first (16384 for a dense model, 32768
for a mixture-of-experts model, or the model's ceiling if lower), then moves the least it can:

```text
dense:  per_layer = (weights on the GPU + kv) / layers
        gpu_layers = floor((limit - overhead) / per_layer)          # at most layers - 1
MoE:    per_layer_experts = routed expert bytes / layers
        cpu_moe = ceil((on the GPU - limit) / per_layer_experts)    # llama.cpp --n-cpu-moe
```

A dense model moves whole layers, cache included. A mixture-of-experts model keeps attention,
the cache and the shared weights on the GPU and moves only the routed experts of `cpu_moe`
layers, which decodes much faster because a token runs only a few experts (the bundled
`hybrid` modes do the same with llama.cpp's own fit). The part in RAM must fit the total RAM
less `max(2048, 10 %)`, else the status is `does-not-fit`. No GPU gives `cpu`.

### Several GPUs

The budget is the sum over the devices. llama.cpp is given a tensor split proportional to
each device's budget, and every device in use pays the 180 MiB context.

## Settings made by hand

The tune screen (`halcore.tuneapp`, `halcore.tuneui`; state in `halcore.tunestate`) lets the
user set the context, the KV type, the GPU layers or, for a mixture-of-experts GGUF, the layers
whose experts stay in RAM. Each setting makes the draft manual and stays as set: the plan is
`tuning.place` with exactly those numbers against the whole card, and anything the user did not
set (the offload, when only the context was set) is chosen to fit the card. Only an Auto button,
Auto-tune or a preset hands a field back to tuning. The screen calls `place` for every value it
previews, so the fit shown is the same arithmetic as the plan that gets written.

`TuneSession.layers_of` and `experts_of` say when those sliders cannot be used (no model, a
server-sized model, an unknown layer count, a dense model, a plan with no GPU). A model the
server sizes (an OpenAI-compatible server such as vLLM) has no plan, but its context can be
declared; it is written as the role's `context` so the gateway can report the window.
`TuneSession.snapshot` and `restore` copy every draft and the source edits, which is what undo
in the tune screen uses.

## How the settings reach the server

`modelplan.apply` writes the role tables and then:

- a GGUF role becomes a preset of the generated llama.cpp mode `tuned` (`halgateway.tunedmode`):
  `ctx-size`, `n-gpu-layers`, `n-cpu-moe`, the cache types and `fit = off`, so llama.cpp keeps
  the numbers instead of re-fitting them;
- an Ollama role is served through `hal-tuned-ROLE`, an Ollama model made with `/api/create`
  from the chosen model plus `num_ctx` (and `num_gpu` when layers spill). Ollama's OpenAI route
  ignores a context sent with a request (checked with Ollama 0.40.1), so this is the only way
  the tuned context reaches it. It is a manifest over the same weights, not a copy, and the
  model pickers leave it out. Ollama applies `OLLAMA_KV_CACHE_TYPE` to every model, so the
  role's `kv_type` only sizes the plan;
- a role on a server-sized model keeps the context the user declared.

## Presets

`halcore.tunepresets.PRESETS` lists the Quick setup budgets. `TuneSession.apply_preset` swaps the
session hardware for `hardware_for(preset, real)` (a card with equal total and free memory, or
no GPU), keeps each role's model if it still runs fully on that hardware, and otherwise calls
`modelplan.choose` for the best discovered model. Roles stay unwritten until the user saves.
When no discovered model fits, the note names `tunepresets.recommended(preset, role, real)`, a
tag from `RECOMMENDED`; `tier_for` maps *This machine* to the largest tier the card covers,
counting a card as its nominal size when it reports at least 95% of it.

## Limits

- The per-layer cost of a dense model is the average. Embedding and output tensors are larger
  than a layer, so the real GPU share of a partial offload is a little different.
- Sliding-window and hybrid-attention models are costed from the metadata keys above. The small
  fixed state of linear-attention layers is not counted, and a model that hides those keys is
  planned as full attention, which is conservative.
- The overhead was measured on one card and one llama.cpp build (see Measurements). Ollama
  runs its own build and placement, so its numbers are estimates.
- Each role is planned as if it were the only model loaded. The gateway and gpu-light swap
  roles, so footprints are not added up.
- An unknown architecture (a server that exposes no metadata) gets 64 KiB of f16 KV per
  token per GiB of weights, 32 layers, and the plan says "architecture unknown".
- The plan uses total memory, so it does not see what other programs need. `hal tune`
  notes when something holds a large part of the card at the time it runs.
