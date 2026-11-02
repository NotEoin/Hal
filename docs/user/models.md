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

`hal tune` on a terminal opens a screen in the style of `hal code`. It shows your hardware (every
GPU with free and total memory, RAM, CPU threads, WSL2 notes), your sources, and each role with its
model, context, KV type, GPU layers, effort and temperature. Under each role a fit line says how much of
the VRAM the model and its cache use and whether it runs on the GPU, on the GPU and RAM, or on the CPU.
It works with no GPU and in an 80 column terminal.

Type a command, or its first letter (Tab completes). A command may start with a role: `model code`.

| Command | What it does |
| --- | --- |
| `role` | Choose the role the other commands change (default `chat`) |
| `model` | Pick a model from your sources with the arrow keys; type to filter |
| `context` | Pick a preset (each shows its fit) or type a number: `48000`, `48k` |
| `kv` | Pick `auto`, `f16`, `q8_0` or `q4_0`, with the fit of each |
| `effort` | Pick a default effort; shown only for models that can think |
| `temp` | Type a temperature from 0 to 2, or `default` |
| `auto` | Tune the role for this hardware; `auto all` tunes every role |
| `source` | Add or remove a source |
| `diff` | Show what a write would change in the config |
| `write` | Show the diff, ask, then pin the models and write the config |
| `quit` | Leave without writing |

A role you set by hand is written with `auto = false`, so a later `hal tune --write` keeps your numbers.
Nothing changes on disk until you answer `y` to `write`. The write is the one `hal tune --write` uses:
comments in your config stay, the old file is saved as `config.toml.bak.<UTC time>`, and a failure
writes nothing. A model that does not fit your machine cannot be written.

`hal setup` opens this screen on a terminal when no `chat` model is assigned (type `quit` to skip it).
Off a terminal it prints the next command instead.

What `hal tune --auto` does:

1. Reads the hardware: every GPU from `nvidia-smi` (or `amd-smi` and `rocm-smi`), RAM, CPU threads,
   WSL2. A tool that is missing or slow is reported as a note; detection never fails the command.
2. Lists the models of every source, with size, parameters, quantisation, layers, heads and
   native context where the source says.
3. Keeps the model of every role that has one. For an empty role it picks among local models
   (never a cloud source or a remote server): `chat` the largest that fits in VRAM with at least
   16384 tokens of context, preferring models that take tools; `fast` and `embed` the smallest
   that works; `code` a model whose name says coder or code; `vision` a model with vision.
   `writer` stays empty and falls back to `chat`.
4. Works out context, KV cache type and GPU layers for each (below).

With `--write` it then pins each chosen model (an Ollama digest, or the sha256 of a GGUF file,
which takes a while for a large file), saves `[models.roles.*]` and the `[hal]` model names the
gateway reads, adds the gateway backends a source needs, and writes the llama.cpp mode `tuned`
for GGUF roles to `~/.config/hal/modes/tuned.ini`. The bundled modes keep working. Your old
config is saved as `config.toml.bak.<UTC time>`. An Anthropic source can be listed and assigned
but the gateway has no Anthropic backend, so that role is left out of the gateway with a note.

A write is all or nothing. Hal checks the new config and every model before it writes. If a pin
fails part way (a file that cannot be read, a full disk), it puts the pin list and the mode file back
and tells you that nothing was written; your config is not touched.

Nothing downloads a model. `hal setup` and `setup.sh` never pull one either; see
[../howto/point-hal-at-your-models.md](../howto/point-hal-at-your-models.md).

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
