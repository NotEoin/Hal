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
