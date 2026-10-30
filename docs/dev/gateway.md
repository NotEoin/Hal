# Gateway developer notes

How `packages/hal-gateway` works and how to change it. The design is
[05-gateway.md](../design/05-gateway.md); this page says what is built.

## Configuration (`halgateway.config`)

- `load_config(path=None)` returns a `GatewayConfig`: the `[gateway]`, `[gateway.log]`,
  `[gateway.sessions]` and `[gateway.gpu]` tables, plus validated `backends` and `aliases` by name.
- Unknown keys, wrong types, a non-loopback `tcp_host`, a remote backend without `allow_remote`
  or without https, a managed router without `models_dir` and an alias that names a missing backend
  all raise `halkit.config.ConfigError`. There is no fallback to defaults.
- With no `[gateway.backends]` table, only an `ollama` backend exists.
- Check a file: `hal-gateway check-config [--config FILE]` (exit 0 ok, 3 invalid).

## Errors (`halgateway.errors`)

Each `GatewayError` subclass owns one HTTP status. `error_body(err, fmt)` renders it for the
`openai`, `anthropic` or `ollama` format.

## Listeners and authentication (`server`, `auth`, `handler`)

- `Gateway(cfg).start()` binds an owner-only unix socket (dir 0700, socket 0600) and, with
  `[gateway].tcp = true`, a loopback TCP listener. `stop()` closes both.
- Unix peers are checked with `SO_PEERCRED`; another uid gets 403 before any body is read.
  Tests inject the uid lookup (`Gateway(cfg, peer_uid=..., own_uid=...)`).
- TCP needs the owner token (`Authorization: Bearer` or `X-Api-Key`), a Host in the loopback
  set plus `allow_hosts`, and no Origin unless listed in `allow_origins`. `GET /health` needs no token.
- The owner token file is created 0600 at the first start. TCP refuses to start if it is
  readable by group or others.
- A stale socket is replaced after a refused connect; a live one stops the start.
- Connections beyond `max_connections` get 503 with `Retry-After: 1`.
- Session and client tokens (`hal-sess-`, `hal-gwc-`) are refused with 401 until their registries
  are built (plan steps 19 and 21). The handler serves `/health` only; routes come with step 3.

## Routes and relay (`routes`, `relay`, `backends`)

- `routes.ROUTES` is the route table of the design; anything else is 404 (llama-server's
  `/props`, `/slots`, `/metrics` are not routes). The query string is ignored for matching and
  forwarded unchanged.
- A model route needs a JSON object body with `Content-Length` (411 without, 400 for chunked,
  413 over `max_request_bytes` with the body unread). `model` is rewritten to the upstream name
  by `Gateway.resolve`; every other field is kept.
- The relay streams each `read1(65536)` chunk and flushes it. It forwards the client's headers
  except hop-by-hop ones, `Host`, `Authorization`, `X-Api-Key`, `Cookie` and `X-Hal-*`, and adds
  the backend's key (`api_key_file` or `api_key_env`).
- A backend that lacks the API gets 404 naming the APIs it serves. Connect or response timeouts
  give 504, other failures before the reply 502; after the reply starts the connection is closed.
- Not built yet: model listing, aliases (step 4), Ollama native routes and `count_tokens`
  estimates (step 5), the request log (step 6). Those routes answer 404 "not available yet".

## Aliases and model listing (`aliases`)

- `Resolver.resolve(name)` follows the design order: user alias, role alias (`hal-chat` ...
  from `[hal]`, empty `code_model` and `writer_model` mean `chat_model`), mode alias, `backend/model`,
  a bare name found in exactly one backend's cached list (two backends give 409), a `patterns`
  match, `default_model`, else 404. Backend lists are cached for 30 s.
- Config refuses a user alias named like a role alias and a `[hal]` model value that is one.
- A role alias whose model is in no list falls back to the only Ollama backend (it loads on demand).
- `GET /v1/models` lists aliases, role aliases and `backend/model` names without duplicates, in the
  OpenAI shape, or the Anthropic shape with an `anthropic-version` header. `context_length` is
  the alias `context_window` for now; router and Ollama sources come with steps 5 and 11.
- Remote backends are marked `owned_by: "remote:<host>"` and replies carry `X-Hal-Remote`.
- Mode aliases (`hal/main`) are a mapping on the resolver that the managed router fills (step 11).

## Ollama backend (`backends/ollama`, `estimate`)

- `OllamaBackend` adds `tags()`, `loaded()` (`/api/ps`) and `unload(model)` (`keep_alive: 0`).
  It drops `llamacpp:<sha>` ids and repeated names from the model list.
- Native routes are the eight of REQ-GW-24 only; `pull`, `push`, `create`, `copy`, `delete`
  and `blobs` are not routes, so they are 404. `GET /api/tags` lists the exposed Ollama models
  plus alias entries (the upstream entry with `name` and `model` set to the alias).
- `POST /v1/messages/count_tokens` to Ollama returns `ceil(len(canonical JSON of system,
  messages, tools) / 4)` with `X-Hal-Estimate: chars/4`.

