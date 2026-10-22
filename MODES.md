# Modes

The `max` profile runs Hal on three local coding models in five modes, so you can trade
speed for model size and context per launch. This page covers what each mode runs, how to
switch between them, how combined mode routes work between its models, and the
measurements behind each choice. It assumes a 24GB GPU and 64GB of RAM.

## Choose a mode

Each mode places its models differently. The following table summarises them:

| Mode | Model | Where it runs | Context | Decode, short / 32K deep | Prefill |
| --- | --- | --- | --- | --- | --- |
| `gpu` | Qwen3.8-27B | VRAM | 96K | 83 / 60 tokens/s | 1,120 tokens/s |
| `gpu-112k` | Qwen3.8-27B | VRAM, headless GPU | 112K | - / 70 tokens/s at 31K | 1,140 tokens/s |
| `cpu` | Qwen3.8-Flash-Next-Coder, 177B | RAM | 128K | 5.7 / 4.6 tokens/s | 360 tokens/s |
| `hybrid` | Qwen3.8-Flash-Next, 177B | VRAM and RAM | 128K | 25 / 18.5 tokens/s | 300 tokens/s |
| `hybrid-256k` | Qwen3.8-Flash-Next, 177B | VRAM and RAM | 256K | 25 / 16 tokens/s | 300 tokens/s |
| `combined` | Flash-Next-Coder plans, Qwen3.8-27B executes | RAM and VRAM | 64K | per model, as above | per model, as above |
| `hybrid-auto` | Qwen3.8-Flash-Next, 177B | VRAM and RAM | 128K | - / 24 tokens/s at 28K | 509 tokens/s |
| `hybrid-auto-256k` | as `hybrid-auto` | VRAM and RAM | 256K | - / 17 to 19 tokens/s at 28K | 470 tokens/s |
| `orchestrate` | Flash-Next plans, Qwen3.8-27B executes, taking turns on the GPU | VRAM and RAM | 128K | 24 and 70 tokens/s at 28K | 509 and 1,140 tokens/s |
| `orchestrate-256k` | as `orchestrate` | VRAM and RAM | 256K | 17 to 19 and 70 tokens/s at 28K | 470 and 1,140 tokens/s |

Decode is generation speed on code, at the start of a conversation and with 32,000 tokens
already in context, which is where a Claude Code session starts: its system prompt and tool
definitions are about 32,000 tokens. Prefill is prompt processing, so the first reply of a
session waits 30 seconds on `gpu` and about 2 minutes on the others. Later replies reuse
the cached prompt.

The two hybrid modes hold about 50GB of experts in RAM, which leaves little for the rest of
a 64GB desktop. When other applications push them out, the kernel reads experts back from
disk and decode slows; `hybrid-256k` gives VRAM to its larger cache and keeps more in RAM,
so it feels this first. Close memory-heavy applications while you use them.

The same task, run end to end in Claude Code (find and fix a failing test, then re-run the
suite), took the following times:

| Mode | Time | Notes |
| --- | --- | --- |
| `gpu` | 2 minutes | |
| `hybrid-256k` | 10 minutes | |
| `hybrid` | 16 minutes | More thinking than the 256K run on the same task |
| `cpu` | 29 minutes | |
| `combined` | 2 minutes | The classifier sent the turn to the 27B |

- `gpu` is the default and the fastest. Use it for most coding work.
- `cpu` runs a model six times larger in system RAM. It writes slowly, so it suits a
  question where you want a careful answer and can wait.
- `hybrid` runs the strongest model by splitting it across both: attention and part of
  the experts sit in VRAM, and the rest of the experts in RAM.
- `hybrid-256k` is `hybrid` with the model's full context window, for work across a
  large codebase or a long session.
