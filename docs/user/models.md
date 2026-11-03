# Models: sources, roles, tuning and effort

Hal does not ship or download models. You point it at models you already have, it reads
your hardware, and it works out settings that make the most of both.

## Sources

A source is a place models come from. Add as many as you like in
`~/.config/hal/config.toml`.

| Kind | What it is | Keys |
| --- | --- | --- |
| `ollama` | An Ollama server | `url` (default `http://127.0.0.1:11434`) |
| `openai` | Any OpenAI-compatible server: LM Studio, llama.cpp server, vLLM | `url` (default `http://127.0.0.1:1234/v1`), optional `api_key_file` |
| `gguf` | A folder of GGUF files served by Hal's llama.cpp router | `path` |
| `anthropic` | The Anthropic API | `api_key_file` |
| `openai-cloud` | The OpenAI API | `api_key_file` |

```toml
[models.sources.local]
kind = "ollama"
url = "http://127.0.0.1:11434"

[models.sources.lmstudio]
kind = "openai"
url = "http://127.0.0.1:1234/v1"
api_key_file = "~/.config/hal/lmstudio.key"   # optional

[models.sources.weights]
kind = "gguf"
path = "~/models"
```

Rules:

- A key is never written in the config. Put it in a file with mode `0600` and name the file
  in `api_key_file`. A config with `api_key`, `key` or `token` is refused.
- A URL must be on this machine (`127.0.0.1`, `::1`, `localhost`). A server on another host
  needs `allow_remote = true` and an `https` URL.

## Roles

A role names the job a model does: `chat` (core Hal), `fast`, `code`, `writer`, `vision`
and `embed`. Each role has a source, a model and settings.

```toml
[models.roles.chat]
source = "local"
model = "gemma3:12b"
context = 76800        # tokens; 0 = decided by tuning
kv_type = "f16"        # f16, q8_0, q4_0 or auto
gpu_layers = -1        # -1 = all on the GPU, or decided by tuning
effort = "medium"      # default effort: off, low, medium, high, max
temperature = 0.7      # -1 = the model's default
auto = true            # true: `hal tune --write` may replace context, kv_type and gpu_layers
```

`[models]` itself takes `context_cap` (largest context tuning plans, default 131072),
`headroom_mib` (VRAM kept free per GPU, 0 = the larger of 1 GiB and 8 percent) and
`kv_type` (default `auto`).

## Commands

```sh
hal models add-source ollama http://127.0.0.1:11434   # add a source
hal models list                                       # every source and its models
hal models list --source local --json                 # one source, for scripts
hal models assign chat local gemma3:12b               # give a role a model, pin it, wire the gateway
hal models remove-source local                        # refused while a role uses it
hal tune --auto                                       # detect, discover, plan; changes nothing
hal tune --auto --write                               # also pin, save the roles, generate the mode
hal tune --auto --json                                # the same plan for scripts
```

A name a source lists twice shows once, and the `llamacpp:<hash>` blob ids that Ollama adds to
its OpenAI-style list are hidden: they have no model name to show.

`hal tune` flags: `--role ROLE` (repeatable) plans only that role; `--reassign` chooses again for
roles that already have a model; `--hardware FILE` plans for the machine described in a JSON
file (the `hardware` object of `hal tune --json`). On a terminal, a bare `hal tune` (or `hal tune -i`) opens
the interactive screen below; off a terminal, or with any other flag, it shows the `--auto` plan.

Exit status of `hal tune`: 0 done, 1 error, 2 usage, 3 nothing to tune (no source, or no model).

## The tune screen

`hal tune` on a terminal opens a menu screen in the style of `hal code`. Above the menu it
shows your hardware (each GPU with free and total memory, RAM, CPU threads, WSL2 notes) and one
line per role: model, context and how it fits.

```text
Hal tune
GPU 0  RTX 4090  24000 / 24564 MiB free
RAM  24576 / 32768 MiB free   CPU  16 threads

chat   local/gemma3:12b (hal-chat)                 32K  9.4 GB  fits
fast   local/qwen3:8b *                            32K  6.1 GB  fits
code   local/hal-gpu                                    set in [hal] (not tuned)
...
guard  ollama/granite4.1-guardian:8b                    safety check, CPU
What next?
> 1. Quick setup (presets)
  2. Adjust a role
  3. Model sources (add or remove)
  4. Review changes
  5. Save and exit
  6. Quit without saving
up/down move | enter select | 1-9 jump | esc back | ': commands'
```

A tag built from another model shows that model first and the tag in brackets, so
`hal-chat:latest` reads `gemma3:12b (hal-chat)`. A role set straight in `[hal]` to a gateway
`backend/model`, such as a llama.cpp router preset, is shown but not tuned here. The `guard` line
is the tool-call safety classifier from `policy.toml` (see [security](security.md)); tune shows it
and leaves it alone.

