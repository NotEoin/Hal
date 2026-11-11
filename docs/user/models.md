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

A source only says where models are. To have the gateway also start and stop the server, add a
`managed-openai` backend with the same name as the source; see
[run a vLLM server under Hal](../howto/run-a-vllm-server-under-hal.md).
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

A role names the job a model does: `chat` (core Hal, hal-voice and `hal chat`), `code`
(`hal code`), `writer` (prose), `vision`, `classifier` (the tool-call risk check, Granite
Guardian by default) and `embed` (vault search). The gateway serves each as `hal-ROLE`, and
every Hal service asks for one of those names, never for a model. `fast` was renamed
`classifier` in 0.2.0; an old `[models.roles.fast]` or `[hal] fast_model` still loads. Each role has a source, a model and settings.

```toml
[models.roles.chat]
source = "local"
model = "gemma3:12b"
context = 76800        # tokens; 0 = decided by tuning
kv_type = "f16"        # f16, q8_0, q4_0 or auto
gpu_layers = -1        # -1 = all on the GPU, or decided by tuning
cpu_moe = 0            # mixture-of-experts GGUF: layers whose experts stay in RAM
parallel = 1           # GGUF: requests served at once, each with `context` tokens
draft = ""             # speculative decoding: "" = the best draft found, "off", or a file
draft_max = 0          # tokens drafted per step; 0 = 5 for an MTP head, 3 for others
effort = "medium"      # default effort: off, low, medium, high, max
temperature = 0.7      # -1 = the model's default
auto = true            # true: `hal tune --write` may replace context, kv_type and the offload
```

