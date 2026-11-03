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

`hal tune` on a terminal opens a full-screen app you can drive with the mouse or the keys.
Nothing is written until you save.

```text
RTX 4090  24000 / 24564 MiB free     RAM 24576 / 32768 MiB free   CPU 16 threads
Plan for  [ This machine ] [ 8 GB ] [ 12 GB ] [ 16 GB ] [ 24 GB ] [ CPU only ]
 Roles | Sources | Review 1
 chat   local/gemma3:12b (hal-chat) *  | chat
        32K  fits                      | Model        local/gemma3:12b (hal-chat)  (change)
 fast   local/qwen3:8b                 | Context      =========O--------------     32K  [Auto]
 code   llamacpp/hal-code              | KV cache     [ auto (q8_0) ] [ f16 ] [ q8_0 ] [ q4_0 ]
        set in [hal] (not tuned)       | GPU layers   ======================O     48  [Auto]
 ...                                   | Effort       [ default ] [ off ] [ low ] [ medium ] ...
 guard  ollama/granite4.1-guardian:8b  | Temperature  ==========O-----------    auto  [Default]
        (safety check, CPU)            | Memory       [##############-------] 9.4 / 22.1 GB  fits
 ^s Save  ^z Undo  esc Quit  f1 Help  f2 Roles  f3 Sources  f4 Review
```

- **Plan for** tunes every role against a VRAM budget: *This machine* (the card's total memory),
  8, 12, 16 or 24 GB, or *CPU only*. A role keeps its model if it fits, otherwise it gets the
  best model you have that does; when none fits, a note names a model to pull. The presets are
  rows of a table in `halcore/tunepresets.py`.
- **Roles** lists every role with its real model (a tag built from another model shows that model
  first and the tag in brackets) and how it fits; `*` marks a change not saved yet. Moving in the
  list opens that role on the right.
- **Context**, **GPU layers** and **Temperature** are sliders: drag the knob, click the track,
  use the wheel on a focused slider (elsewhere the wheel scrolls), or focus them and press
  Left/Right (PgUp/PgDn for big steps, Home/End for the ends). The memory bar moves while you drag; the change lands when you let go. `Auto` (or `a`)
  hands the value back to tuning; `Default` leaves the temperature to the model.
- **GPU layers** puts that many layers on the GPU by hand. It is off, with the reason, when the
  model's layer count is unknown, when the server sizes the model, or when the plan has no GPU.
- **KV cache** and **Effort** are rows of buttons. `auto` shows what tuning picks now. Effort
  levels the model cannot use are struck through; `default` clears the effort.
- **Memory** is green when the role fits, amber when it is tight (over 85 percent of the budget)
  or spills into RAM, red when it does not fit; the lines below it give the numbers.
- **Sources** adds a source (the form keeps what you typed and shows any error in place) or
  removes one; Remove is off, with the reason, while a role uses the source.
- **Review** shows the diff of `config.toml`. **Save** (Ctrl+S) shows the diff again and asks,
  with Cancel focused, before it writes. With nothing changed, Save just exits.
- **Ctrl+Z** undoes the last change, a preset included. **Esc** quits at once when nothing
  changed; otherwise it asks, with Keep editing focused (Discard and quit, or Save).
- The first time (no role assigned) a **Quick start** screen shows what each preset would pick;
  click a card or press Enter on it, choose Adjust by hand, or press Esc to leave. With no model
  source at all, the app opens on the Sources tab instead.

The `code` role set straight in `[hal]` to a gateway `backend/model`, such as a llama.cpp router
preset, is shown but not tuned. The `guard` row is the tool-call safety classifier from
`policy.toml` (see [security](security.md)); tune shows it and leaves it alone. `GB` here means
1024 MiB. It works with no GPU, at 80 by 24, with `NO_COLOR` and in ASCII mode.

### The line screen

Without a capable terminal (`TERM=dumb`, `HAL_TUI=line`, `[ui].interface = "line"`), or when
the full screen cannot start, `hal tune` opens the older menu screen instead: Up and Down move,
Enter chooses, 1 to 9 jump to a row, Esc goes back, a click on a row chooses it. Press `:` on its
main menu for the typed command line (Tab completes; `menu` goes back). A command may start with
a role: `model code`.

| Command | What it does |
| --- | --- |
| `role` | Choose the role the other commands change (default `chat`) |
| `model` | Pick a model from your sources with the arrow keys; type to filter |
| `context` | Pick a size (each shows its fit) or type a number: `48000`, `48k` |
| `kv` | Pick `auto`, `f16`, `q8_0` or `q4_0`, with the fit of each |
| `effort` | Pick a default effort, or `default`; shown only for models that can think |
| `temp` | Pick a temperature, or `default` |
| `auto` | Tune the role for this hardware; `auto all` tunes every role |
| `source` | Add or remove a model source |
| `diff`, `write`, `quit`, `menu` | Show the changes, write them, leave, or go back to the menus |

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