## Request log (`reqlog`)

- Every request, including errors, writes one JSON line to `<state>/gateway/requests.jsonl`
  (mode 0600) after the reply is sent. Fields are in the design, "Request log"; the policy and
  gpu fields come with their steps. No message content, headers or tokens are written.
- `usage` is read from the last 8 KiB of the reply (OpenAI, Anthropic and Ollama counts).
- `X-Request-Id` is echoed when it matches `^[A-Za-z0-9._-]{1,64}$`, else `r-<8 hex>` is made
  and returned as that header.
- `[gateway.log].content = true` also writes `content/<id>.json` (mode 0600) with the bodies.
- The file rotates at `max_bytes` and keeps `keep` old files (`requests.jsonl.1` ...). A failed
  write logs one ERROR a minute and never fails the request.
- Overhead check: `python3 scripts/bench_relay.py [N]` (fake upstream, no models); 0.67 ms median on
  2026-10-30, including the log write.

## Model manifest and the Ollama gate (`models/manifest`, `server`)

- `models.toml` (`[gateway].models_file`, default `config_dir()/models.toml`) is read at start;
  a missing file is an empty manifest (a warning is logged), an invalid one stops the start.
  The gateway never writes it. Verification state is in `<state>/gateway/verified.json`.
- Before relaying a request that names a model to an Ollama backend, `Gateway.check_model`
  compares the digest `/api/tags` reports (cached 30 s) with the pinned `ollama:` entry. An
  unpinned model or a moved tag answers 503 `model_unverified` (REQ-GW-77).
- The manifest `profile` of a model appears as `meta.hal_profile` in `/v1/models` (OpenAI shape).
- Weight-file verification (`Verifier.verify_path`) is for the llama.cpp router; it is wired in
  with the mode renderer (plan step 10).

## Model helpers (`models`, `models/download`)

- `models.Store(manifest, models_dir, verified)` names the files; the functions `list_entries`,
  `plan`, `pull`, `verify`, `add_local` and `record_ollama` take it first. They are the only
  writers of `models.toml` (temp file, then replace, mode 0600).
- `pull` fetches `hf:<repo>@<40-hex commit>` entries over https (redirects stay https; 10 s
  connect, 60 s per read) into a `.part` file beside the target, hashes while writing and links
  it into place only when size and sha256 match. An existing file with another hash is never
  replaced. Unpinned sources (`@main`, `local`) are refused.
- `verify(full=False)` re-hashes only files whose size, mtime or inode changed since the last
  verified state; `full=True` re-hashes all. `ollama:` entries are checked on each request.
- `add_local` and `record_ollama` pin trust; the CLI must show and ask before calling them.

## Mode presets (`modes`, `modes/`)

- `halgateway/modes/` holds the ten INI modes, the prompt and template files, `effort-xhigh.txt`
  and `SHA256SUMS` (the bundled files a preset may reference as a chat template). A file of the
  same name in `[router].modes_dirs` wins.
- Each mode has a `[hal]` block before `[*]` (keys in the design). `hal-serve` drops that block
  with `sed` until the gateway renders modes itself; `hal-gateway render MODE` does it and
  checks the mode first.
- `render_mode` refuses a mode that sets a denied key (`host`, `api-key`, `tools`, `hf-repo`,
  any `*-default`, ...), names a weight file the manifest does not pin or whose sha256 differs,
  names a pickle format, or uses a `chat-template-file` that is neither bundled (name and hash in
  `SHA256SUMS`) nor pinned by `template_sha256` on one of the preset's models. Nothing is written
  when a check fails. Only the first shard of a split GGUF is hashed.
- `hal-gateway modes` lists modes; `hal-gateway render MODE` prints the path, the sha256 and the
  context window (exit 4 when refused).
- Tokens: `@MODELS@`, `@HAL@` (the `halgateway` directory), `@MODES@`, `@STATE@`.

## Managed router (`backends/llama_router`, `launchers`, `llamacmd`)

`LlamaRouter` is the `llama-router` backend. A managed entry owns one `llama-server` process.
It replaces `hal-serve start|stop|status`.

- **Start** (`start(mode)`): refuse while the legacy `hal-serve` router lives
  (`<state>/router.pid` names a `llama-server`), render the mode (which verifies weights),
  unload Ollama models that hold VRAM (`unload_ollama`), spawn, then poll `GET /models` every
  second until every startup preset is `loaded`. A `failed` preset, `unloaded` after `loading`,
  a process exit or `load_timeout_s` stops the router and raises 503 `router_failed` with the
  log path.
- **State**: `<state>/gateway/router.json` (0600) holds pid, `pid_start`, boot id, mode,
  render sha256, address and start time. A signal goes out only when `pid_start` still
  matches (pids are reused). The router key is `<state>/gateway/router.key` (0600), passed as
  `--api-key-file`, never in argv. The log is `router.log`.
- **Adopt**: at gateway start a router with the same mode and render hash is kept; any other
  is stopped. The gateway never stops the router when it exits.