The `context` of a role is the window every client sees: the gateway reports it as the
`context_length` of the role's alias (`hal-chat`, `hal-code`, `hal-NAME`), and `hal code`
sizes its conversation to it. For an Ollama model, saving makes `hal-tuned-ROLE` in Ollama
(the same weights with that `num_ctx`), because Ollama ignores a context sent with a request.
Roles on the same model with the same context (chat and vision, say) share one, so Ollama loads
it once. With `[hal] embed_on_cpu` (the default) the embed role's model has no GPU layers, so
indexing the vault never takes VRAM; see [how the settings reach the server](../dev/tuning.md#how-the-settings-reach-the-server).

### Named roles

You can add your own roles beside the built-in ones. A named role is one model with its own
settings, such as a coding setup with a long context next to the everyday `code` role. The
gateway serves it as `hal-NAME`, so you pick it in `hal code` with `/model hal-code-128k`, or
start with `hal code --model hal-code-128k`.

```toml
[models.roles.code-128k]
source = "local"
model = "qwen3.8:27b"
context = 131072
auto = false

[gateway.aliases.hal-code-128k]   # written by `hal tune` and `hal models assign`
backend = "ollama"
model = "qwen3.8:27b"
```

A name is lowercase letters, digits and `-`, starts with a letter, and is at most 32
characters. Add one in the tune screen (**New role...**), or with
`hal models assign NAME SOURCE MODEL`. Remove one with **Remove role** or
`hal models remove-role NAME`, which also removes its alias. Auto-tune and the presets never
choose a model for a named role: it keeps the model you gave it. A named role on a GGUF file
gets its own preset (`tuned-NAME`) in the generated mode.

`[models]` itself takes `context_cap` (an optional limit on the context auto-tune picks; 0, the
default, means each model's own maximum, such as 262144 for Qwen3.8 27B; a context you set
yourself may always go up to that maximum), `headroom_mib` (VRAM auto-tune
leaves free per GPU, 0 = the larger of 1 GiB and 8 percent) and `kv_type` (default `auto`).

## Commands

```sh
hal models add-source ollama http://127.0.0.1:11434   # add a source
hal models list                                       # every source and its models
hal models list --source local --json                 # one source, for scripts
hal models assign chat local gemma3:12b               # give a role a model, pin it, wire the gateway
hal models assign code-128k local qwen3.8:27b         # add a named role, served as hal-code-128k
hal models remove-role code-128k                      # remove a named role and its alias
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
GPU 0  RTX 4090  24.0 GB
RAM  32.0 GB   CPU  16 threads
Plan for  [ This machine ] [ 8 GB ] [ 12 GB ] [ 16 GB ] [ 24 GB ] [ CPU only ]
 Roles | Sources | Review 1
 chat   local/gemma3:12b (hal-chat) *  | chat
        32K  fits                      | Model        local/gemma3:12b (hal-chat)  (change)
 code   local/qwen3:8b                 | Context      =========O--------------     32K  [Auto]
 code   llamacpp/hal-code              | KV cache     [ auto (q8_0) ] [ f16 ] [ q8_0 ] [ q4_0 ]
        set in [hal] (not tuned)       | GPU layers   ======================O     48  [Auto]
 ...                                   | Effort       [ default ] [ off ] [ low ] [ medium ] ...
 guard  ollama/granite4.1-guardian:8b  | Temperature  ==========O-----------    auto  [Default]
        (safety check, CPU)            | Memory       [##############----|--] 9.4 / 24.0 GB  fits
 ^s Save  ^z Undo  esc Quit  f1 Help  f2 Roles  f3 Sources  f4 Review
```

- **Plan for** tunes every role against a VRAM budget: *This machine* (the card's total memory),
  8, 12, 16 or 24 GB, or *CPU only*. A role keeps its model if it fits, otherwise it gets the
  best model you have that does; when none fits, a note names a model to pull. The presets are
  rows of a table in `halcore/tunepresets.py`.
- **Roles** lists every role with its real model (a tag built from another model shows that model
  first and the tag in brackets) and how it fits; `*` marks a change not saved yet. Moving in the
  list opens that role on the right. On a narrow terminal (down to 60 columns) the list gets
  narrower and the rows of buttons drop their padding, so every control stays on screen.
- **Context**, **GPU layers** and **Temperature** are sliders: drag the knob, click the track,
  use the wheel on a focused slider (elsewhere the wheel scrolls), or focus them and press
  Left/Right (PgUp/PgDn for big steps, Home/End for the ends). Click the number, or press Enter,
  to type an exact value (`48k` for a context); a value outside the range is refused with the
  range. The memory bar moves while you drag; the change lands when you let go. `Auto` (or `a`)
  hands the value back to tuning; `Default` leaves the temperature to the model.
- Every field is yours once you move it: nothing you set changes unless you press its `Auto`
  button, **Auto-tune this role** or a preset. A setting that no longer fits the card is not
  refused; the rest spills to RAM and the memory bar says how much.
- **Context** goes up to the model's own maximum. For a model the server sizes (vLLM and other
  OpenAI-compatible servers) it declares the window the server serves, so clients see it.
- **GPU layers** puts that many layers on the GPU by hand. It is off, with the reason, when the
  model's layer count is unknown, when the server sizes the model, or when the plan has no GPU.
- **Speculative** appears for a GGUF model with a draft model beside it (an MTP head, Gemma's
  `assistant`, a DFlash or DSpark drafter) or with its own MTP layers (`built-in MTP`, often
  in files named `...-MTP-Q4_K_M`): `auto` uses the best one found, `off` none. A draft
  guesses tokens the model checks in one pass, which roughly doubles decode speed (Qwen3.8-27B
  41 to 85 tokens/s, Gemma 4 31B 39 to 100 on code on an RTX 3090; see
  [tuning](../dev/tuning.md#speculative-decoding)). **Draft tokens** sets how many it guesses per
  step. The memory bar counts the draft.
- **Parallel** appears for a GGUF model and serves that many requests at once, each with the
  role's whole context, for bulk work such as vault ingest. The cache holds one context per
  request (4 x 32K costs what one 128K does), so lower the context to keep it on the GPU. The
  gateway lists the count as the alias's `slots`. Ollama models share one server-wide setting,
  `OLLAMA_NUM_PARALLEL`, so the row is not shown for them.
- **RAM experts** replaces GPU layers for a mixture-of-experts GGUF (the kind the `max` profile
  runs): every layer stays on the GPU and the routed experts of that many layers stay in RAM,
  which is much faster than moving whole layers. Use it to run a model larger than your VRAM.
- **KV cache** and **Effort** are rows of buttons. `auto` shows what tuning picks now. Effort
  levels the model cannot use are struck through; `default` clears the effort.
- **Memory** is what the model takes on the card (weights, cache and llama.cpp's own buffers)
  against the card's total, with a `|` at the auto-tune guidance. Auto-tune stays left of it. A
  setting you make may use the whole card: fine on a headless GPU, risky with a desktop on the
  card. `+ N RAM` is the part in system RAM. The bar is green when the role fits, amber when it
  is tight (over 92 percent of the card), past the guidance or spilling into RAM, and red when
  it does not fit; the lines below it give the numbers. It does not change with what is loaded
  right now: plans count the whole card, and a note says when something holds part of it.
- **Sources** adds a source (the form keeps what you typed and shows any error in place) or
  removes one; Remove is off, with the reason, while a role uses the source.
- **Review** shows the diff of `config.toml`. **Save** (Ctrl+S) shows the diff again and asks,
  with Cancel focused, before it writes. With nothing changed, Save just exits.
- **Ctrl+Z** undoes the last change, a preset included. **Esc** quits at once when nothing
  changed; otherwise it asks, with Keep editing focused (Discard and quit, or Save).
- The first time (no role assigned) a **Quick start** screen shows what each preset would pick;
  click a card or press Enter on it, choose Adjust by hand, or press Esc to leave. With no model
  source at all, the app opens on the Sources tab instead.

The **harness** row sets what `hal/main` and `hal/agent` serve: the names Claude Code and other
harnesses ask for (see [gateway](gateway.md#halmain-and-halagent)). Each is one of your roles.

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
| `newrole`, `delrole` | Add a named role (`newrole code-128k`), or remove one |
| `source` | Add or remove a model source |
| `diff`, `write`, `quit`, `menu` | Show the changes, write them, leave, or go back to the menus |

## Tuning

Hal estimates what a model takes on the GPU: the weights there, the KV cache
(`2 x layers x kv_heads x head_dim x bytes` per token: f16 2 bytes, q8_0 about 1.06, q4_0 about
0.56) and llama.cpp's own buffers. Checked against `nvidia-smi` on an RTX 3090, the estimate was
6 to 107 MiB high, never low. Auto-tune picks the most context that fits the card less a headroom
(the larger of 1 GiB and 8 percent), rounded down to 1024 and up to the model's own maximum (or
`[models] context_cap`, when you set one); a 24 GB card and a 12B Q4 model get 79872 tokens at f16. If the weights
do not fit, a dense model moves layers to RAM and a mixture-of-experts model moves experts; with
no GPU the model runs on the CPU. With `kv_type = auto` Hal keeps the most precise cache that
still gives 32768 tokens. Plans use the total VRAM and RAM, not what is free while Hal or
another program runs. If a model's architecture is not known (a server that does not say), Hal
assumes a pessimistic KV size and the plan says so.

The formula, the measurements, a worked example and the limits are in
[../dev/tuning.md](../dev/tuning.md).

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