- `combined` loads the `cpu` and `gpu` models together and a small classifier that picks
  which of them takes each request. See [Combined mode](#combined-mode).

The published agentic coding scores for these models, measured by their vendors in Claude
Code at full precision with a 256K context, are as follows (retrieved 2026-10-08):

| Model | SWE-bench Pro | DeepSWE 1.1 | Other | At Hal's quantisation |
| --- | --- | --- | --- | --- |
| Qwen3.8-27B | 61.7 | 42.2 | Terminal Bench 2.1: 73.0 | Within noise on code perplexity |
| Qwen3.8-Flash-Next | 62.5 | 58.7 | SWE-bench Multilingual: 81.0 | IQ3_XXS keeps 99.4% of the task average |
| Qwen3.8-Flash-Next-Coder | - | - | SWE-bench Verified: 82.8 | IQ1_M scores 75.6 (91.3% retained) |

The two Flash-Next models lead the 27B most on long multi-step work (DeepSWE), and least on
SWE-bench Pro. The quantisation figures come from the
[ISTA-DASLab](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF)
model cards.

## Switch modes

Pick a mode for one launch with `hal mode`. Add `here` to work in the current directory
instead of the vault; any further arguments go to Claude Code:

```bash
hal mode gpu                # Qwen3.8-27B on the GPU, in the vault
hal mode hybrid here        # Qwen3.8-Flash-Next across GPU and RAM, in this directory
hal mode combined here -c   # combined mode, continuing the last session here
```

A plain `hal` uses `HAL_MODE` from `hal.conf`, which is `gpu` after setup. The model
server keeps running after Claude Code exits, so the next launch in the same mode starts
at once. Starting a different mode stops the server and loads that mode's models: about
20 seconds for `gpu`, and a few minutes for the others from a cold disk.

Manage the server directly with the following commands:

```bash
hal serve hybrid      # load a mode without opening Claude Code
hal status            # show the mode being served and each model's state
hal stop              # stop the server and free the VRAM and RAM
```

`hal ask`, `hal brief` and `hal summarize` use the same server, and write with the faster
model of the mode.

## Combined mode

Combined mode loads three models: the 177B coder in RAM (`hal-orchestrator`), the 27B on
the GPU (`hal-agent`), and Kev-0.8B (`hal-router`), a decision model that answers
multiple-choice questions in one forward pass. `hal-route.py` sits between Claude Code and
the model server and handles each request as follows:

1. On each message you send, it asks Kev whether the task needs the fast model
   (routine commands, reading code, small edits) or the strong one (hard debugging,
   design, multi-file refactors, subtle reasoning). The answer takes about 0.4 seconds
   and holds for every request of that turn.
2. A fast turn goes to the 27B with all of Claude Code's tools.
3. A strong turn goes to the 177B with its shell and file-editing tools removed and
   `modes/combined-prompt.txt` added to its system prompt. It plans and decides, and has
   to start subagents for every command and edit.
4. Subagents, and Claude Code's small side requests such as titles and permission
   checks, always run on the 27B.

Every decision is logged with its probability in `~/.local/state/hal/route.log`. Measured
on the bug-fix task above, combined mode took 2 minutes with routing (the 27B did the
turn), against 43 minutes when the 177B handled every request itself. On a design task
(add a plug-in registry to a module, refactor onto it and test it), the classifier chose
the 177B, which made 12 requests while its subagents made 50, and finished in 31 minutes
with every test passing.

Kev-0.8B was chosen from three open decision models on 12 hand-labelled coding requests,
run on the CPU:

| Model | Size | Correct | Median time |
| --- | --- | --- | --- |
| Kev-0.8B | 0.8GB | 12/12 | 0.44s |
| Kev-4B | 3.0GB | 12/12 | 1.9s |
| Laya | 0.4GB | 6/12, every request to the strong model | 0.25s |

Claude Code applies one context limit to the main model and its subagents, so the mode
runs at the 27B's 64K window.

## Hybrid-auto mode

`hybrid-auto` runs the `hybrid` model with two changes for long unattended sessions:

- **Thinking follows the turn.** The chat template defaults to xhigh reasoning on every
  request, so reading a file costs as much thought as a design decision. Here the server
  thinks at medium, and `hal-route.py` (policy `effort`) adds the template's xhigh note as
  a system message at the end of the prompt when the request follows a user message, a
  subagent report, a tool error or a failing shell command. The template puts its own
  effort note at the start of the system prompt, where changing it would discard the
  whole prompt cache; at the end it changes only the last few tokens. Each decision is
  logged in `route.log`.
- **The whole GPU is used.** With no desktop on the GPU, a fit margin of 0 keeps one more
  expert tensor in VRAM (peak 23.3GB on a 28K prefill), and loading without mmap keeps
  experts in RAM instead of reading them back from disk. At 28K deep, prefill rose from
  375 to 509 tokens/s and decode from 21.7 to 24.2 tokens/s. If a desktop uses the GPU,
  raise `fit-target` to 1024.

For long unattended runs (`/goal`), use `hybrid-auto-256k` or `orchestrate-256k`. Claude
Code compacts Hal's models through its reactive path, which skips a compaction when it
finds no finished turn to summarise or when the context refills soon after the last one,
and it refuses to send a request about 10K tokens above its compaction threshold. A 128K
window refills quickly after a compaction and leaves that 10K for one long turn of tool
calls; a `/goal` run on `hybrid-auto` stopped this way ("context limit reached") after one
successful compaction, while `hybrid-256k` compacted four times in 34 hours.

`hybrid-auto-256k` is the same with the full 256K window: Claude Code compacts half as
often, but the extra KV cache takes VRAM from experts. At 28K deep it decodes at 17 to 19
tokens/s against about 22 to 24 for `hybrid-auto`, and prefills at 470 tokens/s against 509,
and it slows further deep into a long session.

## Orchestrate modes

`orchestrate` and `orchestrate-256k` let the 177B plan while the 27B does the work, with
each model taking the whole GPU in turn. `hal-route.py` (policy `swap`) handles requests as
follows:

1. The main conversation goes to the 177B (`hal-hybrid`) with its shell and file-editing
   tools removed and `modes/orchestrate-prompt.txt` added, so it delegates commands and
   edits to subagents.
2. Subagents go to the 27B (`hal-agent`). Side requests such as titles go to whichever
   model is loaded, so they never cause a swap.
3. When a request needs the other model, the proxy waits until the loaded one is idle,
   saves the 177B's KV cache (`slot-save-path`), unloads one model and loads the other.
   The 177B waits until every running subagent has finished, so the models don't swap
   back and forth while subagents work. Moving to the 27B takes about 17 seconds; moving
   back takes about 50, including 3 seconds to restore a 29K-token cache, which saves
   reprocessing the conversation.
4. Both models think at medium, with the xhigh note of `hybrid-auto` added at a turn's
   decision points; for the 177B a subagent's report is one of them.

The router runs with `--models-max 1 --no-models-autoload`, so only the proxy loads
models. Claude Code's context limit comes from the 177B (128K or 256K); subagents start
from a fresh context, and the 27B holds 112K, the most that fits beside its MTP head. If a
subagent still overflows, the proxy returns the error Claude Code compacts on. Requests can
wait for subagents and swaps, so `hal` raises Claude Code's API timeout to two hours. Every
swap is logged with its duration in `route.log`.

`orchestrate-256k` gives the 177B its full window; the extra 128K of KV cache takes VRAM from
experts, so the 177B decodes 20 to 25% slower, as in `hybrid-auto-256k`.

## How it works

`hal-serve` runs llama.cpp's `llama-server` in router mode on port 8080. Each mode is an
INI file of presets in `modes/`, one section per model, holding the same flags you would
pass to `llama-server`. The router loads every model in the file at startup, except presets
with `load-on-startup = false`, and routes each request by its `model` field. In the
`combined`, `hybrid-auto*` and `orchestrate*` modes `hal-serve` also starts `hal-route.py` on
port 8079, and Claude Code talks to that instead.

The launcher points Claude Code at the server and names the models:

| Variable | Other modes | `combined` |
| --- | --- | --- |
| `ANTHROPIC_MODEL` | the mode's model | `hal-orchestrator` |
| `CLAUDE_CODE_SUBAGENT_MODEL` | the mode's model | `hal-agent` |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | the mode's model | `hal-agent` |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | the mode's `ctx-size` | the smallest `ctx-size` |

Two compatibility details matter. Claude Code doesn't know these models, so without
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` it assumes a 200K window and compacts too late. And it
sends system messages in the middle of a conversation, which the stock Qwen3.8 chat
template rejects; `modes/qwen3.8-chat.jinja` is that template with the check replaced by a
system turn.

Ollama keeps serving the embedding model for `hal ask` and `hal index`. Before loading a
mode, `hal-serve` unloads any Ollama model still holding VRAM.

With gpu-light installed, `hal-serve` also claims the GPU light before it loads a mode, and
waits up to 2 minutes for background GPU jobs to leave; `hal stop` releases the claim. See
[SETUP.md](SETUP.md#sharing-the-gpu-with-background-jobs).

## Measurements

All figures were measured on 2026-10-08 on an RTX 3090 (24GB), an i9-12900K and 64GB of
dual-channel DDR4, with llama.cpp build `b11512`, flash attention and a q8_0 KV cache.

### Quantisation

Each mode uses the quantisation that gave the best speed for its memory budget without a
measurable loss on code. Quality was measured as perplexity on 560KB of Python from this
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
| Qwen3.8-Flash-Next | GSQ IQ3_XXS | 71GiB | - | 19.7, or 25.2 with MTP | Used for `hybrid` |
| Flash-Next-Coder | GSQ IQ1_M | 54GiB | - | 5.7 tokens/s in RAM | Used for `cpu` |

Qwen3.8-27B is within noise across the three quantisations, so the smallest one wins: it
leaves VRAM for the multi-token-prediction (MTP) draft head, which doubles decode speed
(41 to 83 tokens/s, with 84% of drafted tokens accepted on code). An MTP draft for
Flash-Next lifts hybrid decode by 28%, with 89% accepted.

A 12-problem private Python benchmark (functions with hidden unit tests, no thinking)
checks that each chosen model writes working code at its quantisation:

| Mode | Passed | Time |
| --- | --- | --- |
| `gpu` | 11/12 | 1 minute |
| `cpu` | 11/12 | 17 minutes |
| `hybrid` | 10/12 | 3 minutes |

Laguna-S-2.1 at UD-IQ3_S passed 10/12 in RAM. The benchmark is too small to rank
these models against each other; it confirms that none is broken by quantisation.

### Placement

Four settings decide most of the speed:

- **CPU decode is bound by memory bandwidth.** Dual-channel DDR4 delivers about 26GB/s,
  so speed follows the bytes of active weights read per token. That is why the 177B coder
  (10 small experts active) decodes faster in RAM than the 118B Laguna (8B active).
- **The GPU does prompt processing for the RAM-resident model.** With `n-gpu-layers = 0`,
  llama.cpp copies each batch's weights to the GPU. A ubatch of 2048 lifts prefill from 45
  to 416 tokens/s and uses 2.8GB of VRAM; combined mode uses a ubatch of 1024 (211
  tokens/s, 1.9GB) to leave the 27B room.
- **The hybrid split is automatic.** The `hybrid` presets set no layer counts, so
  llama.cpp's fit keeps every attention layer and as many experts as fit in VRAM (with a
  1GB margin), and the remaining experts in RAM.
- **Context costs little on these models.** Only one layer in four uses full attention,
  so a 128K window takes about 4GB of KV cache on the 27B and 1.6GB on Flash-Next.

Thread pinning made no difference: 8 or 16 threads, pinned to the performance cores or
not, all decode at 4.4 to 5.1 tokens/s on Laguna-S-2.1.

## Change a model

Edit the mode's file in `modes/`, then restart the server with `hal stop`. Keys are
`llama-server` flags without the leading dashes; `@MODELS@` stands for `HAL_MODELS_DIR`
and `@HAL@` for this repository. Download other GGUFs into `HAL_MODELS_DIR` with
`hf download`, for example:

```bash
hf download unsloth/Qwen3.8-27B-GGUF Qwen3.8-27B-UD-Q5_K_XL.gguf \
  --local-dir "$HAL_MODELS_DIR/qwen3.8-27b"
```