- **Crash**: the next request restarts it, at most `max_restarts` in 600 s, then 503 naming the
  log. After `router/stop` requests get 503 until a mode switch.
- **Switch**: `POST /hal/v1/mode` waits `[gateway].drain_s` for in-flight requests, then 409
  `router_busy` unless `force`. `models/load` verifies the preset's weights again before it
  posts `/models/load`. All four admin POST routes need the owner on the unix socket.
- Not built yet: the docker launcher (plan step 30), the `swap` and effort policies, gpu-light
  claims. `listen = "unix"` is implemented but untested against a real server.

Tests use a fake `llama-server` script (`tests/test_llama_router.py`); no real model is loaded.

## Policies (`policies/`)

Policies apply to `llama-router` backends on `/v1/messages` and `/v1/chat/completions` only.

- **Overflow** (`policies/overflow.py`, REQ-GW-40): a 400 whose JSON `error.type` is
  `exceed_context_size_error` is rewritten to `prompt is too long: N tokens > M maximum`
  (Anthropic, the text Claude Code compacts on) or to the `context_length_exceeded` code
  (OpenAI). The relay reads a 400 body whole, up to 1 MiB, and recomputes `Content-Length`.
  Any other body passes unchanged. Which OpenAI clients act on the code is unverified; check
  by overflowing a small `ctx-size` from a Codex or opencode session.
- **Pipeline** (`policies/__init__.py`): `select` picks the policies for the alias, `run_rewrites`
  applies them in order and is fail-open (a policy that raises is logged and recorded as
  `error: <type>`). Decisions go to the `policy` field of the request log (REQ-GW-49).
  `hal/main` and `hal/agent` are pointed at the models of the active mode's `[hal]` section
  when the router starts, is adopted, or at gateway start (read from the configured mode).
- **Effort** (`policies/effort.py`, `policies/text.py`, REQ-GW-41): on a request with tools, if
  the last non-system message is a decision point (user text, subagent report, failing shell
  command, tool error), the effort note is appended as a final system message. Both formats
  are handled; an OpenAI `tool` message has no `is_error`, so only the failure patterns apply.
  The alias key `effort = true`, or the mode's `[hal] effort`, turns it on. The per-alias
  `shell_tools` and `agent_tools` keys of the design are not read yet (the defaults apply).
- **Route** (`policies/route.py`, REQ-GW-42..44): applies to requests for the mode's `main`
  model when the mode sets `route`. New user text is classified once through the router's
  `POST /v1/systemone` (30 s timeout) and the choice is held per conversation (key: the first
  message) in an LRU of 1024 until the next user text. Requests without tools go to `agent`.
  A failing classifier means the default, `strong`, and a log line. The `force` and `default`
  keys of `hal-route.py` are not ported: the default is fixed.
- **Delegate** (`policies/delegate.py`, REQ-GW-45): on a request that still targets `main`
  and has tools, the tools in `strip-tools` are removed and the `delegate-note` file is added
  to the system prompt (Anthropic `system`, OpenAI first system message). It runs after route,
  so a turn routed to the fast model keeps its tools.
- **Swap** (`policies/swap.py`, REQ-GW-46..48): one `SwapScheduler` per router and mode, for
  modes with `[hal] swap = true`. `main` and `agent` take turns: a request that needs the
  other model waits until nothing is in flight, `quiet_s` (2.0) has passed and no subagent
  conversation was active within `agent_stale_s` (900). Leaving `main` saves KV slots
  `0..swap-slots-1` and arriving back restores them. Requests without tools under 50,000
  characters run on the loaded model, and load `main` when none is. A wait over
  `swap_wait_s` (7200, `[gateway.backends.NAME]`) or a failed swap is 503 `swap_unavailable`
  with `Retry-After`. The swap text is the `swap` entry of the log's `policy` field. The
  subagent key is the conversation key, so a subagent whose conversation grows keeps one key.
  Not built: the idle `load main` after a gpu-light reclaim (plan step 16) and the
  gpu-light claim held for the swap (step 18). Stop reasons come from the last 8 KiB of the reply.

## Chat `options` (`handler`, `backends/ollama`)

`halkit.llm.chat(options=...)` sends an Ollama `options` object on `/v1/chat/completions`;
the classifier and reviewer use `{"num_gpu": 0}` to run on the CPU while the GPU is busy (I5).
Ollama's OpenAI route cannot set `num_gpu`, so for an Ollama backend and a non-streaming
request the gateway calls the native `/api/chat` with the options and answers in OpenAI chat
shape (text and usage only). This is the one place the gateway translates an API; it is kept
this small on purpose (design decision 2). Allowed options: `num_gpu`, `num_ctx`,
`num_predict`, `temperature`, `top_p`, `seed`; anything else, a non-object value, or `tools`
alongside `options` is 400. Every other backend, and a streaming request, gets the field
stripped. The mapping is tested against a fake Ollama only; a run against a real Ollama with
`num_gpu: 0` is unverified (check with `ollama ps`, where the processor column should read
`100% CPU`).
