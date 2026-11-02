# The model gateway

`hal-gateway` is the one local endpoint for model requests. Harnesses, the agent, tool packs and
the box talk to it; it forwards to Ollama, a managed `llama-server` router or a cloud backend
you opted into. It also owns the GPU claims, the mode presets and the model manifest.

Status: the package is built and tested with fake backends. The launcher (`hal serve`, `hal
connect`) still calls the old `hal-serve`; until it switches, run `hal-gateway` directly.
Nothing here has run against a live llama-server in this checkout (unverified; check with
`hal-gateway status` after `hal-gateway serve`).

## Where it listens

| Listener | Default | Who uses it |
| --- | --- | --- |
| Unix socket | `<runtime dir>/gateway.sock`, mode 0600 | Host programs. The owner is the user who owns the socket, found by peer credentials |
| Loopback TCP | off; `127.0.0.1:8741` when `[gateway].tcp = true` | Harnesses that need a URL. Needs a bearer token |
| Model socket | one per box session, made by the gateway | The agent in a box. It reaches the model API only |

## Commands

| Command | What it does |
| --- | --- |
| `hal-gateway serve` | Run in the foreground until SIGTERM or SIGINT |
| `hal-gateway status [--json]` | Backends, mode, GPU claims, leases and remote warnings |
| `hal-gateway mode [NAME] [--force] [--no-wait]` | Show or switch the router mode |
| `hal-gateway models [--json]` | What `/v1/models` returns to the owner |
| `hal-gateway load` / `unload` / `router stop` | Control the managed router |
| `hal-gateway release-gpu` | Unload models and release the GPU claims |
| `hal-gateway token show\|rotate\|issue\|revoke\|list` | Owner and client tokens |
| `hal-gateway modes` / `render` | List and render the mode presets (see [modes](modes.md)) |
| `hal-gateway check-config` | Validate `[gateway]` and exit |

Exit codes: 0 ok, 3 bad config, 4 refused, 5 gateway down or failed, 6 conflict (HTTP 409 or a
busy reclaim).

## Sharing the GPU with gpu-light

With gpu-light v2 running, the gateway registers a reclaimer and claims the card per request
class: chat turns are `interactive`, the warm base model is `service`. `hal jobs` and `hal eval`
run as class `batch`, so they wait for or yield to a chat turn. gpu-light asks Hal to give VRAM
back through `hal-gateway release-gpu --reclaim`. Without gpu-light (or with `HAL_GPU_LIGHT=off`)
none of this happens and Hal uses the GPU directly. Verified by
`packages/hal-gateway/tests/test_gpu_manager.py` and `packages/hal-core/tests/test_evaluate.py`.

## Configuration

Keys live under `[gateway]` in `~/.config/hal/config.toml` (`$HAL_CONFIG` overrides). An unknown
key or a wrong type stops the start with an error naming the key. The full key list with
defaults is in [design 05](../design/05-gateway.md) under "Configuration". Old `hal.conf`
settings (`HAL_LLAMA_SERVER`, `HAL_MODELS_DIR`, `HAL_LLAMA_URL`, `HAL_MODE`, `HAL_OLLAMA_URL`)
map to `[gateway.backends.local]` and `[hal].ollama_url`; `halgateway.legacy.import_hal_conf`
reads them without running the file, and `hal configure` will offer to write them.

## Models and pinning

Ollama models must be pinned in `models.toml` by digest; an unpinned model or a moved tag
answers 503 `model_unverified`. Weight files for the router carry a sha256 and are checked
before they load. See [pin a model](../howto/pin-a-model.md).

## Remote backends

A backend with an `https` URL on another host needs `allow_remote = true`. The gateway then
logs a warning at start, `status` lists it, `/v1/models` marks its models `owned_by:
"remote:<host>"` and every reply carries `X-Hal-Remote`. Box sessions and client tokens cannot
reach it unless granted. Your prompts leave the machine on that path: it is not private. See
[add a cloud backend](../howto/add-a-cloud-backend.md).

## Effort hint

A client asks for thinking effort on a Chat Completions request with the `X-Hal-Effort` header or
the `hal_effort` body field. The value is `off`, `low`, `medium`, `high` or `max`; the header wins.
The gateway turns it into what the model's server understands (Ollama `think`, `reasoning_effort`,
`chat_template_kwargs`, llama.cpp `reasoning_budget`) and removes `hal_effort` before forwarding.
Without a hint, a role alias such as `hal-chat` uses the `effort` and `temperature` of its
`[models.roles.chat]` entry. A field the client already set is never overwritten.

A model that cannot think, an unknown level, or a route other than chat completions is ignored:
the request still runs, a line is logged and the `policy.effort` field of the request log says
why. The table of model families and the token budgets are in [models.md](models.md#effort).

## Related

- [Connect a harness to the gateway](../howto/connect-a-harness-to-the-gateway.md)
- [Run a second Hal install](../howto/run-a-second-hal-install.md)
- [Gateway developer notes](../dev/gateway.md)