No typing is needed. Up and Down move the highlight, Enter chooses, the keys 1 to 9 jump to a
row, Esc or `q` go back, and a click on a row chooses it (most terminals; if yours does not send
mouse events, use the keys). `GB` here means 1024 MiB.

- **Quick setup** tunes every role against a VRAM budget, then shows the result as a diff for
  review; nothing is written until you choose Save and exit. The presets are *This machine
  (auto)* (the card's total memory), *12 GB GPU*, *24 GB GPU* and *CPU only* (no GPU layers).
  A role keeps its model if it fits the preset, otherwise it gets the best model you have that
  does. If none fits, the screen says so and suggests pulling a smaller model (for example with
  `ollama pull`) or adding a source. A preset stays in force for the rest of the session; pick
  *This machine* to go back. The presets are rows of a table in `halcore/tunepresets.py`.
- **Adjust a role** opens a menu for one role: Model (a list you can filter by typing), Context
  (8K to 128K or Custom, each with its size and `fits`, `tight` or `too big`), KV cache, Effort
  (only for models that can think; `default` clears it), Temperature, and Auto-tune this role.
- **Model sources** adds or removes a source.
- **Review changes** shows the diff of `config.toml`; nothing is written yet.
- **Save and exit** shows the diff and asks Yes or No before it writes. With nothing changed it
  just exits.
- A `*` marks a role changed and not saved. Esc on the main menu and **Quit without saving**
  both ask before they drop changes; the highlight starts on No.

Press `:` on the main menu for the typed command line (Tab completes; `menu` goes back). A
command may start with a role: `model code`.

| Command | What it does |
| --- | --- |
| `role` | Choose the role the other commands change (default `chat`) |
| `model` | Pick a model from your sources with the arrow keys; type to filter |
| `context` | Pick a size (each shows its fit) or type a number: `48000`, `48k` |
| `kv` | Pick `auto`, `f16`, `q8_0` or `q4_0`, with the fit of each |
| `effort` | Pick a default effort; shown only for models that can think |
| `temp` | Pick a temperature, or `default` |
| `auto` | Tune the role for this hardware; `auto all` tunes every role |
| `source` | Add or remove a model source |
| `diff`, `write`, `quit`, `menu` | Show the changes, write them, leave, or go back to the menus |

It works with no GPU, in an 80 by 24 terminal, with `NO_COLOR`, and in ASCII mode.

## Tuning

For each GPU Hal keeps the larger of 1 GiB and 8 percent free, and 600 MiB for the runtime.
What is left, over all GPUs, is the budget. It starts from the card's total memory, not what is
free at that moment, so a program that holds the card today does not shrink the plan; if one does,
`hal tune` warns that another program is using N GB and that the plan assumes it is free.
The weights come out of the budget; the rest holds
the KV cache, which costs `2 x layers x kv_heads x head_dim x bytes` per token (f16 2 bytes,
q8_0 about 1.06, q4_0 about 0.56). The context is what fits, rounded down to a multiple of 1024
and capped at the model's native maximum and at `[models] context_cap`. Example: a 24 GB card
and a 12B Q4 model give 79872 tokens at f16. If the weights do not fit, layers move to system
RAM (hybrid); with no GPU the model runs on the CPU. With `kv_type = auto` Hal keeps the most
precise cache that still gives 32768 tokens. If a model's architecture is not known (a server
that does not say), Hal assumes a pessimistic KV size and the plan says so.

The formula, a full worked example and the limits are in [../dev/tuning.md](../dev/tuning.md).

## Effort

Hal has one dial for how much a model thinks: `off`, `low`, `medium`, `high`, `max`. Models
disagree on how to turn it, so Hal keeps a table by model family and maps the level for you.
Set a default per role with `effort`, in the tune screen, or in `hal code` with `/effort` (see
[hal-code.md](hal-code.md#model-and-effort)); a request can also ask (see [gateway.md](gateway.md#effort-hint)).

| Family (detected from the model's architecture or name) | How it thinks | What the levels do |
| --- | --- | --- |
| Qwen3 and later Qwen (not Qwen3-Coder) | Template switch | `off` turns thinking off; any other level turns it on. On llama.cpp the level also sets `reasoning_budget`: low 1024, medium 4096, high 16384, max unlimited tokens |
| gpt-oss, OpenAI o-series and gpt-5 | `reasoning_effort` | low, medium, high; `max` is high; `off` cannot be honoured and uses low |
| Claude (Anthropic) | `thinking.budget_tokens` | low 1024, medium 4096, high 12288, max 31999; `off` disables thinking. `max_tokens` must be larger than the budget |
| DeepSeek-R1, QwQ | Always thinks | Ignored; `off` is reported as not honoured |
| Anything else | Does not think | Ignored with a logged note |

Name patterns are heuristics. If Hal misreads a fine-tune, set `family` in the role
(`family = "qwen3"`). The exact request fields for llama.cpp and for Ollama's `/v1` route follow
those servers' documentation and are not yet checked against a live server of every version.
