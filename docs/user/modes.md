# Modes

A mode is a file of llama.cpp presets that the gateway's managed router (`llama-server` in router
mode) loads. Hal ships none. `hal tune` writes the one you normally use, `tuned`, with a preset per
role served from a GGUF file; you set its numbers in the tune screen, never by hand. Presets load
on their first request. When the presets' files, with 40 percent for cache and buffers, do not fit
the GPU together, `tuned` sets `models-max = 1` and the router swaps them: the first request to
another role unloads the last one (a 27B coder to a 31B writer took 11 to 16 s on an RTX 3090,
2026-11-11). This page says
where modes live and how to write your own, and keeps the measurements that guide a choice of
GGUF.

The old `max` profile and its bundled modes (`gpu`, `gpu-112k`, `hybrid`, `hybrid-auto`,
`combined`, `orchestrate` and their 256K variants) were removed in 0.2.0, together with the
`hal-serve` launcher. Each of them is a set of choices `hal tune` now makes per role: the model
file, the context, the cache type, GPU layers or experts in RAM. See
[models.md](models.md#the-tune-screen).

## Where modes live

The gateway looks in `[gateway.backends.NAME].modes_dirs` (default `~/.config/hal/modes`), and the
router runs the mode named by `mode` in that backend's table. `hal tune` points it at `tuned`
when it wires a GGUF role. List and check the modes with:

```bash
hal-gateway modes            # every mode in the mode folders
hal-gateway render tuned     # render one after the security checks, without starting it
```

## Write your own mode

A mode you write in the mode folder works like `tuned`: an INI file whose sections are presets,
with `llama-server` flags as keys without the leading dashes, and a `[hal]` section that names the
presets the gateway serves as `hal/main` and `hal/agent`. Every weight file must be pinned in the
model manifest (`hal models assign` or `hal tune` pin them), and keys that would open the server
to the network are refused. The full key list is in
[design 05, "Mode files"](../design/05-gateway.md). Prefer `hal tune`: a mode you write is not
sized against your card for you.

## Measurements

All figures were measured on 2026-10-08 on an RTX 3090 (24GB), an i9-12900K and 64GB of
dual-channel DDR4, with llama.cpp build `b11512`, flash attention and a q8_0 KV cache.

### Quantisation

These measurements chose the quantisations of the old bundled modes; they still hold as a guide
for picking a GGUF in `hal tune`. Quality was measured to find the best speed for a memory budget
without a measurable loss on code. It was measured as perplexity on 560KB of Python from this
portfolio of repositories, text no model was trained on. Perplexity compares quantisations
of one model; it can't compare different models, which use different tokenisers.

The following table lists the quantisations tried:

| Model | Quantisation | Size | Code perplexity | Decode | Result |
| --- | --- | --- | --- | --- | --- |
| Qwen3.8-27B | UD-Q4_K_XL | 16.3GiB | 1.9661 | 41 tokens/s | Used, with room for the MTP head |
| Qwen3.8-27B | UD-Q5_K_XL | 19.5GiB | 1.9651 | 40 tokens/s | MTP fits only at 64K context |
| Qwen3.8-27B | UD-Q6_K | 20.5GiB | 1.9643 | - | No room for the MTP head |
| Laguna-S-2.1 (118B) | UD-IQ3_S | 45.1GiB | 2.1474 | 5.0 tokens/s in RAM | Slower than the 177B coder |
| Laguna-S-2.1 (118B) | UD-Q3_K_XL | 50.4GiB | 2.1246 | 4.1 tokens/s in RAM | 17% slower for a 1% gain |
| Laguna-S-2.1 (118B) | UD-Q4_K_XL | 68GiB | - | 12.2 tokens/s hybrid | Slower than Flash-Next |
| Laguna-S-2.1 (118B) | MXFP4_MOE | 66GiB | - | 15.0 tokens/s hybrid | Slower than Flash-Next |
| Qwen3.8-Flash-Next | GSQ IQ2_XS | 63GiB | - | 20.6 tokens/s hybrid | 5% faster, 4% lower task score |
| Qwen3.8-Flash-Next | GSQ IQ3_XXS | 71GiB | - | 19.7, or 25.2 with MTP | Best hybrid choice |
| Flash-Next-Coder | GSQ IQ1_M | 54GiB | - | 5.7 tokens/s in RAM | Best CPU choice |

Qwen3.8-27B is within noise across the three quantisations, so the smallest one wins: it
leaves VRAM for the multi-token-prediction (MTP) draft head, which doubles decode speed
(41 to 83 tokens/s, with 84% of drafted tokens accepted on code). An MTP draft for
Flash-Next lifts hybrid decode by 28%, with 89% accepted.

A 12-problem private Python benchmark (functions with hidden unit tests, no thinking)
checks that each chosen model writes working code at its quantisation:

| Setup | Passed | Time |
| --- | --- | --- |
| Qwen3.8-27B on the GPU | 11/12 | 1 minute |
| Flash-Next-Coder in RAM | 11/12 | 17 minutes |
| Qwen3.8-Flash-Next, GPU and RAM | 10/12 | 3 minutes |

Laguna-S-2.1 at UD-IQ3_S passed 10/12 in RAM. The benchmark is too small to rank
these models against each other; it confirms that none is broken by quantisation.

### Placement

Four settings decide most of the speed:

- **CPU decode is bound by memory bandwidth.** Dual-channel DDR4 delivers about 26GB/s,
  so speed follows the bytes of active weights read per token. That is why the 177B coder
  (10 small experts active) decodes faster in RAM than the 118B Laguna (8B active).
- **The GPU does prompt processing for the RAM-resident model.** With `n-gpu-layers = 0`,
  llama.cpp copies each batch's weights to the GPU. A ubatch of 2048 lifts prefill from 45
  to 416 tokens/s and uses 2.8GB of VRAM; a ubatch of 1024 gives 211 tokens/s in 1.9GB.
- **Keep attention on the GPU, move experts.** A mixture-of-experts model decodes fastest with
  every attention layer and as many experts as fit in VRAM and the remaining experts in RAM,
  which is what **RAM experts** in `hal tune` sets.
- **Context costs little on these models.** Only one layer in four uses full attention,
  so a 128K window takes about 4GB of KV cache on the 27B and 1.6GB on Flash-Next.

Thread pinning made no difference: 8 or 16 threads, pinned to the performance cores or
not, all decode at 4.4 to 5.1 tokens/s on Laguna-S-2.1.

