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

## Effort

Hal has one dial for how much a model thinks: `off`, `low`, `medium`, `high`, `max`. Models
disagree on how to turn it, so Hal keeps a table by model family and maps the level for you.
Set a default per role with `effort`, or ask per request (see
[gateway.md](gateway.md#effort-hint)).

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
