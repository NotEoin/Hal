# halgateway: the local model gateway

This spec defines `halgateway`, the package that gives every Hal client and every third-party
harness one authenticated local model endpoint in front of llama.cpp, Ollama and any
OpenAI-compatible server. It replaces `hal-serve`, `hal-route.py` and the GPU-light hooks of
today's Hal with one stdlib Python service, owns the model manifest (`models.toml`) and model
downloads, and adds the per-session forwarders the sandbox uses to reach models.

Status: draft 2026-10-23, revised 2026-10-24 against the design resolutions (binding) and the brief's
host facts.

## Scope and non-goals

In scope:

- One gateway process with two listeners: a unix socket at `halkit.paths.runtime_dir()/
  gateway.sock` (always) and an optional TCP listener on `127.0.0.1:8741`, off by default,
  protected by a bearer token.
- Passthrough of the model APIs `/v1/chat/completions`, `/v1/responses`,
  `/v1/responses/input_tokens`, `/v1/messages`, `/v1/messages/count_tokens`, `/v1/models`,
  `/v1/embeddings`, a subset of the Ollama native API, and a health route, with streaming (SSE
  and NDJSON) and backpressure.
- Model aliases: the six role aliases (`hal-chat`, `hal-classifier`, `hal-code`, `hal-writer`,
  `hal-embed`, `hal-vision`) resolved from `[hal].*_model`, the mode aliases `hal/main` and
  `hal/agent`, and user aliases that map a public name to a backend and an upstream model.
- Three backend types: a managed or external llama.cpp router (`llama-router`), Ollama
  (`ollama`), and any OpenAI-compatible URL (`openai`). Remote (cloud) backends only with an
  explicit `allow_remote = true` and a visible warning.
- The managed llama-server router, ported from `hal-serve`: mode presets from `modes/*.ini`
  with `@MODELS@`, `@HAL@` and `@STATE@` rendering, `--models-preset`, `--models-max`, load
  wait, Ollama unload before a load.
- The router policies of `hal-route.py` as optional, tested features: `route` (a small
  classifier on `/v1/systemone`), `effort` (xhigh notes at decision points), `delegate`
  (strip shell and edit tools from the strong model), `swap` (two models take turns on the GPU
  with KV slot save and restore), and context-overflow error mapping for Claude Code.
- The model manifest `~/.config/hal/models.toml` (schema owned here; security rules in
  01-security REQ-SEC-79 to REQ-SEC-82) and `halgateway.models` (list, pull with a pinned
  revision and sha256, verify), which `hal models` (06-core-cli) wraps.
- GPU coordination: per-class claims over gpu-light v2's JSON-RPC socket API, a `command`
  reclaimer (`hal-gateway release-gpu --reclaim`) so gpu-light can free the gateway's idle
  VRAM, gpu-light v1 file claims as a fallback, or none; idle release, the off flag with
  lease exemptions, and the peer-GPU guard and per-install state directory taken from
  an earlier private fork.
- Per-session forwarders for the sandbox (session tokens, route allowlist, size and
  concurrency caps), which can also target a remote gateway URL with a client token.
- Named client tokens (`hal-gateway token issue|revoke|list`), persisted hashed, and client
  mode (`[gateway].url`) for a laptop that reaches a desktop gateway through `hal remote`.
- A request log with metadata only by default.
- An admin API on the unix socket, a Python client for halcore, and the `hal-gateway` CLI.
- The `hal-gateway.service` unit template (06-core-cli renders it).

Non-goals:

- **Protocol translation.** The gateway never converts Anthropic Messages to Chat
  Completions or back. A route that a backend does not serve returns a clear error. llama.cpp
  and Ollama serve all three model APIs [verified: section "Verified facts"], so local use
  needs no translation.
- The GPU scheduler itself (gpu-light v2, `gpu-light/docs/design-v2.md`,
  "GL" below). The gateway is a client and follows GL's wire protocol exactly.
- A request queue across clients. Priority between workloads is gpu-light's job.
- TLS on the gateway listener. Remote access goes through SSH (07-remote), which forwards the
  loopback port.
- A web UI, OpenTelemetry export, Prometheus metrics.
- Legacy `/v1/completions`, `/infill` and the llama.cpp-only routes (`/tokenize`, `/slots`,
  `/props`, `/models/load` and so on) for clients. The gateway calls some of them itself; it
  never exposes them.
- Router policies on `/v1/responses` and the Ollama native API. They pass through unchanged.
- Building the container image for the optional containerised router (REQ-GW-71). The
  launcher takes an image name; 02-sandbox owns image builds.

### Interfaces used from other specs

| Owner | Interface | Used for |
| --- | --- | --- |
| 00-overview | `halkit.paths.runtime_dir/state_dir/config_dir/ensure_private_dir/check_socket_path` (one runtime-dir rule, resolution F6), `halkit.config`, the `[hal]` table (`chat_model`, `classifier_model`, `code_model`, `writer_model`, `embed_model`, `vision_model`, `ollama_url`), REQ-SYS-09 to REQ-SYS-12, the ports table | paths, role aliases, listener rules |
| 01-security | REQ-SEC-66 (forwarder route allowlist), REQ-SEC-75 (TCP listener), REQ-SEC-76 and `llama_server_command(...)` (managed llama-server argv and environment), REQ-SEC-78 (llama.cpp lock), REQ-SEC-79 to REQ-SEC-82 (model manifest, pickle refusal, templates, downloads), ATK-09, ATK-32, ATK-33 | security requirements this spec implements |
| 02-sandbox | the session socket directory, the bind mount of `model.sock` at `/run/hal`, `hal-relay`, and the `start_model` callable that halcore supplies (02 "Model socket and forwarder") | where the forwarder listens |
| 03-agent | treats 403 like 404 on count endpoints; reads `context_length` and `meta.hal_profile` from `/v1/models`; agent profiles live in `~/.config/hal/model-profiles.toml` (not this spec's `models.toml`) | agent client behaviour |
| 06-core-cli | `hal serve`, `hal stop`, `hal models`, `hal off`, `hal status`, `hal connect`, `hal claude`, `hal gateway ...` (the command tree), unit rendering (`@BIN@`), session minting for `hal code` | front ends over this spec's client |
| 07-remote | runs `hal-gateway token issue|revoke|list`; writes `[gateway].url` and `[gateway].token_file` on a laptop | per-client tokens, client mode |
| GL (gpu-light v2) | "Socket API" (`hello`, `claim`, `claim.release`, `claim_granted`, `reclaimers.list/add/remove`), "Reclaimers" (`command` type), "Version 1 compatibility" | GPU coordination |

### Verified facts this spec relies on

The llama-server, Ollama and Python checks ran on 2026-10-23 on the development host, read-only or in a
scratch directory with the GPU hidden (`CUDA_VISIBLE_DEVICES=`). That directory was wiped
since, so the prototypes are not kept; the slow test tier re-runs the llama-server checks.
The docker rows were re-checked on 2026-10-24.

| Fact | How it was checked |
| --- | --- |
| llama-server b11512 (`version: 0.6.0-dev (build 11512, commit a11f57ba9)`) defaults `--port` to 8080 and `--host` to 127.0.0.1 | `llama-server --help` and `--version` on `~/.local/share/hal/llama.cpp/llama-b11512/llama-server` |
| `--host` accepts a unix socket path ending in `.sock`; the router listens there (`listening on unix://ls.sock`) and creates the socket with mode 0775 | Started a router with an empty preset and `--host ls.sock`, queried it with `curl --unix-socket` |
| A socket path longer than the `sun_path` limit fails to bind (`couldn't bind HTTP server socket`); a 133-byte path failed | Same experiment with an absolute scratch path |
| `--api-key-file` makes `/v1/models` and `/models` return 401 without a key and 200 with `Authorization: Bearer <key>` or `X-Api-Key: <key>`; `/health` and `/v1/health` stay public | Same experiment |
| An unknown model returns 400 `{"error":{"code":400,"message":"model 'none' not found","type":"invalid_request_error"}}` on `/v1/chat/completions` and `/v1/messages/count_tokens` | Same experiment |
| The b11512 server binary contains the routes `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens`, `/v1/embeddings`, `/v1/models`, `/models/load`, `/models/unload`, `/slots/:id_slot`, `/v1/systemone`, and the strings `exceed_context_size_error`, `n_prompt_tokens`, `n_saved`, `n_restored` | `strings libllama-server-impl.so` |
| Router mode prints "router mode enabled - do not expose to untrusted environments"; `--tools`, `--agent`, `--mcp-servers-config`, `--tools-runtime` and `--ui-mcp-proxy` exist and are marked unsafe | `--help` and the startup log |
| Ollama 0.40.1 runs on 127.0.0.1:11434 and serves `/v1/messages`, `/v1/responses`, `/v1/chat/completions` and `/v1/embeddings` (404 `model not found` for a fake model, so the routes exist) but not `/v1/messages/count_tokens` (`404 page not found`) | `curl` against the running Ollama with model `nonexistent-hal-probe:0` (no model loaded) |
| Ollama's `/v1/models` lists some ids twice and lists internal `llamacpp:<sha256>` ids | `curl http://127.0.0.1:11434/v1/models` |
| Ollama's `/api/tags` returns a 64-hex digest per model | `curl 127.0.0.1:11434/api/tags` (01-security, "Supply chain") |
| `SO_PEERCRED` on an `AF_UNIX` socket returns the peer pid and uid in Python 3.13; the accepted socket's address is the empty string | Python experiment |
| The legacy `hal-route.py` policies ran for real: `~/.local/state/hal/route.log` has 1895 lines, 223 `effort xhigh` decisions, 5 classifier turns and 3 swaps (16.2 s, 47.2 s, 19.2 s); `~/.local/state/hal/slots/` holds `hal-hybrid-0.bin` and `hal-hybrid-1.bin` | `grep -c` on the log, no message text read |
| gpu-light v1 code existed but was not installed; gpu-light v2 existed as a design (`docs/design-v2.md`) with no code yet | measured during development, 2026-10-24 |
| Docker lists runtimes `io.containerd.runc.v2`, `nvidia`, `runc`, `runsc`, `runsc-gpu`; `/etc/docker/daemon.json` registers `runsc` without `--host-uds` and `runsc-gpu` with `--nvproxy` | `docker info --format '{{range $k,$v := .Runtimes}}{{$k}} {{end}}'` and `cat /etc/docker/daemon.json`, 2026-10-24 |
| `--runtime=nvidia --gpus all` sees the RTX 3090; `--runtime=runsc-gpu --gpus all` fails with "cannot create sandbox: cannot read client sync file: EOF"; runsc refuses a bind-mounted host unix socket unless registered with `--host-uds=open` | Brief, "Host facts" (verified 2026-10-23) |

### Measured proxy overhead (why stdlib is enough)

A prototype of the relay in this spec (stdlib `ThreadingHTTPServer` over TCP and over
`AF_UNIX`, JSON parse and re-serialise of every body, `read1(65536)` plus `flush()` per
chunk) ran in front of a fake SSE backend, each in its own process, on Python 3.13.11 (three
runs, 2026-10-23; the script was lost with the scratch directory and is re-written as
`scripts/bench_relay.py`, implementation step 6):

| Measure | Direct | Via proxy, TCP | Via proxy, unix |
| --- | --- | --- | --- |
| Small non-streaming request, p50 | 390 to 396 us | 759 to 778 us | 572 to 743 us |
| Paced stream (100 events, 10 ms apart), per-event latency p50 | 73 to 88 us | 120 to 131 us | 109 to 111 us |
| Same, p99 | 135 to 310 us | 201 to 209 us | 193 to 282 us |
| 2000-event burst stream, total | 5.7 to 11.3 ms | 7.4 to 15.2 ms | 7.7 to 10.5 ms |

- 8 concurrent 2000-event streams through the unix proxy finished in 71 to 91 ms.
- A client reading 20,000 events with a 2 ms pause per read left the proxy's RSS flat
  (19,508 to 19,568 KiB): blocking writes push back on the upstream read, so the relay buffers
  at most one 64 KiB chunk per stream.
- Parsing and re-serialising a 1.0 MiB request body costs 2.2 ms.

The proxy adds about 0.2 to 0.4 ms per request and about 40 us per streamed event. A local
model decodes one token every 12 to 200 ms (MODES.md: 83 to 5 tokens/s), so the overhead is
below 1% of the fastest token interval. No third-party server (aiohttp, uvicorn) is justified.

## Requirements

Test kinds: **unit** (hermetic, in-process fakes), **integration** (real sockets and
subprocesses with fake backends; hermetic, runs by default), **slow** (real llama-server
binary with the GPU hidden; marked `slow`, skipped when the binary is absent), **docker**
(marked `docker`), **gpu** (marked `gpu`, real models), **manual**. Markers follow 00-overview
(resolution E12).

### Listeners and authentication (REQ-GW-0x)

- **REQ-GW-01** The gateway MUST listen on a unix socket, by default
  `halkit.paths.runtime_dir()/gateway.sock`. The directory MUST be created with
  `ensure_private_dir` (0700) and the socket with mode 0600, and the gateway MUST refuse to
  start if the directory exists with another owner or with group or other permission bits
  (REQ-SYS-11). Test: integration `test_unix_socket_is_owner_only`,
  `test_start_refuses_group_writable_socket_dir`.
- **REQ-GW-02** On the unix socket the gateway MUST check the peer uid with `SO_PEERCRED` and
  reject any peer whose uid differs from its own with 403, before reading the request body.
  Test: unit `test_foreign_uid_peer_is_rejected` (peer-cred lookup injected).
- **REQ-GW-03** When `gateway.unix_path` is empty and `runtime_dir()` raises (00-overview's
  rule finds no runtime directory), the gateway MUST exit with that error plus the names
  `HAL_RUNTIME_DIR` and `[gateway].unix_path`. It MUST reject a socket path over 107 bytes
  through `check_socket_path`. Test: unit `test_missing_runtime_dir_fails_loudly`,
  `test_overlong_socket_path_is_rejected`.
- **REQ-GW-04** When a socket file already exists, the gateway MUST connect to it: on
  `ECONNREFUSED` it MUST unlink it and bind; if the connect succeeds it MUST exit with "another
  gateway is running". Test: integration `test_stale_socket_is_replaced`,
  `test_second_gateway_refuses_to_start`.
- **REQ-GW-05** The TCP listener MUST be off by default (`[gateway].tcp = false`); `hal
  connect` (06) and `hal remote authorize` (07) turn it on. When on, it MUST bind only to
  `127.0.0.1` or `::1`; any other `tcp_host` is a config error (REQ-SYS-09). Test: unit
  `test_tcp_is_off_by_default`, `test_tcp_host_must_be_loopback`.
- **REQ-GW-06** Every TCP request except `GET /health` MUST carry the owner token, a valid
  client token or a valid session token as `Authorization: Bearer <t>` or `X-Api-Key: <t>`;
  token secrets MUST be compared with `hmac.compare_digest`. A missing or wrong token MUST
  get 401 in the route's error format. Test: unit `test_tcp_request_without_token_is_401`,
  `test_x_api_key_header_is_accepted`.
- **REQ-GW-07** The owner token file MUST be created with mode 0600 at the first gateway start
  if it is missing, and the gateway MUST refuse to start the TCP listener if the file is
  readable by group or others. Test: unit `test_token_file_is_created_0600_at_first_start`,
  `test_world_readable_token_file_blocks_tcp`.
- **REQ-GW-08** On TCP, a request whose `Host` header is not `127.0.0.1:<port>`,
  `localhost:<port>`, `[::1]:<port>` or a value in `allow_hosts` MUST get 403, and a request
  with an `Origin` header not in `allow_origins` (empty by default) MUST get 403 (REQ-SYS-10,
  REQ-SEC-75, ATK-09). Test: unit `test_dns_rebinding_host_is_403`,
  `test_browser_origin_is_403`.
- **REQ-GW-09** Mutating admin routes (`POST`/`DELETE` under `/hal/v1/`) MUST be served only
  on the unix socket to the owner principal; on TCP they MUST return 403. Read-only admin
  routes MAY be served on TCP to the owner token. Client and session principals MUST get 403
  on every admin route. Test: unit `test_admin_mutation_over_tcp_is_forbidden`,
  `test_session_cannot_reach_admin`, `test_client_token_cannot_reach_admin`.

### Routes and passthrough (REQ-GW-1x)

- **REQ-GW-10** The gateway MUST serve exactly the routes in the route table (Design,
  "Route table") and return 404 in a neutral JSON body for any other path. Routes MUST be
  matched on the path without the query string. Test: unit `test_unknown_path_is_404`,
  `test_llama_internal_routes_are_not_exposed`, `test_query_string_is_ignored_for_matching`.
- **REQ-GW-11** For model routes, the gateway MUST resolve the request's `model` field to a
  target, replace it with the upstream model name, and forward every other field and the
  query string unchanged. Test: unit `test_alias_is_rewritten_and_other_fields_kept`.
- **REQ-GW-12** Responses MUST be streamed to the client as they arrive: each chunk read with
  `read1(65536)` MUST be written and flushed before the next read. Test: integration
  `test_sse_events_arrive_before_stream_ends` (fake backend holds the stream open after the
  first event).
- **REQ-GW-13** The relay MUST NOT buffer more than one read chunk per stream; a slow client
  MUST slow the upstream read. Test: integration `test_slow_reader_keeps_memory_flat` (20,000
  events, relay buffer size asserted through an instrumented relay, not RSS).
- **REQ-GW-14** Hop-by-hop headers (`Connection`, `Keep-Alive`, `Proxy-*`, `TE`, `Trailer`,
  `Transfer-Encoding`, `Upgrade`), `Host`, `Content-Length`, `Authorization`, `X-Api-Key`,
  `Cookie` and every `X-Hal-*` header from the client MUST NOT be forwarded; the gateway MUST
  add the backend's own credentials. Test: unit `test_client_credentials_never_reach_backend`.
- **REQ-GW-15** A `POST` without `Content-Length` MUST get 411 and a request with
  `Transfer-Encoding` MUST get 400, both before any body is read. Test: unit
  `test_chunked_request_body_is_rejected`.
- **REQ-GW-16** A body larger than the principal's `max_request_bytes` MUST get 413 without
  the body being read, and the connection MUST be closed. Test: unit
  `test_oversized_body_is_413_without_reading`.
- **REQ-GW-17** `GET /v1/models` MUST return the models the principal may use, in the
  Anthropic list shape when the request has an `anthropic-version` header and in the OpenAI
  list shape otherwise, with duplicate ids removed. Each OpenAI-shape entry MUST carry
  `context_length` when it is known (Design, "Model listing") and `meta.hal_profile` when the
  model's manifest entry names a profile. Test: unit
  `test_models_listing_shape_follows_anthropic_header`, `test_models_listing_has_no_duplicates`,
  `test_models_listing_carries_context_length_and_profile`.
- **REQ-GW-18** `POST /v1/messages/count_tokens` to a backend that does not serve it MUST
  return `{"input_tokens": N}` estimated as `ceil(len(canonical JSON of system, messages and
  tools) / 4)` with the header `X-Hal-Estimate: chars/4`. Test: unit
  `test_count_tokens_is_estimated_for_ollama`.
- **REQ-GW-19** A request for an API the target backend does not serve MUST get 404 with a
  message naming the backend and the APIs it serves (`/v1/responses/input_tokens` gets no
  estimate). Test: unit `test_unsupported_api_names_backend_apis`.

### Aliases and backends (REQ-GW-2x)

- **REQ-GW-20** Model names MUST resolve in the order given in Design, "Alias resolution",
  and an ambiguous bare name MUST get 409 listing the candidates. Test: unit
  `test_resolution_order` (table-driven), `test_ambiguous_bare_name_is_409`.
- **REQ-GW-21** A backend whose URL is not loopback or a unix socket MUST be rejected at config
  load unless it has `allow_remote = true`, and a remote backend MUST use `https`. Test: unit
  `test_remote_backend_needs_allow_remote`, `test_remote_backend_needs_https`.
- **REQ-GW-22** For each remote backend the gateway MUST log a warning at startup, list it as
  remote in `hal-gateway status`, add `X-Hal-Remote: <host>` to each response from it, and mark
  its entries in `/v1/models` with `"owned_by": "remote:<host>"`. Test: unit
  `test_remote_backend_is_visibly_marked`.
- **REQ-GW-23** Backend API keys MUST come only from `api_key_file` (mode 0600 or stricter)
  or `api_key_env`; a key written inline in the config MUST be a config error. Test: unit
  `test_inline_api_key_is_rejected`, `test_loose_key_file_is_rejected`.
- **REQ-GW-24** The Ollama native routes MUST be limited to `/api/chat`, `/api/generate`,
  `/api/embed`, `/api/embeddings`, `/api/show`, `/api/tags`, `/api/ps` and `/api/version`;
  `pull`, `push`, `create`, `copy`, `delete` and `blobs` MUST return 404. Test: unit
  `test_ollama_management_routes_are_absent`.
- **REQ-GW-25** Session principals MUST get 403 on every `/api/*` route, matching the
  forwarder allowlist of 01-security REQ-SEC-66 (no Ollama native route from a box). Test:
  unit `test_session_cannot_use_ollama_native_routes`.
- **REQ-GW-26** The role aliases `hal-chat`, `hal-classifier`, `hal-code`, `hal-writer`,
  `hal-embed` and `hal-vision` MUST resolve from `[hal].chat_model`, `classifier_model`,
  `code_model`, `writer_model`, `embed_model` and `vision_model` (00-overview, "The `[hal]`
  table"); an empty `code_model` or `writer_model` MUST mean `chat_model`, and an empty
  `classifier_model` MUST fall back to the `fast_model` of a config from before 0.2.0
  (`test_an_old_fast_model_still_feeds_the_classifier_alias`). The configured value
  MUST resolve through steps 3 to 5 of "Alias resolution", and a value that names a role alias
  MUST be a config error. Test: unit `test_role_aliases_follow_hal_table`,
  `test_empty_code_model_means_chat_model`, `test_role_alias_cannot_name_role_alias`.
- **REQ-GW-27** Every role alias MUST appear in `/v1/models` for principals allowed to use its
  target, with the target's `context_length`. Test: unit `test_role_aliases_are_listed`.

### Managed llama-server router (REQ-GW-3x)

- **REQ-GW-30** The gateway MUST render a mode INI by replacing `@MODELS@`, `@HAL@`,
  `@STATE@` and `@MODES@` (Design, "Mode files"), remove the `[hal]` section, and write the
  result to `<state>/gateway/rendered/<mode>.ini` with mode 0600. Test: unit
  `test_render_replaces_tokens_and_drops_hal_section`.
- **REQ-GW-31** Rendering MUST fail with an error naming the key when a preset sets a
  denied key (Design, "Denied preset keys"), when `slot-save-path` resolves outside
  `<state>/gateway/slots`, or when a `model`, `model-draft` or `mmproj` path is not a `.gguf`
  file. Test: unit `test_denied_preset_key_is_refused`,
  `test_slot_save_path_outside_state_is_refused`, `test_non_gguf_model_is_refused`.
- **REQ-GW-32** The managed router's argv and environment MUST be built by
  `llama_server_command` as 01-security defines it (REQ-SEC-76, "Managed llama-server"):
  `--host` set to `127.0.0.1` or a unix socket under `<state>/gateway/run/`, an explicit
  `--port` on TCP, `--models-preset`, `--models-max`, `--api-key-file` pointing at a generated
  0600 key, and `--no-webui`, with the environment allowlist of 01-security and nothing else.
  Test: unit `test_llama_args.py` (01 names it); integration `test_router_argv_and_env` (fake
  llama-server records argv and env).
- **REQ-GW-33** Before starting the router, the gateway MUST unload every Ollama model with
  `size_vram > 0` on the Ollama backends listed in `unload_ollama`. Test: unit
  `test_ollama_models_are_unloaded_before_router_start` (fake Ollama).
- **REQ-GW-34** After starting, the gateway MUST poll `/models` every second until every
  startup preset reports `loaded`, and MUST stop the router and fail with the log path when a
  preset reports `failed`, drops from `loading` to `unloaded`, the process exits, or
  `load_timeout_s` (900) passes. Test: unit `test_load_wait_outcomes` (table-driven over the
  fake router's status sequences).
- **REQ-GW-35** Stopping MUST send `SIGTERM`, wait up to `stop_timeout_s` (60), then send
  `SIGKILL`, and MUST only signal a pid whose start time matches the recorded `pid_start`.
  Test: integration `test_stop_escalates_to_sigkill`, `test_reused_pid_is_never_signalled`.
- **REQ-GW-36** On gateway start, a running router recorded in the pid file with the same
  mode and render hash MUST be adopted, and one with a different mode or hash MUST be stopped.
  Test: integration `test_router_is_adopted_across_gateway_restart`.
- **REQ-GW-37** Withdrawn (0.2.0): the `hal-serve` launcher is gone, so there is no legacy router
  to guard against.
- **REQ-GW-38** A mode switch MUST wait up to `drain_s` (30) for in-flight requests to the
  router and then fail with 409 unless `force` is set. Test: unit
  `test_mode_switch_waits_for_inflight_requests`.
- **REQ-GW-39** After a router crash, the next request MUST restart it, at most
  `max_restarts` (3) times in 600 s; beyond that requests MUST get 503 naming the log. Test:
  unit `test_crash_restart_budget`.

### Managed OpenAI-compatible servers (REQ-GW-9x)

- **REQ-GW-90** A `managed-openai` backend MUST name a non-empty `command` whose first item is
  an absolute path and a non-empty `served_models`, MUST NOT set `url` (it is derived from
  `port`), and its `env` table MUST refuse a name that looks like a secret under 01-security's
  key rule, `PORT`, `HOST` and the `key_env` name. Test: unit
  `test_managed_server_needs_a_command_and_its_models`,
  `test_managed_server_env_refuses_secret_looking_names`.
- **REQ-GW-91** The managed server's environment MUST be exactly the 01-security allowlist,
  the configured `env`, `HOST=127.0.0.1`, `PORT` and, when `key_env` is set, the generated key
  from `<state>/gateway/<name>.key` (mode 0600). The key MUST NOT appear in argv. Test:
  integration `test_child_gets_only_allowlisted_env_plus_port_host_and_key`,
  `test_shell_secrets_never_reach_the_child`, `test_key_is_never_in_the_argv`.
- **REQ-GW-92** After a start, the gateway MUST poll `health_path` and `/v1/models` every
  second until the first answers 200 and the second lists a served model, and MUST stop the
  process group and fail with the log path when the process exits or `startup_timeout_s` (300)
  passes. Test: integration `test_start_waits_for_health_and_writes_the_record`,
  `test_exit_during_startup_fails_with_the_log_path`,
  `test_startup_timeout_stops_the_process_group`.
- **REQ-GW-93** Stopping a managed process (router or server) MUST signal its whole process
  group, `SIGTERM` then `SIGKILL` after `stop_timeout_s`, and wait until no process of the group
  is left, including children whose leader already exited; a group whose leader pid now has
  another start time MUST NOT be signalled. On gateway start a recorded server with the same
  argv, cwd, env and port hash MUST be adopted, and any other stopped. Test: integration
  `test_stop_ends_the_whole_process_group`, `test_stop_escalates_to_sigkill`,
  `test_stop_ends_children_left_after_the_leader_exited`,
  `test_server_is_adopted_across_gateway_restart_when_config_is_unchanged`,
  `test_changed_config_stops_the_old_server_at_start`.
- **REQ-GW-94** A request to a managed server MUST start it on demand, restart it after a crash
  at most `max_restarts` times in 600 s, and get 503 after `backend/stop` until the next start;
  `/v1/models` MUST list `served_models` without contacting or starting it, and effort hints
  MUST map as for an `openai` backend. Test: integration
  `test_request_starts_the_server_on_demand_and_carries_its_key`, `test_crash_restart_budget`,
  `test_stopped_server_is_not_restarted_by_a_request`,
  `test_model_listing_shows_served_models_without_starting_the_server`,
  `test_effort_off_disables_thinking_on_a_managed_server`.
- **REQ-GW-95** Before a managed server with `exclusive_gpu` starts, the gateway MUST stop the
  router and other managed GPU servers without the deliberate-stop mark (the next request to
  them starts them again) and unload the Ollama models of `unload_ollama`; a router start MUST
  stop an exclusive managed server the same way. A backend with requests in flight MUST NOT be
  stopped: the start fails with 503. Test: integration
  `test_managed_start_yields_the_router_and_empties_ollama`,
  `test_managed_start_refuses_to_stop_a_backend_with_requests_running`,
  `test_router_start_stops_the_managed_server_without_marking_it_stopped`, unit
  `test_yield_gpu_stops_the_router_without_marking_it_stopped`,
  `test_router_start_calls_before_start_first`.
- **REQ-GW-96** The idle unload MUST NOT stop a managed server with `idle_stop = false`; a GPU
  release (`release-gpu`, a reclaim) MUST stop it and report its models, and `release-gpu
  --stop-router` MUST stop it with the deliberate-stop mark. Test: integration
  `test_idle_unload_keeps_a_server_with_idle_stop_false`,
  `test_free_vram_stops_the_server_and_returns_its_models`,
  `test_release_gpu_stop_router_stops_managed_servers`.

### Router policies (REQ-GW-4x)

- **REQ-GW-40** Context overflow: a 400 response whose JSON `error.type` is
  `exceed_context_size_error` MUST be rewritten, on Anthropic routes, to
  `{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: <n_prompt_tokens> tokens > <n_ctx> maximum"}}`,
  and on OpenAI routes to an error with `"code": "context_length_exceeded"`. Any other body
  MUST pass unchanged. Test: unit `test_overflow_maps_to_claude_code_message`,
  `test_overflow_maps_to_openai_code`, `test_other_400_passes_unchanged`.
- **REQ-GW-41** Effort: on an alias with the effort policy, a request with tools whose last
  non-system message is a decision point (Design, "Effort policy") MUST get the effort note
  appended as a final system message; other requests MUST be unchanged. Test: unit
  `test_decision_points` (ported cases: user text, subagent report, failing Bash, exit code 1
  of a search, tool error, plain tool result), for both Anthropic and OpenAI formats.
- **REQ-GW-42** Route: on an alias with the route policy, each request that carries new user
  text MUST be classified once through `POST /v1/systemone` on the classifier's backend and the
  choice MUST hold for every request of that conversation until the next user text. Requests
  without tools MUST go to the fast model. Test: unit `test_route_choice_holds_for_turn`,
  `test_side_request_goes_fast`.
- **REQ-GW-43** When the classifier fails or times out (30 s), the route policy MUST use its
  `default` choice and log the failure. Test: unit `test_classifier_failure_uses_default`.
- **REQ-GW-44** The route policy's per-conversation memory MUST be bounded (LRU, 1024
  conversations). Test: unit `test_route_memory_is_bounded`.
- **REQ-GW-45** Delegate: for a strong-model request, tools named in `strip_tools` MUST be
  removed and the delegate note appended to the system prompt. Test: unit
  `test_delegate_strips_tools_and_adds_note`.
- **REQ-GW-46** Swap: on a router backend with swap enabled, a request that needs the model
  not loaded MUST wait until no request is in flight, `quiet_s` (2.0) has passed since the
  last one, and (when leaving the fast model) no fast-model conversation was active within
  `agent_stale_s` (900); then the gateway MUST save the strong model's slots `0..swap_slots-1`,
  unload the loaded model, load the other, and restore the slots when the strong model
  returns. Test: unit `test_swap_waits_for_quiet_and_subagents`,
  `test_swap_saves_and_restores_strong_slots` (fake clock, fake router).
- **REQ-GW-47** A request waiting for a swap longer than `swap_wait_s` (7200) MUST get 503
  with `Retry-After`. A failed swap MUST return 503 for that request, re-read which model is
  loaded, and let the next request try again. Test: unit `test_swap_wait_times_out`,
  `test_failed_swap_recovers_state`.
- **REQ-GW-48** Side requests (no tools, body under 50,000 characters of messages) MUST run
  on whichever model is loaded, and when none is loaded the gateway MUST load the main model.
  Test: unit `test_side_request_uses_loaded_model`, `test_side_request_loads_main_when_idle`.
- **REQ-GW-49** Every policy decision MUST be recorded in the request's log record
  (`policy` field). Test: unit `test_policy_decisions_are_logged`.

### GPU coordination (REQ-GW-5x)

- **REQ-GW-50** With `coordinator = "auto"`, the gateway MUST use gpu-light v2 when the GL
  socket (`$GPU_LIGHT_SOCKET`, else `$XDG_RUNTIME_DIR/gpu-light/daemon.sock`, or
  `gpu.v2_socket`) answers `hello` with protocol 2 within 2 s; a server with another protocol
  major MUST NOT be used (GL REQ-API-06) and MUST be logged. Otherwise it MUST use gpu-light
  v1 file claims when the v1 state directory exists and is owned by the user with mode 0700,
  else no coordination. Test: unit `test_coordinator_autodetect` (table-driven),
  `test_v2_protocol_mismatch_is_not_used`.
- **REQ-GW-51** With v2, before forwarding to a backend with `gpu = true` while no claim is
  held for the request's class (`interactive` or `service`), the gateway MUST send a GL `claim` (Design, "gpu-light v2") on its own
  long-lived connection with lifetime `connection`, and MUST wait up to `claim_wait_s` (10)
  for `granted` or a `claim_granted` notification. On timeout it MUST proceed and log a
  warning when `on_busy = "proceed"` (default), or return 503 when `on_busy = "fail"`. Test:
  unit `test_v2_claim_follows_gl_methods`, `test_claim_wait_timeout_proceeds`,
  `test_claim_wait_timeout_fails_when_configured`.
- **REQ-GW-52** v1 claims MUST follow gpu-light protocol 1: `claims/<name>.json` with
  `lifetime: "pid"` bound to the gateway's pid and `pid_start`, written atomically with
  `fsync` of file and directory, and removed on release. The gateway MUST then wait until
  `light.json` shows an empty `background` list or `v1_claim_wait_s` (120) passes. Test: unit
  `test_v1_claim_file_matches_protocol` (validated against gpu-light's own `records.py`
  rules, copied as fixtures), `test_v1_claim_is_removed_on_release`.
- **REQ-GW-53** With v2, a claim MUST be released (`claim.release`) `idle_release_s` (120)
  after the last in-flight request of its class ends, and the models MUST stay loaded (gpu-light frees them
  through the reclaimer, REQ-GW-59). With v1 or no coordinator, after `idle_unload_s` (900)
  with no request in flight to a GPU backend, the gateway MUST unload the models it loaded
  (router: `/models/unload` for each loaded preset; Ollama: `keep_alive: 0` for the models it
  served) and release the v1 lease. `0` MUST disable either timer. Test: unit
  `test_v2_idle_releases_claim_and_keeps_models`, `test_v1_idle_timer_unloads_and_releases`
  (fake clock).
- **REQ-GW-54** While the off flag `<state>/off` exists and `respect_off_flag = true`, a
  request to a GPU backend from a principal that holds no lease MUST get 503 "Hal is off; run
  `hal on`" and no claim MUST be taken; requests from sessions holding a lease (REQ-GW-60)
  and requests to non-GPU backends MUST keep working. Test: unit
  `test_off_flag_blocks_gpu_backends_only`, `test_leased_session_works_while_off`.
- **REQ-GW-55** `POST /hal/v1/gpu/release` MUST, without `force`, change nothing and return
  the kept leases when any lease is held; with `reclaim = true` it MUST ignore leases and
  change nothing while a request is in flight, returning `busy`. Otherwise it MUST unload the
  models the gateway loaded or served on GPU backends, stop the managed router when
  `stop_router = true`, release every claim, and return a `ReleaseResult`. Test: unit
  `test_release_keeps_leased_gpu`, `test_force_release_ignores_leases`,
  `test_reclaim_is_refused_while_busy`, `test_release_can_stop_router`.
- **REQ-GW-56** Before starting the router or acquiring a claim, the gateway MUST query each
  configured peer's `/models` (timeout `peer_timeout_s`, 3) and refuse with 409 `gpu_busy_peer`
  naming the peer when any peer model reports `loaded`. An unreachable peer MUST be treated as
  not holding the GPU, with a warning. Test: unit `test_peer_holding_gpu_blocks_load`,
  `test_unreachable_peer_does_not_block`.
- **REQ-GW-57** After acquiring a claim, the gateway MUST unload Ollama models that sit
  partly in system RAM (`size_vram < size`) on Ollama backends with
  `unload_offloaded_on_claim = true`. Test: unit `test_offloaded_ollama_models_are_unloaded`.
- **REQ-GW-58** All gateway state MUST live under `<state>/gateway/`, where `<state>` is
  `halkit.paths.state_dir()`, so two installs with different `HAL_STATE_DIR`,
  `HAL_RUNTIME_DIR`, ports and claim names run side by side. Test: integration
  `test_two_instances_do_not_share_state`.
- **REQ-GW-59** With v2, at start and after each reconnect, the gateway MUST make sure a GL
  `command` reclaimer named `reclaimer_name` (default `hal-gateway`) exists with the spec in
  Design, "gpu-light v2": it MUST add it when absent, replace a CLI-added one whose spec
  differs, and leave a config-defined one alone with a warning when it differs. Test: unit
  `test_reclaimer_is_registered_once`, `test_stale_cli_reclaimer_is_replaced`,
  `test_config_reclaimer_is_not_touched`.

### Sessions, client tokens and forwarders (REQ-GW-6x)

- **REQ-GW-60** `POST /hal/v1/sessions` MUST mint a session token, store only the SHA-256 of
  its secret, and return it once. A session MUST carry a GPU `class` (`interactive` or
  `service`) and a `lease` flag. Tokens MUST expire after `ttl_s` and MUST be revocable with
  `DELETE /hal/v1/sessions/<id>`. Sessions MUST NOT survive a gateway restart. Test: unit
  `test_session_token_lifecycle`, `test_only_token_hash_is_stored`.
- **REQ-GW-61** A session principal MUST be limited to its alias list (glob patterns), MUST
  NOT resolve remote aliases unless minted with `allow_remote = true`, and MUST be limited to
  its `max_request_bytes` and `max_concurrent` (429 beyond it). Test: unit
  `test_session_alias_scope`, `test_session_cannot_reach_remote_by_default`,
  `test_session_concurrency_cap_is_429`.
- **REQ-GW-62** A request carrying a session or client token MUST get that principal's scope
  on every listener, including the unix socket, so such a token can never widen a principal.
  Test: unit `test_session_token_narrows_unix_owner`, `test_client_token_narrows_unix_owner`.
- **REQ-GW-63** `SessionForwarder` MUST listen on the given socket path with mode 0600, pass
  only the routes of 01-security REQ-SEC-66 (matched on the path without the query string)
  and answer 403 to every other path without contacting the gateway (ATK-33), strip every
  client credential and `X-Hal-*` header, inject its token, enforce its own size and
  concurrency caps, time out a request head or body that takes longer than `read_timeout_s`
  (30), and stream responses back unchanged. Test: integration
  `test_forwarder_injects_token_and_strips_auth`, `test_forwarder_rejects_admin_path_with_403`,
  `test_forwarder_matches_path_without_query`, `test_forwarder_slowloris_times_out`,
  `test_forwarder_streams_sse`.
- **REQ-GW-64** When the gateway answers 401 `session_unknown`, the forwarder MUST call its
  `on_event` callback with `token_rejected` so the owner can mint a new token and call
  `set_token`. Test: unit `test_forwarder_reports_rejected_token`.
- **REQ-GW-65** `SessionForwarder` MUST relay either to the gateway's unix socket or, when
  given `gateway_url`, to a loopback `http://` URL (the `hal remote` tunnel end); any other
  URL MUST be refused at construction. Test: integration `test_forwarder_relays_to_tcp_url`,
  unit `test_forwarder_refuses_non_loopback_url`.
- **REQ-GW-66** `hal-gateway token issue --name NAME` MUST mint a client token, store only
  the SHA-256 of its secret with the name in `<state>/gateway/tokens.json` (0600), and print
  the token once; `token revoke --name NAME` MUST make the token fail with 401 on the next
  request without a gateway restart; `token list` MUST show names and dates, never secrets.
  Client tokens MUST survive restarts. Test: unit `test_client_token_lifecycle`,
  `test_revoked_client_token_fails_without_restart`, `test_token_list_has_no_secrets`.
- **REQ-GW-67** A client principal MUST reach the model routes like the owner, MUST NOT reach
  remote backends unless issued with `--allow-remote`, and MUST appear in the request log as
  `client:<name>`. Test: unit `test_client_scope`, `test_client_name_is_logged`.
- **REQ-GW-68** When `[gateway].url` is set (client mode), `hal-gateway serve` MUST refuse to
  start with an error naming the key, and `url` MUST be a loopback `http://` URL. Test: unit
  `test_serve_refuses_in_client_mode`, `test_gateway_url_must_be_loopback`.
- **REQ-GW-69** `SessionForwarder` MUST enforce a per-session model budget on the host, so a
  process in the box that bypasses the agent's own budgets (03 REQ-AGT-16) by calling
  `/run/hal/model.sock` directly is still bounded: at most `max_requests_per_min` (120)
  requests in any 60 s window; a `max_tokens`, `max_completion_tokens` or
  `max_output_tokens` value above `max_output_tokens` (16384), or a missing one, MUST be set
  to `max_output_tokens` before the body is relayed (with `Content-Length` recomputed); and at most `max_tokens_per_hour`
  (100,000,000) input plus output tokens in any 3600 s window, counted from the response
  `usage` (the JSON body, or the final SSE usage event), or as the clamped output cap plus
  the request body length / 4 when a response carries no usage. Over a limit the forwarder
  MUST answer 429 `budget_exceeded` with `Retry-After` set to the seconds until the window
  frees enough room, without contacting the gateway, and MUST call `on_event` with
  `rejected` and the limit's name. The budget lives in the forwarder process, so it holds in
  client mode too, and resets only when the forwarder is closed (security-gaps review
  G22). Test: unit `test_session_token_budget_is_enforced_host_side`,
  `test_forwarder_clamps_max_tokens`, `test_forwarder_request_rate_is_429_with_retry_after`,
  `test_forwarder_counts_estimate_when_usage_missing`.

### Logging, limits, operation and models (REQ-GW-7x)

- **REQ-GW-70** Every request MUST produce one JSONL record in
  `<state>/gateway/requests.jsonl` with the fields of Design, "Request log", and no message
  content, headers or tokens. With `log.content = true`, request and response bodies MAY be
  written to `<state>/gateway/content/<id>.json` with mode 0600. Test: unit
  `test_request_record_has_no_content`, `test_content_log_is_opt_in`.
- **REQ-GW-71** The managed router MAY run in a container (`launcher = "docker"`) with
  `--runtime=<container_runtime>` (default `nvidia`), `--gpus all`, `--network none`,
  `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, the models directory
  mounted read-only and the router socket directory mounted read-write, listening on a unix
  socket. Test: unit `test_docker_argv_uses_nvidia_runtime`; docker+gpu
  `test_containerized_router_serves_over_socket`, manual drill.
- **REQ-GW-72** The gateway MUST cap open connections at `max_connections` (64) and answer
  503 with `Retry-After: 1` beyond it. Test: unit `test_connection_cap_is_503`.
- **REQ-GW-73** Upstream connects MUST time out after `upstream_connect_timeout_s` (10) and
  reads after `upstream_read_timeout_s` (3600) without a byte; client sockets after
  `client_timeout_s` (300) without progress. A timeout before the response starts MUST give
  504; after it starts, the gateway MUST close the connection. Test: unit
  `test_upstream_connect_timeout_is_504`, `test_stalled_client_is_dropped`.
- **REQ-GW-74** The request log MUST rotate at `log.max_bytes` (10 MiB) keeping `log.keep`
  (3) files, and a failed log write MUST be reported once per minute at ERROR and MUST NOT
  fail the request. Test: unit `test_request_log_rotates`, `test_log_failure_does_not_fail_request`.
- **REQ-GW-75** Every `model`, `model-draft` and `mmproj` path in a rendered mode MUST have an
  entry in `models.toml` (Design, "Model manifest") with format `gguf` or `safetensors`, and
  its SHA-256 MUST match before the first load and again whenever the file's size, mtime or
  inode changes (REQ-SEC-79); otherwise the mode MUST be refused. A pickle format or path
  (`.pt`, `.pth`, `.ckpt`, `.bin`, `.pkl`, `.pickle`) MUST be refused (REQ-SEC-80, ATK-32).
  Test: unit `test_model_manifest.py` (01 names it): `test_unlisted_model_is_refused`,
  `test_hash_mismatch_is_refused`, `test_changed_inode_forces_rehash`,
  `test_pickle_format_is_refused`.
- **REQ-GW-76** `GET /health` MUST answer `{"status":"ok"}` without authentication and
  without touching any backend (one of the two REQ-SYS-10 exceptions, resolution G6). Test:
  unit `test_health_is_public_and_local`.
- **REQ-GW-77** A request to an Ollama backend MUST get 503 `model_unverified` unless
  `models.toml` has an `ollama:` entry for the upstream model whose digest equals the digest
  `/api/tags` reports (cached 30 s). Test: unit `test_unpinned_ollama_model_is_refused`,
  `test_moved_ollama_tag_is_refused`.
- **REQ-GW-78** A `chat-template-file` in a rendered mode MUST be a bundled file listed with
  its sha256 in `halgateway/modes/SHA256SUMS`, or match the `template_sha256` of the
  preset's model entry (REQ-SEC-81). Test: unit `test_unpinned_template_is_refused`.
- **REQ-GW-79** `halgateway.models.pull` MUST download only over HTTPS from a source pinned to
  a commit (`hf:<repo>@<40-hex commit>`), write to a temporary file in the target directory,
  verify the manifest sha256, and rename into place only on a match; a mismatch MUST delete
  the temporary file and raise (REQ-SEC-82). An existing file with another hash MUST NOT be
  overwritten. Test: unit `test_pull_verifies_before_rename`,
  `test_pull_refuses_unpinned_source`, `test_pull_never_overwrites_a_different_file`.
- **REQ-GW-80** `halgateway.models.verify(names, full=True)` MUST re-hash every named file and
  report each mismatch; `full=False` MUST re-hash only files whose size, mtime or inode
  changed. Test: unit `test_verify_reports_mismatch`, `test_quick_verify_uses_cache`.

- **REQ-GW-81** On Chat Completions requests the gateway MUST read the effort hint (`X-Hal-Effort`
  header, else the `hal_effort` body field; `off|low|medium|high|max`), merge the matching request
  fields for the model family and backend without overwriting a field the client set, remove
  `hal_effort` before forwarding, and ignore the hint with a log line for a model that cannot think
  or an unknown level. A role alias without a hint MUST use the role's `effort` and `temperature`;
  so MUST an alias `hal-NAME` when `[models.roles.NAME]` is a named role (06-core-cli REQ-CLI-121).
  Test: unit `test_header_beats_the_body_field_and_the_field_never_goes_upstream`,
  `test_a_model_that_cannot_think_is_left_alone_with_a_note`,
  `test_role_default_effort_and_temperature_apply_to_a_role_alias_only`,
  `test_a_named_role_alias_names_its_role`; integration
  `test_gateway_header_overrides_the_role_default_and_is_not_forwarded`.
- **REQ-GW-82** The generated mode `tuned` MUST pass the same checks as any mode (pinned weight
  files, no forbidden key) and MUST take context, KV cache type, GPU layers and tensor split from
  `halkit.tuning`, never from constants. Test: unit
  `test_the_generated_mode_renders_through_the_normal_mode_checks`,
  `test_an_unpinned_weight_file_is_refused_by_the_mode_checks`.

## Design

### Components and data flow

```text
client (Claude Code, Codex, hal-agent, halkit.llm, curl)
  |  HTTP/1.1 over unix socket (owner)  or  127.0.0.1:8741 + owner/client/session token
  v
+------------------------------- halgateway (one process) ------------------------------+
| listeners: UnixHTTPServer, LoopbackHTTPServer (ThreadingMixIn, daemon threads)        |
|   -> Handler: limits, Host/Origin, auth (Principal), route table                      |
|   -> Resolver: model -> Target(alias, backend, upstream model, policies)              |
|   -> manifest gate (Ollama digest, router files checked at render)                    |
|   -> policy pipeline: route -> delegate -> effort -> GPU gate -> swap admit           |
|   -> Relay: upstream connection, header filter, streaming copy, usage scan            |
|   -> overflow map (on 400) -> swap finish -> RequestLog                               |
| background: GpuManager (claims, idle timers, leases, reclaim), Router supervisor,     |
|             Sessions, ClientTokens (tokens.json, re-read on change)                   |
+-----------------------+----------------------+------------------------------------------+
                        |                      |
          managed llama-server router     Ollama 127.0.0.1:11434     https://remote (opt-in)
          127.0.0.1:8080 + api key
```

Sandbox path:

```text
container agent --HTTP--> /run/hal/model.sock  (bind mount of $R/s/<sid>/run/model.sock, 02)
   host: SessionForwarder (in the hal host process) --session token--> gateway.sock
   laptop (client mode): SessionForwarder --client token--> http://127.0.0.1:8741 (tunnel, 07)
```

One request, step by step:

1. The listener accepts; the connection semaphore (`max_connections`) is taken or the
   client gets 503.
2. The handler reads the request line and headers (`max_header_bytes`, `client_timeout_s`),
   checks `Host` and `Origin` on TCP, resolves the principal (unix peer uid, session token,
   client token, owner token), and finds the route.
3. Length checks (411, 400, 413), then the body is read and parsed as a JSON object
   (400 `invalid_json` otherwise).
4. The resolver maps `model` to a `Target`; scope checks run (403 or 404); for Ollama
   targets the manifest digest check runs (503 `model_unverified`).
5. Policies rewrite the body; the GPU gate checks the off flag and leases, then takes the
   claim for the request's class if needed; the swap policy admits the request (may block).
6. The backend is made ready (`ensure_ready`: starts the managed router on demand).
7. The relay opens the upstream connection, sends the rewritten body, and copies the
   response. A 400 body is read whole (capped at 1 MiB) for overflow mapping.
8. `finally`: policies finish (swap bookkeeping), activity counters drop, the log record is
   written.

### Package and file layout

```text
packages/hal-gateway/
  pyproject.toml                 name "hal-gateway", requires hal-kit (workspace), no third party;
                                 scripts: hal-gateway = "halgateway.cli:main"
  scripts/bench_relay.py         relay benchmark, run by hand before a release
  src/halgateway/
    __init__.py                  version, public API re-exports
    __main__.py                  python -m halgateway -> cli.main
    cli.py                       hal-gateway CLI; the only module that prints
    config.py                    [gateway] schema, strict validation, defaults
    server.py                    Gateway object, listeners, lifecycle, signal handling
    auth.py                      Principal, owner token, peer credentials, Host/Origin checks
    sessions.py                  SessionRegistry (in memory)
    clienttokens.py              ClientTokens: tokens.json, issue/revoke/list, reload on change
    handler.py                   HTTP handler: limits, auth, dispatch
    routes.py                    route table, Api and Format enums
    aliases.py                   Resolver, Target, ModelEntry, role aliases
    relay.py                     upstream connections (TCP, TLS, unix), header filter, copy loop
    errors.py                    GatewayError tree, error bodies per format
    estimate.py                  count_tokens estimate
    reqlog.py                    RequestLog (JSONL, rotation)
    admin.py                     /hal/v1/* handlers
    client.py                    GatewayClient for halcore (halsandbox never imports halgateway)
    forwarder.py                 SessionForwarder
    modes.py                     mode discovery, [hal] section, rendering, denied keys
    legacy.py                    hal.conf import
    llamacpp.lock.toml           pinned llama.cpp release and hashes (01 REQ-SEC-78; the installer reads it)
    models/
      __init__.py                list_entries, plan, pull, verify, add_local, record_ollama
      manifest.py                models.toml schema, load, verification cache
      download.py                HTTPS download with pinned revision and sha256
    backends/
      __init__.py                Backend protocol, factory
      openai.py                  generic OpenAI-compatible backend (local or remote)
      ollama.py                  Ollama backend, ps/unload/tags helpers (ported from power.py)
      llama_router.py            router backend: external or managed (process supervisor)
      managed.py                 managed-openai backend: a server the gateway starts and stops
      llama_args.py              llama_server_command (01 REQ-SEC-76)
      launchers.py               process launcher and docker launcher for the managed router
    policies/
      __init__.py                Policy protocol, pipeline, RequestContext
      text.py                    user_text, decision_point, conversation_key (ported)
      route.py                   route policy (systemone classifier)
      delegate.py                strip tools + system note
      effort.py                  effort policy
      swap.py                    swap scheduler
      overflow.py                context overflow mapping
    gpu/
      __init__.py                GpuCoordinator protocol, detect(), claim helpers for halcore
      manager.py                 GpuManager: claims, idle timers, off flag, leases, release
      lightv1.py                 gpu-light protocol 1 file claims (FileClaim)
      lightv2.py                 gpu-light v2 JSON-RPC client (stdlib socket)
      peers.py                   peer guard
    modes/                       package data (moved from Hal/modes/)
      gpu.ini gpu-112k.ini cpu.ini hybrid.ini hybrid-256k.ini hybrid-auto.ini
      hybrid-auto-256k.ini combined.ini orchestrate.ini orchestrate-256k.ini
      qwen3.8-chat.jinja combined-prompt.txt orchestrate-prompt.txt effort-xhigh.txt
      SHA256SUMS                 hashes of the bundled templates (REQ-GW-78)
    data/hal-gateway.service     systemd user unit template (06 renders it)
  tests/                         one test file per module (Test plan)
```

`halgateway` imports only `halkit` and the standard library. Nothing in Hal imports
`gpu_light`; `gpu/lightv2.py` speaks GL's socket protocol itself (resolution A6).

Runtime files:

| Path | Mode | Written by | Content |
| --- | --- | --- | --- |
| `$R/` (`runtime_dir()`) | 0700 dir | gateway (`ensure_private_dir`) | runtime dir |
| `$R/gateway.sock` | 0600 | gateway | unix listener |
| `~/.config/hal/gateway-token` | 0600 | gateway (first start), `hal-gateway token rotate` | owner token, one line |
| `~/.config/hal/models.toml` | 0600 | the user, `halgateway.models` (`pull`, `add_local`, `record_ollama`) | model manifest; never written by `serve` |
| `<state>/gateway/` | 0700 dir | gateway | all state below |
| `<state>/gateway/tokens.json` | 0600 | `hal-gateway token issue/revoke` (under `flock`) | client token names, hashes, dates |
| `<state>/gateway/requests.jsonl` (+ `.1` .. `.3`) | 0600 | gateway | request log |
| `<state>/gateway/content/<id>.json` | 0600 | gateway, only with `log.content` | bodies |
| `<state>/gateway/rendered/<mode>.ini` | 0600 | gateway | rendered preset |
| `<state>/gateway/router.json` | 0600 | gateway | `pid`, `pid_start`, `boot_id`, `mode`, `render_sha256`, `address`, `started` |
| `<state>/gateway/router-api-key` | 0600 | gateway | generated key for the managed router |
| `<state>/gateway/router.log` | 0600 | llama-server stdout and stderr | truncated at each start, previous kept as `.1` |
| `<state>/gateway/run/` | 0700 dir | gateway | router socket when `listen = "unix"` |
| `<state>/gateway/slots/` | 0700 dir | llama-server | KV slot files |
| `<state>/gateway/verified.json` | 0600 | gateway, `halgateway.models` | manifest verification cache: path, size, mtime_ns, inode, sha256, `verified_at` |
| `<state>/off` | any | `hal off` (halcore) | off flag, read only |

`<state>` is `halkit.paths.state_dir()`: `$HAL_STATE_DIR`, else `$XDG_STATE_HOME/hal`, else
`~/.local/state/hal`. This is the per-install state dir of an earlier private fork made generic. `$R` is
`halkit.paths.runtime_dir()` (00-overview, one rule for all components).

### Listeners

```python
class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Serves HTTP on an owner-only unix socket."""
    daemon_threads = True
    request_queue_size = 64
    def server_bind(self) -> None: ...      # umask 0o177 around bind, then chmod 0600;
                                             # sets server_name/server_port without getfqdn
    def verify_request(self, request, client_address) -> bool: ...  # SO_PEERCRED uid check

class LoopbackHTTPServer(http.server.ThreadingHTTPServer):
    """Serves HTTP on 127.0.0.1 or ::1 only."""
    daemon_threads = True
    request_queue_size = 64
```

Notes for the implementer, from the prototype:

- `http.server.HTTPServer.server_bind` unpacks `server_address` as `(host, port)` and calls
  `socket.getfqdn`; on `AF_UNIX` the address is a path string, so `UnixHTTPServer` derives
  from `socketserver.UnixStreamServer` and sets `server_name = "localhost"`,
  `server_port = 0` itself.
- On `AF_UNIX` the handler's `client_address` is `""`, so `Handler.address_string()` returns
  `"unix"` instead of indexing it.
- The handler uses `protocol_version = "HTTP/1.1"`. Responses with `Content-Length` keep the
  connection alive; streamed responses without it send `Connection: close` and end by closing
  (close-delimited body), as `hal-route.py` does today.
- `verify_request` rejecting a peer closes the socket without a response; REQ-GW-02 needs a
  403, so the uid check runs in `Handler.setup()` and writes a minimal 403 before closing.
- The TCP listener reads `[gateway].tcp` at start; turning it on or off takes a gateway
  restart (`systemctl --user restart hal-gateway.service`), which `hal connect` and
  `hal remote authorize` do after setting the key.

### Principals and tokens

```python
@dataclass(frozen=True)
class Principal:
    kind: Literal["owner", "client", "session", "anonymous"]
    session: Session | None
    client: ClientToken | None
    listener: Literal["unix", "tcp"]

def principal_for(listener: str, peer_uid: int | None, headers: Mapping[str, str],
                  owner_token: str | None, sessions: SessionRegistry,
                  clients: ClientTokens) -> Principal: ...
```

Rules, in order:

1. A header token that starts with `hal-sess-` is looked up in the session registry: valid
   gives a session principal, otherwise 401 `session_unknown` (or `session_expired`).
2. A header token that starts with `hal-gwc-` is looked up in the client tokens: valid gives
   a client principal, otherwise 401 `invalid_api_key`.
3. On the unix socket, a peer with our uid is the owner; any other header token is ignored
   there (the peer uid already proves the owner, and a stale owner token in `halkit.llm` must
   not break host packs).
4. On TCP, a token equal to the owner token is the owner.
5. Otherwise anonymous, which may reach only `GET /health`.

Token formats:

- Owner: `hal-gw-` + `secrets.token_urlsafe(32)`.
- Client: `hal-gwc-<name>.<secret>`, `name` matching `^[a-z0-9][a-z0-9-]{0,31}$`, `secret` =
  `secrets.token_urlsafe(32)`. `tokens.json` stores per name `sha256(secret)`, `created`,
  `allow_remote`, `class`.
- Session: `hal-sess-<id>.<secret>`, `id` = 8 lowercase hex characters, `secret` =
  `secrets.token_urlsafe(32)`. The registry stores `sha256(secret)`.

Every secret comparison uses `hmac.compare_digest` on the hashes.

### Route table

| Method | Path | Api | Error format | Owner | Client | Session |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/health` | health | openai | yes | yes | yes (anonymous too) |
| POST | `/v1/chat/completions` | chat | openai | yes | yes | yes |
| POST | `/v1/responses` | responses | openai | yes | yes | yes |
| POST | `/v1/responses/input_tokens` | responses_count | openai | yes | yes | yes |
| POST | `/v1/messages` | messages | anthropic | yes | yes | yes |
| POST | `/v1/messages/count_tokens` | count_tokens | anthropic | yes | yes | yes |
| GET | `/v1/models` | models | by `anthropic-version` header | yes | yes (filtered) | yes (filtered) |
| GET | `/v1/models/{id}` | models | as above | yes | yes (filtered) | no |
| POST | `/v1/embeddings` | embeddings | openai | yes | yes | yes |
| POST | `/api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings`, `/api/show` | ollama_native | ollama | yes | yes | no |
| GET | `/api/tags`, `/api/version` | ollama_native | ollama | yes | yes (tags filtered) | no |
| GET | `/api/ps` | ollama_native | ollama | yes | no | no |
| GET | `/hal/v1/status`, `/hal/v1/aliases`, `/hal/v1/sessions`, `/hal/v1/leases` | admin | openai | yes | no | no |
| POST | `/hal/v1/mode`, `/hal/v1/router/stop`, `/hal/v1/models/load`, `/hal/v1/models/unload`, `/hal/v1/gpu/release`, `/hal/v1/sessions`, `/hal/v1/backend/start`, `/hal/v1/backend/stop`, `/hal/v1/backend/restart` | admin | openai | unix only | no | no |
| DELETE | `/hal/v1/sessions/{id}` | admin | openai | unix only | no | no |

The session column equals 01-security REQ-SEC-66 plus `GET /health`, which the forwarder
does not pass (the box has no route to it). `/v1/responses/input_tokens` is forwarded to
backends that list `responses`; whether llama-server b11512 serves it was not checked
[unverified: the `strings` check above did not include it; probe it in the slow tier]. A
backend 404 passes through, and 03 treats it (and a 403) as "no server count".

`/api/tags` returns the exposed Ollama models and the aliases whose backend is Ollama, in
Ollama's `{"models":[{"name","model","modified_at","size","digest","details"}]}` shape (alias
entries copy the upstream model's entry with `name` and `model` set to the alias).

Error bodies:

| Format | Body |
| --- | --- |
| openai | `{"error":{"message":"...","type":"<type>","param":null,"code":"<code>"}}` |
| anthropic | `{"type":"error","error":{"type":"<type>","message":"..."}}` |
| ollama | `{"error":"..."}` |

| Condition | Status | openai `type` / `code` | anthropic `type` |
| --- | --- | --- | --- |
| No or bad token | 401 | `authentication_error` / `invalid_api_key` | `authentication_error` |
| Session unknown or expired | 401 | `authentication_error` / `session_unknown`, `session_expired` | `authentication_error` |
| Foreign `Host` or `Origin` | 403 | `permission_error` / `forbidden_host`, `forbidden_origin` | `permission_error` |
| Scope (admin, alias, remote, route) | 403 | `permission_error` / `forbidden` | `permission_error` |
| Unknown model | 404 | `not_found_error` / `model_not_found` | `not_found_error` |
| API not served by backend | 404 | `not_found_error` / `unsupported_api` | `not_found_error` |
| Ambiguous model | 409 | `invalid_request_error` / `ambiguous_model` | `invalid_request_error` |
| Peer holds GPU, mode drain failed | 409 | `conflict` / `gpu_busy_peer`, `router_busy` | `invalid_request_error` |
| Missing length | 411 | `invalid_request_error` / `length_required` | `invalid_request_error` |
| Body too large | 413 | `invalid_request_error` / `request_too_large` | `request_too_large` |
| Session concurrency, forwarder budget (REQ-GW-69) | 429 | `rate_limit_error` / `too_many_requests`, `budget_exceeded` | `rate_limit_error` |
| Upstream refused or crashed | 502 | `api_error` / `upstream_unavailable` | `api_error` |
| Off flag, swap wait, router down, connection cap, claim refused (`on_busy = "fail"`), model not in manifest or hash mismatch | 503 | `overloaded_error` / `hal_off`, `swap_timeout`, `router_down`, `busy`, `gpu_busy`, `model_unverified` | `overloaded_error` |
| Upstream timeout before response | 504 | `api_error` / `upstream_timeout` | `api_error` |

### Alias resolution

```python
@dataclass(frozen=True)
class Target:
    alias: str              # the public name the client used, after resolution
    backend: Backend
    model: str              # upstream model name
    policies: tuple[Policy, ...]
    remote: bool
    context_length: int | None

class Resolver:
    """Maps a requested model name to a Target for one principal."""
    def resolve(self, requested: str | None, api: Api, principal: Principal) -> Target: ...
    def listing(self, principal: Principal) -> list[ModelEntry]: ...
```

Order (first match wins):

1. An alias from `[gateway.aliases.<name>]`.
2. A role alias (`hal-chat`, `hal-classifier`, `hal-code`, `hal-writer`, `hal-embed`,
   `hal-vision`): its `[hal]` value is resolved with steps 3 to 5 (REQ-GW-26). User aliases
   in step 1 may not use these six names (config error), so the role table stays the single
   source of truth (00-overview).
3. A mode alias of the active mode of a router backend: `hal/main`, `hal/agent`,
   `hal/classifier` (when set), and every preset name of the active mode (`hal-gpu`,
   `hal-hybrid`, ...), so today's names keep working.
4. `<backend>/<model>` for a backend with `expose_models = true`. The backend name `hal` is
   reserved.
5. A bare name present in the model list of exactly one backend with `expose_models = true`
   (lists cached for 30 s); more than one gives 409.
6. The first alias whose `patterns` (fnmatch, case-sensitive) match, in config order. This
   catches the fixed names some harnesses send for side requests.
7. `default_model`, when set.
8. 404 `model_not_found`.

A missing `model` field resolves through step 7 only. For a session principal the result
must also match one of the session's alias globs and, when remote, the session must allow
remote; for a client principal, remote needs `allow_remote`; otherwise 403.

Example: with `[hal].chat_model = "gemma4:12b-it-qat"` and `code_model = "hal/main"`,
`hal-chat` resolves through step 5 to the Ollama backend and `hal-code` through step 3 to the
managed router's main preset.

### Model listing

`GET /v1/models` entries in the OpenAI shape:

```json
{"object": "list", "data": [
  {"id": "hal-code", "object": "model", "created": 0, "owned_by": "local",
   "context_length": 114688, "meta": {"hal_profile": "qwen3"}},
  {"id": "hal-chat", "object": "model", "created": 0, "owned_by": "ollama",
   "context_length": 32768}]}
```

`context_length` comes from, in order: the alias's `context_window` key; for a role alias
(`hal-ROLE`, including a named role's alias) the `context` of `[models.roles.ROLE]`, which
`hal tune` writes for every backend and, for Ollama, carries to the server as a derived model's
`num_ctx` (06, "hal tune"); for router targets the preset's `ctx-size` (the same rule as the
mode's `context-window`, "Mode files"); otherwise it is omitted and clients use their own
fallback (02 uses 32768). `hal-code` and `hal-writer` with no model of their own report chat's.
Test: unit `test_a_role_alias_reports_the_context_hal_tune_set_for_any_backend`.
`meta.hal_profile` is the `profile` key of the model's manifest
entry; 03 reads it to pick an agent profile. The Anthropic shape carries the standard fields
only.

### Backends

```python
class Api(enum.Enum):
    CHAT = "chat"; RESPONSES = "responses"; RESPONSES_COUNT = "responses_count"
    MESSAGES = "messages"; COUNT_TOKENS = "count_tokens"; EMBEDDINGS = "embeddings"
    MODELS = "models"; OLLAMA_NATIVE = "ollama_native"

@dataclass(frozen=True)
class UpstreamAddress:
    scheme: Literal["http", "https", "unix"]
    host: str
    port: int
    path: str | None        # unix socket path

class Backend(Protocol):
    name: str
    remote: bool
    gpu: bool
    expose_models: bool
    gpu_light_uses: tuple[str, ...]
    def supports(self, api: Api) -> bool: ...
    def address(self) -> UpstreamAddress: ...
    def auth_headers(self) -> dict[str, str]: ...
    def list_models(self, timeout_s: float) -> list[str]: ...
    def ensure_ready(self, model: str, deadline: float) -> None: ...   # raises GatewayError
    def free_vram(self) -> list[str]: ...                              # names unloaded
    def status(self) -> dict[str, object]: ...
```

| Type | Default `apis` | `gpu` default | `expose_models` default | `gpu_light_uses` default | Notes |
| --- | --- | --- | --- | --- | --- |
| `llama-router` | all except `ollama_native` | true | true | `[<reclaimer_name>]` | `/v1/models` of the router lists presets; `free_vram` posts `/models/unload` for each loaded preset |
| `ollama` | all except `responses_count` | true | true | `[<reclaimer_name>]` | `count_tokens` is estimated (REQ-GW-18); `free_vram` posts `/api/generate {"model", "keep_alive": 0}` for the models this gateway served; `/v1/models` duplicates and `llamacpp:<sha>` ids are dropped; digests checked (REQ-GW-77) |
| `openai` | `chat`, `models`, `embeddings` | false | true when local, false when remote | `[]` | `apis` must be set to add `responses`, `responses_count` or `messages` |
| `managed-openai` | `chat`, `models`, `embeddings` | true | true | `[]` | The gateway runs the server (see "Managed OpenAI-compatible server"); `/v1/models` lists `served_models`; `free_vram` stops the process |

Upstream credentials: `llama-router` managed and `managed-openai` with `key_env` use their
generated key; external routers,
`openai` backends and remote backends use `api_key_file` or `api_key_env`; Ollama has none
(it has no auth built in; research-interop section 6).

External routers and `openai` backends load their own files, so the gateway cannot verify
them; `hal-gateway status` and `hal doctor` show them as `verified: false` (Decision 22).

### Managed OpenAI-compatible server

`ManagedServer` (`backends/managed.py`) runs any OpenAI-compatible server that takes its
port from `PORT` and an API key from an environment variable: a vLLM launch script, SGLang,
a llama-server wrapper. It shares the launcher, the record format and the crash rules with the
router.

- Files in `<state>/gateway/`: `<name>.json` (0600: `pid`, `pid_start`, `boot_id`,
  `config_sha256`, `address`, `started`), `<name>.key` (0600, generated once) and `<name>.log`.
- `config_sha256` is the sha256 of the argv, cwd, `env`, `port` and `key_env`. A gateway
  restart adopts a live server with the same hash; any other recorded server is stopped.
- Child: `Popen(command, cwd=cwd or dirname(command[0]), env=<REQ-GW-91>,
  start_new_session=True)`, output appended to the log. No shell, no `PATH` lookup.
- Ready: `GET <health_path>` answers 200, then `GET /v1/models` lists one of `served_models`.
  vLLM answers `/health` before it lists its models, so both are checked.
- Stop: the whole process group (vLLM's `EngineCore` is a child of the API server and holds
  the VRAM), `SIGTERM`, `stop_timeout_s`, `SIGKILL`.
- `autostart = "on-demand"` (default) starts it at the first request or at `hal-gateway backend
  start`; `"with-gateway"` at gateway start.
- GPU: with `exclusive_gpu` (default) a start first stops the router and other managed GPU
  servers (`yield_gpu`, no deliberate-stop mark) and unloads Ollama (`unload_ollama`). The router
  and every managed server share one lifecycle lock, so two starts that each stop the other run
  one after the other instead of deadlocking.
- `idle_stop = false` keeps the server through the idle unload; a cold start of a 27B model
  under vLLM took 57 to 138 s on the reference machine (RTX 3090, 2026-11-05).

### Managed llama-server router

```python
@dataclass(frozen=True)
class ModelSection:
    name: str                   # preset section name, e.g. "hal-gpu"
    model: Path
    model_draft: Path | None
    mmproj: Path | None
    chat_template_file: Path | None
    ctx_size: int | None
    load_on_startup: bool

@dataclass(frozen=True)
class ModeInfo:
    name: str
    path: Path                  # source INI
    sections: tuple[ModelSection, ...]
    main: str
    agent: str
    classifier: str | None
    models_max: int
    autoload: bool
    route: bool
    effort: bool
    effort_note: Path
    delegate_note: Path | None
    strip_tools: tuple[str, ...]
    swap: bool
    swap_slots: int
    context_window: int
    client_timeout_ms: int | None

@dataclass(frozen=True)
class Preset:
    """A rendered mode file: the `--models-preset` argument (01 calls it `Preset`)."""
    mode: ModeInfo
    path: Path                  # <state>/gateway/rendered/<mode>.ini
    sha256: str

def find_mode(name: str, dirs: Sequence[Path]) -> Path: ...            # first dir wins
def parse_mode(path: Path) -> ModeInfo: ...
def render_mode(info: ModeInfo, *, models_dir: Path, hal_dir: Path, state_dir: Path,
                out_dir: Path, manifest: Manifest) -> Preset: ...
def llama_server_command(preset: Preset, *, listen: str, api_key_file: Path | None,
                         state: Path) -> tuple[list[str], dict[str, str]]: ...  # 01 REQ-SEC-76

class RouterSupervisor:
    """Starts, adopts, watches and stops one llama-server router process."""
    def start(self, mode: str, deadline: float) -> None: ...
    def stop(self) -> None: ...
    def switch(self, mode: str, *, force: bool, deadline: float) -> None: ...
    def loaded(self) -> dict[str, str]: ...        # preset -> status value
    def load(self, preset: str, deadline: float) -> None: ...
    def unload(self, preset: str, deadline: float) -> None: ...
    def save_slot(self, preset: str, slot: int) -> int: ...      # n_saved
    def restore_slot(self, preset: str, slot: int) -> int: ...   # n_restored
```

`render_mode` checks every model path against the manifest (REQ-GW-75) and every template
against REQ-GW-78 before it writes the file, so a refused mode never reaches llama-server.

#### Mode files

Modes are INI files of llama-server presets plus one Hal section. Hal ships none since 0.2.0:
`hal tune` writes `tuned` (06), and a user may write others. Discovery looks in
`router.modes_dirs` (default `["~/.config/hal/modes"]`); the bundled `halgateway/modes/` keeps
only the files a mode may reference (the chat template, the policy prompts, `SHA256SUMS`). A mode is
what 06 and 00 call a preset in `hal serve [PRESET]`; inside a mode, each INI section is a
llama-server preset.

The `[hal]` section (removed before llama-server sees the file):

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `description` | string | "" | One line for `hal-gateway modes` |
| `main` | preset name | required | Target of `hal/main` |
| `agent` | preset name | `main` | Target of `hal/agent` (subagents, side requests) |
| `classifier` | preset name | none | Decision model for `route` |
| `models-max` | int | 3 | `--models-max` (hal-serve default) |
| `autoload` | bool | true | false adds `--no-models-autoload` |
| `route` | bool | false | Route policy on `hal/main` (needs `agent`, `classifier`) |
| `effort` | bool | false | Effort policy on `hal/main` and `hal/agent` |
| `effort-note` | file | `@MODES@/effort-xhigh.txt` | Text of the xhigh note |
| `delegate-note` | file | none | Delegate policy on strong-model requests when set |
| `strip-tools` | comma list | `Bash,Edit,MultiEdit,Write,NotebookEdit` | Tools the delegate policy removes |
| `swap` | bool | false | Swap scheduler between `main` and `agent` |
| `swap-slots` | int | 2 | KV slots of `main` saved across a swap (hal-route `--slots`) |
| `context-window` | int | computed | See below |
| `client-timeout-ms` | int | none | Hint for `hal connect` (`API_TIMEOUT_MS`) |

`context-window` default: the smallest `ctx-size` among presets, ignoring the classifier,
and in swap modes ignoring `agent` (subagents start from a fresh context). This ports the awk
rule of `hal:159-168`. A preset without `ctx-size` takes it from `[*]`; with neither the
mode is a config error, because Claude Code would assume 200K (MODES.md).

Port of the existing modes (`hal:127-139`, `hal-serve:142-145`, `hal-serve:180-191`):

| Mode | main | agent | classifier | Other `[hal]` keys |
| --- | --- | --- | --- | --- |
| `gpu`, `gpu-112k` | `hal-gpu` | `hal-gpu` | | |
| `cpu` | `hal-cpu` | `hal-cpu` | | |
| `hybrid`, `hybrid-256k` | `hal-hybrid` | `hal-hybrid` | | |
| `hybrid-auto`, `hybrid-auto-256k` | `hal-hybrid` | `hal-hybrid` | | `effort = true` |
| `combined` | `hal-orchestrator` | `hal-agent` | `hal-router` | `route = true`, `delegate-note = @MODES@/combined-prompt.txt` |
| `orchestrate`, `orchestrate-256k` | `hal-hybrid` | `hal-agent` | | `swap = true`, `effort = true`, `delegate-note = @MODES@/orchestrate-prompt.txt`, `models-max = 1`, `autoload = false`, `client-timeout-ms = 7200000` |

Tokens:

| Token | Value |
| --- | --- |
| `@MODELS@` | `router.models_dir` (was `HAL_MODELS_DIR`) |
| `@HAL@` | the parent of the bundled `modes/` directory, so `@HAL@/modes/qwen3.8-chat.jinja` keeps working in user files |
| `@MODES@` | the directory of the INI being rendered |
| `@STATE@` | `<state>/gateway` (slots go to `@STATE@/slots`) |

Rendering is a plain token substitution on the text, then an INI parse with
`configparser` (`interpolation=None`, `strict=True`, keys kept case-sensitive) for the checks,
then the text minus the `[hal]` section is written. Comments are kept.

#### Denied preset keys

Rendering fails if any section other than `[hal]` sets one of these keys (with or without
leading dashes; long or short alias): `host`, `port`, `api-key`, `api-key-file`,
`ssl-key-file`, `ssl-cert-file`, `api-prefix`, `path`, `cors-origins`, `cors-methods`,
`cors-headers`, `cors-credentials`, `tools`, `tools-runtime`, `agent`, `mcp-servers-config`,
`mcp-servers-json`, `ui-mcp-proxy`, `webui-mcp-proxy`, `ui`, `webui`, `props`, `media-path`,
`log-prompts-dir`, `rpc`, `models-dir`, `models-preset`, `models-max`, `models-autoload`,
`hf-repo`, `hf-file`, `hf-repo-draft`, `hf-token`, `model-url`, `docker-repo`,
`embd-gemma-default`, `fim-qwen-1.5b-default` and every other `*-default` key that downloads
weights. Reason: the first group would bypass the gateway or widen the listener, the tool and
MCP keys let the server act on model output (marked unsafe in `--help`), and the download
keys fetch unverified weights. `models-*` belong to the `[hal]` section. This list covers
every flag 01-security REQ-SEC-76 forbids.

#### Process

Command line (TCP listen), as `llama_server_command` builds it:

```text
<server> --host 127.0.0.1 --port <port> --models-preset <state>/gateway/rendered/<mode>.ini
         --models-max <models-max> [--no-models-autoload]
         --api-key-file <state>/gateway/router-api-key --no-webui
```

With `listen = "unix"`, `--host <state>/gateway/run/llama.sock` replaces host and port;
the path is checked with `check_socket_path`.

- Environment: exactly the allowlist of 01-security ("Managed llama-server"): `PATH`,
  `HOME`, `LANG`, `LC_ALL`, `CUDA_VISIBLE_DEVICES`, `LD_LIBRARY_PATH` (only the llama.cpp
  directory), and the `LLAMA_ARG_*` keys the function sets itself. `LLAMA_ARG_HOST` and
  friends from the user's environment would set server options (`--help` lists them).
- `subprocess.Popen(..., start_new_session=True, stdin=DEVNULL, stdout=log, stderr=log)`.
- `router.json` is written atomically after the spawn with `pid`, `pid_start` (field 22 of
  `/proc/<pid>/stat`), `boot_id`, `mode`, `render_sha256`, `address`, `started` (UTC `Z`).
- Startup wait: poll `GET /models` every 1 s; per startup preset (presets without
  `load-on-startup = false`): `loaded` ends the wait for it; `failed`, or `unloaded` after
  `loading` was seen, stops the router and fails; process exit fails; `load_timeout_s` fails.
  A `/models` that does not answer yet counts as `starting`.
- `autostart = "on-demand"` (default) starts the router at the first request that needs it
  or at `hal-gateway mode`; `"with-gateway"` starts it at gateway start.
- The gateway never sends `/models/load` in autoload modes; the router loads on first use.
  In `autoload = false` modes only the swap policy and admin `load` call `/models/load`.

#### Docker launcher (optional, REQ-GW-71)

```text
docker run --rm --name hal-llama-<instance> --runtime=<container_runtime> --gpus all
  --network none --read-only
  --cap-drop ALL --security-opt no-new-privileges --user <uid>:<gid> --pids-limit 256
  --tmpfs /tmp:rw,nosuid,nodev,size=64m
  -v <models_dir>:/models:ro -v <bundled modes dir>:/hal/modes:ro
  -v <state>/gateway/run:/run/llama:rw -v <state>/gateway/slots:/state/slots:rw
  -v <state>/gateway/router-api-key:/run/secrets/key:ro
  <image> --host /run/llama/llama.sock --models-preset /run/llama/<mode>.ini ...
```

The preset is rendered with container paths (`@MODELS@=/models`, `@HAL@=/hal`,
`@STATE@=/state`). `<image>` comes from `router.container_image`; there is no default image in
this spec (02-sandbox owns image builds). `router.container_runtime` is `nvidia` by default
(resolution D12; the NVIDIA Container Toolkit 1.20.1 is installed and `--runtime=nvidia
--gpus all` sees the RTX 3090), `runc` for CPU-only modes (`--gpus` is then left out), or
`runsc-gpu`, which fails on the development host ("cannot read client sync file: EOF", host check). Under runsc the host must connect to a socket the container creates in a
bind mount, which gVisor allows only with a `--host-uds` setting that includes `create`
[unverified: gVisor flag documentation; the sandbox's `--host-uds=open` covers only sockets
the host creates]. The model directory is mounted read-only, so a compromised server cannot
alter weights the manifest pins.

### Router policies

```python
@dataclass
class RequestContext:
    id: str
    api: Api
    fmt: Literal["anthropic", "openai", "ollama"]
    path: str
    principal: Principal
    target: Target
    body: dict[str, object]
    decisions: dict[str, object]          # goes to the log record "policy" field

@dataclass(frozen=True)
class Outcome:
    status: int
    stop_reason: str | None               # last stop_reason / finish_reason seen
    error: str | None

class Policy(Protocol):
    name: str
    def rewrite(self, ctx: RequestContext) -> None: ...
    def admit(self, ctx: RequestContext, deadline: float) -> object | None: ...   # ticket
    def finish(self, ctx: RequestContext, ticket: object | None, outcome: Outcome) -> None: ...
```

Policies apply only to `messages` and `chat` APIs. Pipeline order for one target:
route, delegate, effort, then swap admission. Each policy catches its own exceptions in
`rewrite` (fail-open, logged as `"<policy>": "error: <type>"`), except swap admission, which
returns 503 on failure.

Shared helpers (ported from `hal-route.py:348-443`, behaviour kept, with an OpenAI variant):

```python
def conversation_key(messages: list[dict]) -> str: ...      # sha256 of canonical first message
def user_text(message: dict, fmt: str) -> str: ...           # "" for tool results, reminders stripped
def decision_point(messages: list[dict], fmt: str, shell_tools: frozenset[str],
                   agent_tools: frozenset[str]) -> str: ...  # "" or a reason
def append_system(body: dict, fmt: str, note: str) -> None: ...
```

OpenAI-format mapping: a `tool` role message is a tool result; its tool name comes from the
matching `tool_calls[].id` in the previous assistant message; `is_error` does not exist, so
only the `FAILURE` and `EXIT_1` regexes apply. `shell_tools` defaults to
`{"Bash", "run", "bash", "shell"}` and `agent_tools` to `{"Agent", "Task"}`; both are
per-alias config keys.

#### Effort policy

Ported from `EffortRouter` and `with_effort` (`hal-route.py:154-160, 377-419`). On a request
with tools, if `decision_point` returns a reason, append the note as a final system message:
Anthropic `{"role": "system", "content": note}` appended to `messages` (as today; Claude Code
already sends mid-conversation system messages and `modes/qwen3.8-chat.jinja` accepts them),
OpenAI the same shape. Decision recorded as `"effort": "xhigh (<reason>)"` or `"medium"`.
The note is model-template specific; the bundled `effort-xhigh.txt` holds the wording of
`modes/qwen3.8-chat.jinja` (`hal-route.py:46-48`).

#### Route policy

Ported from `Router` (`hal-route.py:93-151`). Applies when the resolved upstream model is the
alias's strong model (`main`).

1. No tools: rewrite to the fast model (side request). Decision `"route": "side -> fast"`.
2. The last non-system message has user text: classify, store the choice under the
   conversation key (LRU, 1024 entries), decision `"route": "<choice> p_strong=<p>"`.
3. Otherwise use the stored choice, or `default` if none.
4. `fast`: rewrite `model` to the fast model; `strong`: apply the delegate policy.

Classifier call (upstream of the classifier preset's backend, with that backend's auth,
timeout 30 s):

```json
POST /v1/systemone
{"model": "hal-router",
 "state": "<last 4000 characters of the user text>",
 "questions": {"route": {"type": "choice",
   "instructions": "A coding assistant must handle this request. Pick the model that should do it.",
   "criteria": {"fast": "A small fast model is enough: ...",
                "strong": "Needs a large careful model: ..."}}}}
```

```json
{"answers": {"route": {"choice": "fast", "probabilities": {"fast": 0.91, "strong": 0.09}}}}
```

The criteria text is `ROUTE_QUESTION` from `hal-route.py:53-64`, unchanged. The request and
response shapes are those `hal-route.py` used in production [verified: 5 classifier turns
in `route.log`; the route exists in b11512 per `strings`]; the full llama.cpp documentation
of `/v1/systemone` was not re-read [unverified beyond those two checks]. `force` (fast or
strong) skips the classifier.

#### Delegate policy

Ported from `orchestrate()` (`hal-route.py:354-359`): remove tools whose name (Anthropic
`name`, OpenAI `function.name`) is in `strip_tools`, and append the delegate note to the
system prompt (Anthropic `system` string or block list; OpenAI: append to the first system
message, or insert one at index 0). Used by route on strong turns and by swap on main-model
requests with tools.

#### Swap policy

Ported from `SwapRouter` (`hal-route.py:167-345`) as a scheduler owned by the router
backend, shared by every alias of that backend. Constants keep their values and reasons:
`quiet_s = 2.0`, `agent_stale_s = 900`, `big_chars = 50_000`, `load_timeout_s = 900`,
`swap_slots = 2`. New: `swap_wait_s = 7200` (equals the two-hour client timeout `hal` sets
for these modes, `hal:148`).

- `owner(ctx)`: `main` or `agent` for requests to either with tools or with messages over
  `big_chars` characters of JSON; `None` (either will do) otherwise.
- `admit`: as `begin()` today, with a deadline. A `None`-owner request runs on the active
  model; if no model is loaded (after an idle unload or a reclaim), the scheduler loads
  `main` first.
- Switch: if leaving `main`, save slots `0..swap_slots-1`
  (`POST /slots/<i>?action=save {"filename": "<main>-<i>.bin"}`, keep those with `n_saved`);
  `POST /models/unload {"model": leaving}`; wait `unloaded`; `POST /models/load
  {"model": target}`; wait `loaded`; if arriving at `main`, restore the saved slots
  (`?action=restore`, sum `n_restored`). Decision `"swap": "<from> -> <to> in <s>s, restored <n> tokens"`.
- `finish`: as `end()` today; a fast-model conversation ends when `stop_reason` is
  `end_turn`, `stop_sequence` or (OpenAI) `stop`.
- The scheduler holds the claim of the class that triggered the swap for the whole swap; a swap counts
  as activity for the idle timers, and a reclaim (REQ-GW-55) is answered `busy` while a swap
  runs, so the scheduler's view of what is loaded stays right (the reason for the `command`
  reclaimer, resolution D7).

Stop reasons are found by scanning the response with the regexes
`"stop_reason"\s*:\s*"(\w+)"` and `"finish_reason"\s*:\s*"(\w+)"` over each chunk plus the
previous chunk's last 64 bytes, as `hal-route.py:519-525` does.

#### Overflow mapping

```python
def map_overflow(fmt: str, status: int, body: bytes) -> bytes | None: ...
```

Applies to `messages` and `chat` routes on llama-router backends. On status 400 the relay
reads the body (cap 1 MiB) before sending headers, and if `error.type ==
"exceed_context_size_error"`:

- Anthropic: `{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 120000 tokens > 114688 maximum"}}`
  (the text Claude Code compacts on; `hal-route.py:446-456`).
- OpenAI: `{"error":{"message":"This model's maximum context length is 114688 tokens. However, your messages resulted in 120000 tokens.","type":"invalid_request_error","param":"messages","code":"context_length_exceeded"}}`.
  Which OpenAI-format clients act on this code was not checked [unverified; check by
  overflowing a Codex and an opencode session against a small `ctx-size`].

The response `Content-Length` is recomputed.

### GPU coordination

```python
@dataclass(frozen=True)
class AcquireResult:
    held: bool
    waited_s: float
    warning: str

class GpuCoordinator(Protocol):
    kind: Literal["gpu-light-v2", "gpu-light-v1", "none"]
    def acquire(self, name: str, *, cls: Literal["interactive", "service"], wait_s: float,
                vram_mib: int | None, uses: Sequence[str], reason: str) -> AcquireResult: ...
    def release(self, name: str) -> None: ...
    def status(self) -> dict[str, object]: ...

@dataclass(frozen=True)
class LeaseInfo:
    session_id: str
    label: str
    since: str                  # UTC Z

@dataclass(frozen=True)
class ReleaseResult:
    released: bool              # false when kept by leases or busy
    unloaded: tuple[str, ...]
    router_stopped: bool
    kept: tuple[LeaseInfo, ...] # leases that blocked a release without force
    busy: int                   # in-flight requests that blocked a reclaim

class GpuManager:
    """Takes one claim per class, releases it when idle, and answers release requests."""
    def gate(self, target: Target, principal: Principal) -> None: ...  # off flag, peers, claim
    def started(self, target: Target) -> None: ...     # in-flight += 1
    def ended(self, target: Target) -> None: ...
    def leases(self) -> list[LeaseInfo]: ...
    def release(self, *, force: bool, stop_router: bool, reclaim: bool) -> ReleaseResult: ...
    def tick(self, now: float) -> None: ...            # called every 10 s by a daemon thread
```

A **lease** is a session minted with `lease = true` (REQ-GW-60). It exists from mint to
revoke or expiry. Leases do two things only: requests from a leased session pass the off flag
(resolution D9), and `release` without `force` keeps the GPU state while any lease exists
(06 REQ-CLI-50, `hal off` keeps an open coding session's server). A reclaim from gpu-light
ignores leases: an idle leased session gets its model reloaded on the next request, which is
gpu-light's anti-thrash trade (GL `idle_after_s`).

The claim **class** comes from the principal: a session's `class` (default `interactive`;
06 mints `service` sessions for headless `hal -p` and long jobs), a client token's
`class` (default `interactive`), the owner on TCP `owner_tcp_class` (default `interactive`:
host harnesses through `hal connect`), the owner on the unix socket `owner_unix_class`
(default `service`: host packs through `halkit.llm`, such as the briefing). This is GL's
recipe ("Hal gateway"): a long headless run does not keep batch work paused while nobody is
watching.

#### gpu-light v2 (JSON-RPC socket)

The gateway follows GL "Socket API" exactly; `gpu/lightv2.py` is a stdlib client.

- Connection: one long-lived connection to the GL socket, opened at gateway start (or at
  the first GPU request when the daemon was down), with `hello` first:
  `{"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"client": "hal-gateway",
  "version": "<halgateway version>", "protocol": 2}}`. A reader thread routes answers by `id`
  and handles `claim_granted` notifications. Every call has a 2 s timeout except the grant
  wait. Lost connection: claims end with it (GL REQ-API-04); the gateway reconnects with
  backoff (1 s doubling to 30 s), re-sends `hello`, re-registers the reclaimer
  (REQ-GW-59) and re-claims each class that has requests in flight.
- Claims per class (GL "Hal gateway", step 2): the gateway holds at most two connection
  claims, `hal-gateway-interactive` (class `interactive`) while a request from a session where
  a person waits is in flight, and `hal-gateway-service` (class `service`) while only headless
  work is in flight, both with `uses = ["hal-gateway"]` and a `reason` naming the alias. The
  request's class comes from its principal ("Principals and tokens" above). Both names are
  `<reclaimer_name>-interactive` and `<reclaimer_name>-service`, so the defaults are GL's
  names and a second install with another `reclaimer_name` does not collide (REQ-GW-58).
  Requests to every GPU backend of one class share that class's claim.

  ```json
  {"jsonrpc": "2.0", "id": 7, "method": "claim",
   "params": {"name": "hal-gateway-interactive", "class": "interactive", "gpu": 0,
              "vram_mib": 22000, "exclusive": true, "uses": ["hal-gateway"],
              "reason": "hal gateway: hal-code for session 3f9a1c2e", "lifetime": "connection"}}
  {"jsonrpc": "2.0", "id": 7, "result": {"claim": {"name": "hal-gateway-interactive", "granted": false, ...}}}
  {"jsonrpc": "2.0", "method": "claim_granted", "params": {"name": "hal-gateway-interactive"}}
  ```

  `vram_mib` comes from `[gateway.gpu.vram_mib]` keyed by the upstream model or preset name of
  the request that takes the claim, else it is left out (whole card); GL credits the VRAM the
  gateway's servers already hold (GL REQ-VRAM-10). `uses` is the union of the in-flight
  backends' `gpu_light_uses`, which is `["hal-gateway"]` with the defaults. The `reason` names the alias and
  the principal (`session <id>`, `client <name>`, `owner`), which is how a remote client's
  use shows up in `gpu-light ls` (07). When GL answers -32001 for a name in `uses` (no such
  reclaimer), the gateway retries without `uses` and logs one warning per backend.
- Release: `{"method": "claim.release", "params": {"name": ...}}` `idle_release_s` after the
  target's last request ends (REQ-GW-53), at `release`, and at gateway stop.
- Reclaimer: at start the gateway calls `reclaimers.list` and makes sure this entry exists
  (REQ-GW-59), adding it with `reclaimers.add`:

  ```json
  {"name": "hal-gateway", "type": "command",
   "match_unit": ["hal-gateway.service", "ollama.service"], "match_exe": ["llama-server"],
   "class": "service", "idle_after_s": 120, "reclaim_for": ["interactive", "batch"],
   "timeout_s": 60,
   "reclaim": ["/home/me/.local/share/hal/venv/bin/hal-gateway", "release-gpu", "--reclaim"]}
  ```

  The argv uses the absolute path of the running console script (`sys.argv[0]` resolved), so
  it does not depend on `PATH`. `match_unit` lets gpu-light attribute the managed router's
  VRAM to this service: `KillMode=control-group` keeps llama-server in the gateway unit's
  cgroup. Activity comes from `uses` in the gateway's claims (GL REQ-RCL-07), so no `status`
  argv is given. `ollama.service` and `match_exe = ["llama-server"]` are in the match lists
  because this one reclaimer frees every server the gateway drives, the Docker-run
  llama-server included; a Hal user configures no separate `ollama` or `llama-router`
  reclaimer for them (GL "Hal gateway", step 4). `timeout_s = 60` covers unloading two presets. `idle_after_s = 120` equals
  `idle_release_s`. This replaces GL's recipe item 4 (a `llama-router` reclaimer calling the
  router directly), because a direct unload would bypass the swap scheduler's bookkeeping
  (resolution D7). `hal-gateway release-gpu --reclaim` posts `/hal/v1/gpu/release` with
  `{"reclaim": true}` and exits 0 when released, 3 when busy.

#### gpu-light v1 (file claims)

With only gpu-light v1, today's behaviour stays (resolution D8): one gateway-wide lease. The
gateway writes the claim itself (no `gpu-light` binary needed), per
`gpu-light/docs/protocol.md`:

```json
{"protocol": 1, "name": "hal-gateway", "lifetime": "pid", "pid": 41234, "pid_start": 5551234,
 "boot_id": "0b9c6a4e-...", "created": "2026-10-23T18:30:00Z", "expires": null,
 "reason": "hal gateway: hal/main on local", "command": null}
```

- Directory: `$GPU_LIGHT_DIR`, else `$XDG_STATE_HOME/gpu-light`, else
  `~/.local/state/gpu-light`; must be owned by the user with no group or other bits.
- Write `.<name>.json.<pid>.tmp`, `fsync`, `rename`, `fsync` the directory.
- Wait: read `light.json` every 1 s until its effective state is fresh and `background` is
  empty, or `v1_claim_wait_s`. A missing or stale `light.json` (no watcher) ends the wait at
  once: with no watcher there is nobody to report background jobs, and v1 clients in degraded
  mode release on seeing the claim.
- Release: unlink the file. A crash voids the claim because its process is gone.
- Claim name: `gpu.v1_claim_name` (default `hal-gateway`), checked against
  `^[a-z0-9][a-z0-9._-]{0,63}$`.

#### Idle release, off flag, release requests

- `tick` (every 10 s): with v2, release each claim whose target has `in_flight == 0` and
  `now - last_activity >= idle_release_s`, keeping models loaded. With v1 or none, when
  `in_flight == 0` on every GPU backend and `now - last_activity >= idle_unload_s`, call
  `free_vram()` on every GPU backend this gateway used since the lease was taken, then
  release the lease. The managed router process keeps running (it holds no VRAM with no
  model loaded); the next request reloads.
- The off flag check is a `stat` per GPU request (cheap, no caching, so `hal on` takes
  effect at once), skipped for leased sessions.
- `POST /hal/v1/gpu/release` body `{"force": false, "stop_router": false, "reclaim": false}`
  answers 200 with the `ReleaseResult` as JSON. `hal off` (06) sends `stop_router = true`;
  gpu-light's reclaimer sends `reclaim = true`. With `force`, in-flight requests to unloaded
  models fail with 502 and their sessions see an error turn.

#### Peer guard

Ported and generalised from an earlier private fork (`HAL_PEER_LLAMA_URL`): `[[gateway.gpu.peers]]` with
`url` and optional `api_key_file`. The check runs `GET <url>/models`, parses JSON, and counts
a peer as holding the GPU when any entry has `status.value == "loaded"` (the fork used a
`grep` on the body). It runs before router start, before `load`, and before each claim.

#### Helpers for halcore

halcore holds no `hal on` claim with any gpu-light version (06 REQ-CLI-51). It would use these instead of `halcore/gpulight.py`:

```python
def detect(timeout_s: float = 2.0) -> Literal["gpu-light-v2", "gpu-light-v1", "none"]: ...
def status_line(timeout_s: float = 2.0) -> str: ...   # "GPU light: green" or "gpu-light: absent"
class FileClaim:                                      # halgateway.gpu.lightv1
    """A gpu-light protocol 1 claim file owned by this process."""
    def __init__(self, name: str, *, reason: str, directory: Path | None = None) -> None: ...
    def acquire(self, wait_s: float) -> AcquireResult: ...
    def release(self) -> None: ...
```

### Sessions and the forwarder

Admin protocol (HTTP over `gateway.sock`):

```text
POST /hal/v1/sessions HTTP/1.1
Host: localhost
Content-Type: application/json
Content-Length: 154

{"label": "hal code ~/src/proj", "aliases": ["hal-*"], "allow_remote": false,
 "max_request_bytes": 8388608, "max_concurrent": 4, "ttl_s": 86400,
 "class": "interactive", "lease": true}
```

```text
HTTP/1.1 201 Created
Content-Type: application/json

{"id": "3f9a1c2e", "token": "hal-sess-3f9a1c2e.Zt0...", "label": "hal code ~/src/proj",
 "aliases": ["hal-*"], "allow_remote": false, "max_request_bytes": 8388608,
 "max_concurrent": 4, "class": "interactive", "lease": true,
 "expires": "2026-10-25T18:30:00Z"}
```

All fields but `label` are optional and default to `[gateway.sessions]`; `aliases` defaults
to `["*"]` (every local alias), `class` to `interactive`, `lease` to false.
`GET /hal/v1/sessions` lists sessions without tokens. `DELETE /hal/v1/sessions/3f9a1c2e`
answers 204. `GET /hal/v1/leases` lists `LeaseInfo` objects.

```python
@dataclass(frozen=True)
class ForwarderLimits:
    max_request_bytes: int = 8 * 2**20
    max_concurrent: int = 4
    read_timeout_s: float = 30.0
    max_requests_per_min: int = 120         # REQ-GW-69
    max_output_tokens: int = 16384          # REQ-GW-69: larger requested caps are clamped
    max_tokens_per_hour: int = 100_000_000  # REQ-GW-69: input plus output, sliding hour

@dataclass(frozen=True)
class ForwarderEvent:
    kind: Literal["token_rejected", "gateway_down", "rejected", "request"]
    detail: dict[str, object]

class SessionForwarder:
    """Serves one sandbox session's model socket and relays to the gateway with its token."""
    def __init__(self, listen_path: Path, gateway_socket: Path | None = None, token: str = "",
                 *, gateway_url: str | None = None,
                 limits: ForwarderLimits = ForwarderLimits(),
                 on_event: Callable[[ForwarderEvent], None] | None = None) -> None: ...
                 # exactly one of gateway_socket and gateway_url; token must be non-empty
    def start(self) -> None: ...        # bind (0600), serve in a daemon thread
    def set_token(self, token: str) -> None: ...
    def close(self, timeout_s: float = 5.0) -> None: ...   # stop, close upstreams, unlink
```

02's call `SessionForwarder(listen_path=..., gateway_socket=..., token=...)` is the local
form. In client mode halcore passes `gateway_url=[gateway].url` and the client token from
`[gateway].token_file` instead, because a laptop cannot mint sessions (admin routes are
unix-only on the desktop); the client token's scope then applies at the desktop gateway, and
the forwarder's own route allowlist still applies on the laptop (REQ-GW-63, REQ-GW-65).

The forwarder passes exactly the routes of 01-security REQ-SEC-66 and answers 403 to anything
else without contacting the gateway; it matches on the path without the query string (Claude
Code calls `/v1/messages?beta=true`) and forwards the query string unchanged. The gateway
enforces the session scope again (REQ-GW-25, REQ-GW-61); the forwarder is a cheap first
filter, the gateway is authoritative. The model budget (REQ-GW-69) is the exception: the
forwarder alone enforces it, because it is the one host process that sees every request a
session makes. halcore builds `ForwarderLimits` from `[gateway.sessions]`.

02-sandbox owns where the session directory lives, the bind mount to `/run/hal`, and the
in-container relay that serves the socket on `127.0.0.1:8741` inside the box.

### Client tokens and client mode

```python
@dataclass(frozen=True)
class ClientToken:
    name: str
    created: str                 # UTC Z
    allow_remote: bool
    cls: Literal["interactive", "service"]

class ClientTokens:
    """Named client tokens in <state>/gateway/tokens.json; re-read when its mtime changes."""
    def issue(self, name: str, *, allow_remote: bool = False,
              cls: str = "interactive") -> str: ...      # returns the token once
    def revoke(self, name: str) -> bool: ...
    def list(self) -> list[ClientToken]: ...
    def check(self, token: str) -> ClientToken | None: ...
```

- `hal-gateway token issue --name NAME [--allow-remote] [--class C] [--json]` prints
  `{"name": "laptop", "token": "hal-gwc-laptop.Zt0..."}` with `--json` (07 REQ-RMT-10 reads
  it); issuing an existing name is an error (exit 3) unless `--rotate`.
- `hal-gateway token revoke --name NAME` and `token list [--json]` (names, `created`,
  `allow_remote`, `class`).
- The CLI edits `tokens.json` itself under `fcntl.flock` (5 s timeout) with an atomic
  replace, so it works while the gateway is down; the gateway `stat`s the file per
  authenticated request and re-reads it on a changed mtime or inode, so a revoke takes effect
  on the next request (REQ-GW-66).
- `hal-gateway token show | rotate` keep handling the owner token; they work on the file
  directly, and the gateway re-reads the owner token file on a changed mtime the same way, so
  a rotation needs no restart. resolution D6 writes these
  commands as `hal gateway token ...`; 06's command tree maps `hal gateway <args>` to
  `hal-gateway <args>`.

Client mode: a machine whose `[gateway].url` is set has no gateway of its own. `serve`
refuses (REQ-GW-68), `halkit.llm` (00) and the forwarder use `url` with the token in
`[gateway].token_file`, and `GatewayClient` admin calls raise `GatewayError` naming client
mode. `url` must be `http://127.0.0.1:<port>` or `http://[::1]:<port>` (the tunnel end,
07-remote).

### GatewayClient (halcore only)

```python
class GatewayClient:
    """Calls the admin API over the gateway's unix socket."""
    def __init__(self, socket_path: Path | None = None, timeout_s: float = 10.0) -> None: ...
    def health(self) -> bool: ...
    def status(self) -> dict[str, object]: ...
    def aliases(self) -> dict[str, object]: ...
    def models(self) -> list[dict[str, object]]: ...          # GET /v1/models as the owner
    def set_mode(self, mode: str, *, wait_s: float = 900.0, force: bool = False) -> dict[str, object]: ...
    def stop_router(self) -> None: ...
    def load(self, model: str, wait_s: float = 900.0) -> None: ...
    def unload(self, model: str) -> None: ...
    def release_gpu(self, force: bool = False, *, stop_router: bool = False,
                    reclaim: bool = False) -> ReleaseResult: ...
    def leases(self) -> list[LeaseInfo]: ...
    def create_session(self, label: str, *, aliases: Sequence[str] = ("*",),
                       allow_remote: bool = False, max_request_bytes: int | None = None,
                       max_concurrent: int | None = None, ttl_s: int | None = None,
                       cls: Literal["interactive", "service"] = "interactive",
                       lease: bool = False) -> SessionGrant: ...
    def revoke_session(self, session_id: str) -> None: ...
```

`SessionGrant` has `id`, `token`, `expires`. halcore is the only importer (00 layering;
resolution A5); halsandbox gets the forwarder through 02's `start_model` callable. 06's
calls map as: `llama_start(preset, wait_s)` -> `set_mode`, `llama_stop()` -> `stop_router`,
`llama_status()` -> `status()["backends"]`, `release_gpu(force)` and `leases()` as named.

### Model manifest

`~/.config/hal/models.toml` (`config_dir()/models.toml`) is the gateway's model manifest
(resolution D2). It is user config: `serve` only reads it; `halgateway.models` writes it
(atomic replace, 0600) when the user pulls or adds a model. Agent profiles are a different
file (`model-profiles.toml`, 03).

```toml
schema = 1

[[model]]
name = "qwen3.6-35b-a3b-q4"                    # unique id for `hal models pull NAME`
file = "qwen/Qwen3.6-35B-A3B-Q4_K_M.gguf"      # relative to router.models_dir, or absolute
format = "gguf"                                # "gguf" | "safetensors"
sha256 = "<64 hex>"
size = 21474836480                             # bytes: pull size and a cheap first check
source = "hf:Qwen/Qwen3.6-35B-A3B-GGUF@<40-hex commit>"   # or "local"
hf_file = "Qwen3.6-35B-A3B-Q4_K_M.gguf"        # path inside the repo; hf sources only
template_sha256 = ""                           # needed when a preset gives this model a chat-template-file outside the bundled modes
profile = "qwen3"                              # optional: meta.hal_profile in /v1/models

[[model]]
name = "gemma4:12b-it-qat"
source = "ollama:gemma4:12b-it-qat@sha256:<64 hex>"       # digest from /api/tags
profile = "gemma4"
```

Rules: unknown keys and wrong types are errors naming them; `name` is unique; `file`,
`format`, `sha256` and `size` are required for `hf:` and `local` sources and forbidden for
`ollama:` ones; a `file` with a pickle extension or another `format` is refused
(REQ-GW-75). These are the security fields 01-security lists ("Supply chain"); the
verification state (`verified_at`, size, mtime_ns, inode) lives in
`<state>/gateway/verified.json`, not in the user's file, so `serve` never writes config.

```python
@dataclass(frozen=True)
class ModelEntry:
    name: str; source: str; file: Path | None; format: str | None
    sha256: str | None; size: int | None; profile: str | None

@dataclass(frozen=True)
class VerifyResult:
    name: str; ok: bool; detail: str          # "ok", "missing", "sha256 mismatch", ...

def list_entries() -> list[ModelEntry]: ...
def plan(names: Sequence[str]) -> list[ModelEntry]: ...     # what pull would fetch, with sizes
def pull(names: Sequence[str], *, progress: Callable[[str, int, int], None]) -> None: ...
def verify(names: Sequence[str], *, full: bool) -> list[VerifyResult]: ...
def add_local(path: Path, *, name: str | None = None, profile: str | None = None) -> ModelEntry: ...
def record_ollama(names: Sequence[str]) -> list[ModelEntry]: ...   # pins current digests
```

- `pull` downloads `hf:` entries from `https://huggingface.co/<repo>/resolve/<commit>/
  <hf_file>` (REQ-GW-79; timeout per read 60 s, the connect 10 s), and pulls `ollama:` entries
  through Ollama's own `/api/pull` on the configured Ollama backend, then checks that the
  digest matches the pinned one (a moved tag fails). The size confirmation and `--yes` belong
  to `hal models pull` (06 REQ-CLI-48), which calls `plan` first.
- `add_local` hashes an existing file and records it with `source = "local"`;
  `record_ollama` reads `/api/tags` and pins the current digests. 06's `hal models` shows the
  entries and asks before either writes, because pinning is a statement of trust.
- `hal configure` (06) offers `record_ollama` for the models `[hal]` names, so an existing
  install keeps working after the update (Migration).

### Admin status and aliases

`GET /hal/v1/status`:

```json
{"version": "0.1.0", "started": "2026-10-23T18:00:00Z",
 "listeners": {"unix": "/run/user/1000/hal/gateway.sock", "tcp": null},
 "backends": {
   "local": {"type": "llama-router", "managed": true, "remote": false, "gpu": true,
             "verified": true, "state": "running", "mode": "gpu", "pid": 51234,
             "address": "127.0.0.1:8080", "models": {"hal-gpu": "loaded"}, "restarts": 0},
   "ollama": {"type": "ollama", "remote": false, "gpu": true, "verified": true, "state": "up",
              "served": ["gemma4:12b-it-qat"]}},
 "gpu": {"coordinator": "gpu-light-v2", "claims": [{"name": "hal-gateway-interactive",
          "class": "interactive", "granted": true, "since": "2026-10-23T18:05:12Z",
          "release_at": "2026-10-23T18:07:12Z"}],
         "reclaimer": "hal-gateway", "off": false, "leases": 1, "peers": []},
 "clients": 1, "sessions": 1, "in_flight": 0}
```

`GET /hal/v1/aliases` (what `hal connect` and `hal claude` need to write a harness config,
replacing the env block of `hal:124-178`):

```json
{"aliases": [
  {"name": "hal/main", "backend": "local", "model": "hal-hybrid", "remote": false,
   "context_window": 131072, "policies": ["delegate", "effort", "swap"]},
  {"name": "hal/agent", "backend": "local", "model": "hal-agent", "remote": false,
   "context_window": 114688, "policies": ["effort", "swap"]},
  {"name": "hal-code", "backend": "local", "model": "hal-hybrid", "remote": false,
   "context_window": 131072, "policies": ["delegate", "effort", "swap"], "role": "code"}],
 "roles": {"main": "hal/main", "agent": "hal/agent"},
 "mode": {"name": "orchestrate", "context_window": 131072, "client_timeout_ms": 7200000}}
```

`POST /hal/v1/mode` body `{"mode": "gpu", "wait_s": 900, "force": false}`; answers 200 with
the status when loaded, 409 `router_busy` when draining failed, 4xx/5xx with the log path on
load failure. `POST /hal/v1/models/load` and `/unload` body `{"model": "<preset>"}`.
`POST /hal/v1/backend/start`, `/stop` and `/restart` body `{"name": "<backend>"}` act on a
`managed-openai` backend and answer `{"backends": {"<name>": <status>}}`; an unknown name is 404.

### CLI

```text
hal-gateway serve [--config PATH]            run in the foreground (systemd ExecStart)
hal-gateway status [--json]                  backends, mode, GPU claims, leases, remote warnings
hal-gateway mode [NAME] [--force] [--no-wait]  show or switch the managed router's mode
hal-gateway modes                            list modes with descriptions and sources
hal-gateway models [--json]                  what /v1/models returns to the owner
hal-gateway load MODEL | unload MODEL        load or unload a router preset
hal-gateway router stop                      stop the managed router process
hal-gateway backend start|stop|restart NAME  control a managed-openai server
hal-gateway release-gpu [--force] [--stop-router] [--reclaim]
                                             unload and release the GPU (gpu-light's reclaimer uses --reclaim);
                                             --stop-router also stops managed servers
hal-gateway token show | rotate              print or replace the owner token
hal-gateway token issue --name NAME [--allow-remote] [--class C] [--rotate] [--json]
hal-gateway token revoke --name NAME
hal-gateway token list [--json]
hal-gateway render MODE                      print the rendered preset (no side effects)
hal-gateway check-config                     validate [gateway] and models.toml, exit 0 or 2
```

Exit codes: 0 success, 1 runtime error (gateway down, load failed), 2 usage or config error,
3 conflict (`router_busy`, `gpu_busy_peer`, release kept or busy, token name taken). Every
command except `serve`, `render`, `check-config` and `token ...` talks to a
running gateway through `GatewayClient`. 06 owns the `hal` command tree that wraps these
(`hal serve`, `hal stop`, `hal status`, `hal models`, `hal gateway ...`).

### Configuration

All keys live in `~/.config/hal/config.toml` (path kept for hal-employment; `$HAL_CONFIG`
overrides) under `[gateway]`, loaded by `halkit` and validated strictly: an unknown key or a
wrong type is an error naming the key, and the gateway does not start. Role aliases read the
`[hal]` table (00 owns it); the default Ollama URL is `[hal].ollama_url`.

```toml
[gateway]
url = ""                    # client mode: "http://127.0.0.1:8741" on a laptop (07); "" = local gateway
unix_path = ""              # "" = halkit.paths.runtime_dir() + "/gateway.sock"
tcp = false                 # off until `hal connect` or `hal remote authorize` turns it on (resolution D4)
tcp_host = "127.0.0.1"      # "127.0.0.1" or "::1" only
tcp_port = 8741             # brief, "Ports and paths"
token_file = "~/.config/hal/gateway-token"   # owner token; in client mode, the client token
allow_origins = []          # browsers have no business here (REQ-SEC-75)
allow_hosts = []            # extra Host values, e.g. a tailnet name in front of loopback (07)
default_model = ""          # "" = unknown names are 404
models_file = ""            # "" = config_dir()/models.toml
max_request_bytes = 33554432  # 32 MiB: a 256K-token prompt is about 1 MiB of JSON; the rest is images
max_header_bytes = 65536    # http.server's own line limit
max_connections = 64        # one thread per connection
client_timeout_s = 300.0
upstream_connect_timeout_s = 10.0
upstream_read_timeout_s = 3600.0  # llama-server's own --timeout default; a non-streaming cpu-mode reply sends nothing for minutes
drain_s = 30.0              # mode switch wait for in-flight requests

[gateway.log]
requests = true
content = false
max_bytes = 10485760
keep = 3

[gateway.sessions]
max_request_bytes = 8388608   # 8 MiB: 8x a full 256K-token prompt; caps what a rogue box can push
max_concurrent = 4            # an agent plus three subagents
ttl_s = 86400                 # one day; hal re-mints per session anyway
max_requests_per_min = 120    # REQ-GW-69: a fast agent with subagents; stops a tight loop
max_output_tokens = 16384     # REQ-GW-69: clamp on max_tokens and its aliases
max_tokens_per_hour = 100000000 # REQ-GW-69: about 13 full 128K-token turns a minute

[gateway.gpu]
coordinator = "auto"        # "auto" | "gpu-light-v2" | "gpu-light-v1" | "none"
claim_wait_s = 10.0         # GL recipe: a turn waits briefly, then proceeds
on_busy = "proceed"         # "proceed" | "fail"; proceed matches today's hooks
idle_release_s = 120.0      # v2: release the claim 2 min after the last request (GL recipe); models stay loaded
idle_unload_s = 900.0       # v1 and none: unload and release after 15 min; 0 = hold until stop
owner_unix_class = "service"      # host packs through halkit.llm
owner_tcp_class = "interactive"   # host harnesses through hal connect
respect_off_flag = true     # leased sessions are exempt (resolution D9)
peer_timeout_s = 3.0
v2_socket = ""              # "" = $GPU_LIGHT_SOCKET, else $XDG_RUNTIME_DIR/gpu-light/daemon.sock (GL)
reclaimer_name = "hal-gateway"
reclaimer_timeout_s = 60.0  # unloading two presets
v1_claim_name = "hal-gateway"
v1_claim_wait_s = 120.0     # hal-serve LIGHT_WAIT: 30 s release deadline plus a late job
v1_dir = ""                 # "" = gpu-light protocol 1 resolution order

[gateway.gpu.vram_mib]      # upstream model or preset -> MiB for GL admission; absent = not sent
# "hal-gpu" = 22000

# [[gateway.gpu.peers]]
# url = "http://127.0.0.1:8180"
# api_key_file = ""

[gateway.backends.local]
type = "llama-router"
url = ""                    # "" when managed: derived from listen/port
managed = true
server = "~/.local/share/hal/llama.cpp/llama-b11512/llama-server"
models_dir = ""             # required when managed (was HAL_MODELS_DIR)
mode = "gpu"                # was HAL_MODE
modes_dirs = ["~/.config/hal/modes"]
listen = "tcp"              # "tcp" | "unix"
port = 8080                 # brief: explicit port, never 0.0.0.0
autostart = "on-demand"     # "on-demand" | "with-gateway"
load_timeout_s = 900.0      # hal-serve: 50 GB from a cold disk takes minutes
stop_timeout_s = 60.0       # hal-serve stop loop
max_restarts = 3
unload_ollama = ["ollama"]  # Ollama backends to empty before a router load
launcher = "process"        # "process" | "docker"
container_image = ""
container_runtime = "nvidia"  # "nvidia" | "runc" (CPU-only modes) | "runsc-gpu" (fails here today)
gpu = true
expose_models = true
gpu_light_uses = []         # [] = [reclaimer_name]
api_key_file = ""           # only for managed = false

[gateway.backends.ollama]
type = "ollama"
url = ""                    # "" = [hal].ollama_url
gpu = true
expose_models = true
unload_offloaded_on_claim = true
gpu_light_uses = ["hal-gateway"]  # default [reclaimer_name]: the gateway reclaimer frees Ollama too (GL "Hal gateway", step 4)

# [gateway.backends.qwen]
# type = "managed-openai"
# command = ["/opt/qwen/start-vllm.sh"]  # argv, no shell; the first item is an absolute path
# cwd = ""                  # "" = the command's directory
# env = {}                  # extra variables; names that look like secrets are refused
# port = 8000               # passed as PORT, with HOST = "127.0.0.1"
# key_env = ""              # e.g. "VLLM_API_KEY": gets the generated <state>/gateway/<name>.key
# served_models = []        # required: what /v1/models lists, and what readiness waits for
# health_path = "/health"
# startup_timeout_s = 300.0
# stop_timeout_s = 60.0
# autostart = "on-demand"   # "on-demand" | "with-gateway"
# max_restarts = 3
# exclusive_gpu = true      # stop the router and other managed servers, empty Ollama, before a start
# unload_ollama = ["ollama"]
# idle_stop = true          # false: the idle unload leaves it running

# [gateway.backends.openrouter]
# type = "openai"
# url = "https://openrouter.ai/api"
# allow_remote = true
# api_key_file = "~/.config/hal/openrouter-key"
# apis = ["chat", "models"]

# [gateway.aliases.coder]
# backend = "ollama"
# model = "qwen3.5:9b"
# patterns = []
# context_window = 32768
# effort = false
# route = {}                # {fast, classifier, default = "strong", force = ""}
# delegate = {}             # {note_file, strip_tools}
# shell_tools = ["Bash", "run"]
# agent_tools = ["Agent", "Task"]
```

Common backend keys: `type`, `url` (`http://`, `https://` or `unix:/abs/path.sock`),
`allow_remote` (false), `api_key_file`, `api_key_env`, `apis`, `gpu`, `expose_models`,
`gpu_light_uses`, `connect_timeout_s`, `read_timeout_s` (override the gateway values). With
no `[gateway.backends]` table at all, the defaults above apply only for `ollama`; the `local`
router needs `models_dir`, so it is created by `hal configure` (06) when a llama-server and a
models directory exist.

Environment read by the gateway: `HAL_CONFIG`, `HAL_STATE_DIR`, `HAL_RUNTIME_DIR`,
`XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `GPU_LIGHT_DIR`, `GPU_LIGHT_SOCKET`,
and each backend's `api_key_env`. No other variable changes behaviour, so the systemd unit
and a shell run behave the same.

### Request log

One JSON object per line, `ensure_ascii=True`:

```json
{"ts": "2026-10-23T18:31:02Z", "id": "r-5c1e9a0b", "listener": "unix", "principal": "session:3f9a1c2e",
 "method": "POST", "path": "/v1/messages", "alias": "hal/main", "backend": "local",
 "upstream_model": "hal-hybrid", "remote": false, "stream": true, "status": 200,
 "bytes_in": 412233, "bytes_out": 18234, "ttfb_ms": 1834.2, "duration_ms": 41022.7,
 "usage": {"input_tokens": 98123, "output_tokens": 812},
 "policy": {"effort": "xhigh (user message)", "swap": "hal-agent -> hal-hybrid in 47.2s, restored 29011 tokens"},
 "gpu": {"claim": "hal-gateway-interactive", "waited_s": 0.0}, "error": null}
```

`principal` is `owner:unix`, `owner:tcp`, `client:<name>`, `session:<id>` or `anonymous`.
`usage` is best effort: the relay keeps the last 8 KiB of the response and takes the last
value of each of `prompt_tokens`, `completion_tokens`, `input_tokens`, `output_tokens`,
`prompt_eval_count` and `eval_count`, normalised to `input_tokens` and `output_tokens`.
`X-Request-Id` from the client is echoed when it matches `^[A-Za-z0-9._-]{1,64}$`, else a
new `r-<8 hex>` id is used and returned in the header.

This log is operational, not the security audit log (01 owns that, through the broker's
`EventSink`). It records which session or client used which model, which is what an
investigation of a rogue box or a stolen client token needs.

### systemd unit

This spec ships the template as `halgateway/data/hal-gateway.service`; 06 renders it with its
placeholders (resolution F8) and ships no second copy.

```ini
[Unit]
Description=Hal model gateway
PartOf=hal.target
After=default.target

[Service]
ExecStart=@BIN@/hal-gateway serve
Restart=on-failure
RestartSec=2
KillMode=control-group
TimeoutStopSec=90
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=hal.target
```

`@BIN@` is 06's placeholder for the `bin/` directory of the env Hal runs in. The unit is not
marked `X-HalGPU=yes`: `hal off` frees the GPU through `POST /hal/v1/gpu/release` and the off
flag instead of stopping the endpoint every client depends on. `KillMode=control-group` makes
the managed router die with the gateway and keeps it in the cgroup gpu-light's
`match_unit` sees. `TimeoutStopSec=90` covers `stop_timeout_s` plus margin.

## Failure modes

| Layer | Failure | Behaviour | Open or closed |
| --- | --- | --- | --- |
| Config | Invalid TOML, unknown key, wrong type, inline API key, remote backend without `allow_remote` or over `http`, non-loopback `url`, user alias named like a role alias | Refuse to start, error names the key | Closed |
| Config | `[gateway].url` set (client mode) | `serve` refuses, names the key | Closed |
| Listener | Runtime dir missing, wrong owner or mode, path too long, another gateway on the socket, TCP port in use | Refuse to start | Closed |
| Listener | Token file missing at start | Create it (0600) and log its path | n/a |
| Listener | Token file group or world readable | Unix listener starts, TCP listener does not, error logged and shown in status | Closed |
| Auth | Foreign uid on unix socket, bad or missing token on TCP, expired session, revoked client token | 403 or 401 | Closed |
| Auth | Foreign `Host` or `Origin` on TCP | 403 | Closed |
| Auth | Session or client token on the unix socket | That principal's scope, never owner | Closed |
| Limits | Too many connections, body too large, chunked body, slow client | 503, 413, 400 or 411, timeout and close | Closed |
| Resolver | Unknown, ambiguous or out-of-scope model | 404, 409 or 403 | Closed |
| Manifest | Ollama model not pinned, or its tag moved to another digest | 503 `model_unverified` naming the model and `hal models` | Closed |
| Manifest | `models.toml` missing or invalid | Router modes refused; Ollama requests 503; `check-config` names the error | Closed |
| Backend | Upstream refuses connection | 502 `upstream_unavailable` | n/a |
| Backend | No response within read timeout | 504 before headers; close after | n/a |
| Backend | Client disconnects mid-stream | Upstream connection closed (llama.cpp cancels the task on disconnect [unverified for the router's child processes]); `finally` runs swap bookkeeping and logging | n/a |
| Backend | External router or `openai` backend (files not verifiable) | Served; `verified: false` in status and `hal doctor` | Open (documented residual risk) |
| Managed router | Binary missing, models dir missing | 503 `router_down` with the missing path; status shows it | Closed |
| Managed router | Preset fails to load, load timeout | Router stopped, error with `router.log` path | Closed |
| Managed router | Crash while serving | 502 for in-flight requests; restart on next request within budget, then 503 | n/a |
| Managed router | Legacy `hal-serve` router running | Refuse to start own router, say `hal-serve stop` | Closed |
| Mode render | Denied key, slot path outside state, non-GGUF, unlisted file, hash mismatch, unpinned template | Mode refused, router not started | Closed |
| Policy: route | Classifier error or timeout | Configured `default` (strong), logged | Open (quality feature) |
| Policy: effort, delegate | Unexpected message shape | Request unchanged, logged | Open (quality feature; the sandbox, not tool stripping, is the security boundary) |
| Policy: swap | Unload, load or wait fails | 503 for that request, state re-read, next request retries | n/a |
| Policy: swap | Slot save or restore fails | Logged; the prompt is reprocessed | Open |
| Policy: overflow | Body not the llama.cpp overflow error | Passed unchanged | Open |
| GPU | Coordinator absent, socket error, claim file write error | Proceed without a claim, warning in log and status | Open (as today: "the light can delay the router but never stop it", `hal-serve:41-42`) |
| GPU | GL daemon restarts or the connection drops | Claims end (GL REQ-API-04); reconnect with backoff, re-claim in-flight targets, re-register the reclaimer | Open |
| GPU | GL protocol major differs | v2 not used; v1 or none; warning | Open |
| GPU | Claim wait times out | `on_busy = "proceed"`: proceed with warning; `"fail"`: 503 `gpu_busy` | Open by default, configurable |
| GPU | Gateway not run by `hal-gateway.service` (detached `serve`) | `match_unit` does not match, so gpu-light sees llama-server as a foreign process and treats it as exclusive; status warns | Open |
| GPU | Reclaim while a request or swap runs | `release-gpu --reclaim` exits 3 (`busy`); gpu-light retries after `preempt.retry_s` | Closed |
| GPU | Off flag present, no lease | 503 `hal_off` for GPU backends | Closed |
| GPU | Off flag present, leased session | Served (resolution D9) | Open by design |
| GPU | Peer holds the GPU | 409 `gpu_busy_peer` | Closed |
| GPU | Peer unreachable | Treated as not holding, warning | Open (a down peer holds no VRAM; a hung one is the residual risk) |
| GPU | Idle unload fails to unload a model | Logged; lease released anyway, so background jobs may find VRAM still held and wait | Open |
| Sessions | Gateway restarted | Sessions gone; forwarder reports `token_rejected`; owner re-mints | Closed |
| Client tokens | `tokens.json` unreadable or invalid | Client tokens rejected (401), error logged once per minute, owner and sessions unaffected | Closed |
| Forwarder | Gateway down | 502 to the box, `gateway_down` event; keeps listening | n/a |
| Forwarder | Path outside REQ-SEC-66 | 403, gateway not contacted | Closed |
| Logging | Request log write fails | ERROR once per minute; request served | Open (operational log; the broker's audit log is the security record) |
| Remote | Remote backend reached | Warning header and log line on every response | n/a (explicit opt-in) |

## Migration

| Today | Replaced by | Notes |
| --- | --- | --- |
| `hal-serve` (bash, 224 lines) | `halgateway.backends.llama_router.RouterSupervisor`, `halgateway.modes`, `hal-gateway mode/router stop/status` | Same flags, same load wait; adds api key, `--no-webui`, env allowlist, adoption, denied keys, manifest check |
| `hal-serve:86-99` `free_ollama_vram` | `OllamaBackend.free_vram` via `unload_ollama` | Parses JSON instead of building JSON with shell quoting |
| `hal-route.py` (567 lines, port 8079) | `halgateway.policies.*` on the gateway's own port | Port 8079 and `route.pid` go away; decisions move from `route.log` to `requests.jsonl` |
| `hal-route.py` `--policy route/effort/swap`, `--fast`, `--strong`, `--classifier`, `--prompt`, `--slots`, `--default`, `--force` | `[hal]` section keys of each mode; `route.default` and `route.force` on aliases | `hal-serve:180-191` case statement becomes data in the INI |
| `modes/*.ini`, `modes/*.txt`, `modes/qwen3.8-chat.jinja` | `packages/hal-gateway/src/halgateway/modes/` with a `[hal]` section each | `git mv`, then add `[hal]`; `@HAL@` semantics kept; user overrides in `~/.config/hal/modes/` |
| Earlier private-fork model swaps | A user mode file in `~/.config/hal/modes/` | No fork |
| `HAL_STATE_DIR` (earlier fork) | `halkit.paths.state_dir()` honouring `HAL_STATE_DIR`; gateway state under `<state>/gateway/` | Plus `HAL_RUNTIME_DIR` for the socket |
| `HAL_PEER_LLAMA_URL` (earlier fork) | `[[gateway.gpu.peers]]` | JSON parse instead of `grep` |
| `halcore/gpulight.py` (CLI wrapper of `gpu-light`) | `halgateway.gpu` (`detect`, `status_line`, `lightv1.FileClaim`, `lightv2`) | With v2 the gateway claims per use and `hal on` holds no claim (resolution D8); with v1, halcore's `hal on` claim uses `FileClaim` |
| `halcore/power.py:32-74` (Ollama ps/unload) | `halgateway.backends.ollama` helpers | `power.py` imports them; `hal off` also calls `GatewayClient.release_gpu(stop_router=True)` |
| `hal:124-178` max-profile env block | `hal connect claude` / `hal claude` (06) from `GET /hal/v1/aliases` | `ANTHROPIC_BASE_URL` is the gateway root `http://127.0.0.1:8741` (resolution F7); models `hal/main` and `hal/agent`; `CLAUDE_CODE_MAX_CONTEXT_TOKENS` from `context_window`; `API_TIMEOUT_MS` from `client_timeout_ms` |
| `hal:150-151` dummy tokens `ANTHROPIC_AUTH_TOKEN=hal` | The owner token, or a leased session token minted by 06 | Real auth |
| `HAL_CHAT_URL=$LLAMA_URL` for Python tools (`hal:176-178`) | Gateway (through `halkit.llm`) | Direct use of 8080 fails once the router has a key |
| `models/catalog.txt`, `pull-all.sh`, `build-roles.sh` | `models.toml` entries and `halgateway.models.pull` (via `hal models pull`) | Each entry gains a pinned commit and sha256 |
| No manifest today | `hal configure` runs `record_ollama` and `add_local` after showing what it pins | Without it, Ollama requests answer 503 `model_unverified` with the fix in the message |
| `~/.local/state/hal/{router.pid,router.mode,router.log,route.pid,route.log,<mode>.ini,slots/}` | `<state>/gateway/{router.json,router.log,rendered/,slots/,requests.jsonl}` | Old files are left in place and ignored; old slot files are not reused (their names match, but a new render may change the model) |
| `HAL_LLAMA_SERVER`, `HAL_MODELS_DIR`, `HAL_LLAMA_URL`, `HAL_MODE`, `HAL_OLLAMA_URL` in `hal.conf` | `[gateway.backends.local]` `server`, `models_dir`, `port`, `mode`; `[hal].ollama_url` | `halgateway.legacy.import_hal_conf(path) -> dict` parses `KEY=value` lines (never sources the file) and returns a `[gateway]` table; `hal configure` writes it |

Keeping existing users working:

- Preset names (`hal-gpu`, `hal-hybrid`, `hal-agent`, ...) stay valid model names through
  alias step 3, so a Claude Code config that names them keeps working once its base URL points
  at the gateway.
- hal-employment is unaffected: it talks to Ollama directly (`ai.ollama_url`) or to
  `ai.openai_url` (it is not a Hal component, so resolution G5 does not bind it), reads
  `hal.conf` model lines (still written by halcore, not by the gateway), and self-gates on
  `nvidia-smi`. Its `OWN` list does not include `llama-server`, so it waits while the managed
  router holds VRAM, as it does today with `hal-serve`.
- `~/.local/state/hal/off` keeps its meaning and gains one: the gateway honours it for
  principals without a lease.
- `X-HalGPU=yes` stays on the units that have it; the gateway unit does not get it.

Removed: `hal-serve`, `hal-route.py`, `halcore/gpulight.py`, port 8079, `route.log`,
`HAL_ROUTE_URL`, `HAL_GPU_LIGHT` (the gateway no longer runs the `gpu-light` binary), and the
`hal-serve` section of `MODES.md` (the measurements move to `docs/user/modes.md` unchanged).

## Test plan

Layout: `packages/hal-gateway/tests/test_<module>.py`, one per module, pytest, behaviour
sentences as names, markers `slow`, `docker`, `gpu`, `network`, `live` deselected by default
(00-overview's marker list). Nothing touches the real `~/.config`, `~/.local/state`,
`$XDG_RUNTIME_DIR`, the GPU, the running Ollama or a running gpu-light: a `hal_env` fixture
(built on `halkit.testing.TempHalHome`) sets `HAL_CONFIG`, `HAL_STATE_DIR`,
`HAL_RUNTIME_DIR`, `GPU_LIGHT_DIR`, `GPU_LIGHT_SOCKET` and `XDG_*` to a short temp dir (under
60 bytes, for socket paths).

Fakes (in `tests/fakes/`):

| Fake | What it does |
| --- | --- |
| `FakeOpenAIServer` | ThreadingHTTPServer on TCP or a unix socket; serves chat, responses, responses input_tokens, messages, count_tokens, embeddings, models; SSE with scripted events and pauses; scripted status codes and bodies (including `exceed_context_size_error`); records each request's headers, path and query |
| `FakeLlamaRouter` | `FakeOpenAIServer` plus `/models` with scripted status sequences, `/models/load`, `/models/unload`, `/slots/<i>?action=save/restore` returning `n_saved`/`n_restored`, `/v1/systemone` with scripted answers or errors, api-key check |
| `fake-llama-server` script | Executable that parses llama-server's argv, writes argv and env to a JSON file, serves `FakeLlamaRouter` on `--host`/`--port`, and can be told to crash or hang via env |
| `FakeOllama` | `/api/ps`, `/api/generate` (records `keep_alive: 0`), `/api/chat` NDJSON stream, `/api/tags` with scripted digests, `/api/show`, `/api/pull`, `/v1/*`, 404 for `/v1/messages/count_tokens` |
| `FakeGpuLightV2` | Unix socket server speaking GL's JSON-RPC 2.0 framing: `hello` (protocol 2, or a scripted other protocol), `claim` with scripted `granted` and later `claim_granted`, `claim.release`, `reclaimers.list/add/remove` with config- and CLI-sourced entries, scripted errors (-32001, -32004) and disconnects |
| gpu-light v1 dir | Temp dir with `light.json` written by the test (fresh, stale, with background entries) |
| `FakeHFServer` | HTTPS-less stand-in injected through the downloader's opener: serves files at `/<repo>/resolve/<commit>/<path>`, can serve wrong bytes |
| `FakeClock` | `halkit.testing.FakeClock` for idle timers, swap quiet and stale timers, load waits, token expiry |
| `FakePeerCred` | Injected uid lookup for REQ-GW-02 |

Unit tests per module (each REQ names its tests above): `test_config.py`, `test_auth.py`,
`test_sessions.py`, `test_clienttokens.py`, `test_routes.py`, `test_aliases.py`,
`test_relay.py`, `test_errors.py`, `test_estimate.py`, `test_reqlog.py`, `test_modes.py`
(a user mode parses and renders with its `[hal]` wiring; no mode ships with Hal; bundled
templates match `SHA256SUMS`), `test_model_manifest.py`,
`test_model_download.py`, `test_models.py`, `test_llama_args.py`,
`test_policies_text.py` (ported decision-point cases), `test_policies_route.py`,
`test_policies_delegate.py`, `test_policies_effort.py`, `test_policies_swap.py`,
`test_policies_overflow.py`, `test_gpu_manager.py`, `test_gpu_lightv1.py`,
`test_gpu_lightv2.py`, `test_gpu_peers.py`, `test_backends_ollama.py`,
`test_backends_openai.py`, `test_backends_llama_router.py`, `test_launchers.py`,
`test_admin.py`, `test_client.py`, `test_forwarder.py`, `test_cli.py`, `test_legacy.py`.
`test_forwarder.py` reads the route list from 01's REQ-SEC-66 table through one shared
constant, so the forwarder, the session column and ATK-33 cannot drift apart.

Integration (hermetic, default run): a real gateway on temp sockets in front of the fakes:
end-to-end SSE through unix and TCP listeners; forwarder to gateway to fake router;
forwarder to a TCP gateway URL with a client token; managed router start, adoption, crash
restart and stop with `fake-llama-server`; v2 claim, idle release and a reclaim through
`hal-gateway release-gpu --reclaim` against `FakeGpuLightV2`; two instances side by side;
slow-reader backpressure.

Slow (`slow`, skipped without `~/.local/share/hal/llama.cpp/*/llama-server`): start the real
binary in router mode with an empty preset and `CUDA_VISIBLE_DEVICES=` on a unix socket and
on TCP, check 401 without key, `/v1/models` through the gateway, whether
`/v1/responses/input_tokens` exists, the denied-key render failure, and `SIGTERM` stop. This
is the experiment of this spec, made repeatable.

GPU (`gpu`, manual opt-in): load mode `gpu` through the gateway, stream a completion,
overflow a small `ctx-size` and check Claude Code compacts, run an `orchestrate` swap and
check `restored <n> tokens` in the log, idle unload frees VRAM in `nvidia-smi`, and (once
gpu-light v2 runs) a batch job's demand triggers the reclaimer and frees the router's VRAM.

Docker (`docker` + `gpu`): REQ-GW-71 drill with `--runtime=nvidia`.

Benchmark (not a test): `packages/hal-gateway/scripts/bench_relay.py`, re-written from
"Measured proxy overhead", run by hand before a release; its numbers go in
`docs/dev/gateway.md` with the date.

## Implementation plan

Each step is one commit in the Hal monorepo (branch `next`), with its tests and
the matching section of `docs/dev/gateway.md`, and passes `ruff`, the ASCII gate and the
test suite. Subjects follow the brief: `<Verb> <object>`, no article after the verb, at most
60 characters, no body.

1. **`Add hal-gateway package with strict config schema`**: `pyproject.toml`,
   `config.py`, `errors.py`, `__main__.py`, `check-config`; tests for every default and error.
2. **`Add gateway unix and loopback listeners with auth`**: `server.py`, `auth.py`,
   `handler.py` skeleton, `/health`, peer-cred check, owner token file at first start, Host
   and Origin checks (403), connection cap.
3. **`Add streaming relay for OpenAI-compatible backends`**: `routes.py`, `relay.py`,
   `backends/openai.py`, header filter, length checks, timeouts, error bodies, query-string
   matching.
4. **`Add model aliases and resolution order`**: `aliases.py`, role aliases from `[hal]`,
   `/v1/models` in both shapes with `context_length`, pattern and bare-name rules.
5. **`Add Ollama backend with limited native routes`**: `backends/ollama.py`, native route
   allowlist, `/api/tags` synthesis, dedup of `/v1/models`, count_tokens estimate
   (`estimate.py`).
6. **`Add request metadata log with rotation`**: `reqlog.py`, usage scan, request ids,
   content opt-in; `scripts/bench_relay.py`.
7. **`Add model manifest with sha256 and digest checks`**: `models/manifest.py`,
   verification cache, Ollama digest gate (REQ-GW-77), `meta.hal_profile`.
8. **`Add model pull, verify and pinning helpers`**: `models/__init__.py`,
   `models/download.py`, `add_local`, `record_ollama`.
9. **`Move llama.cpp mode presets into hal-gateway`**: `git mv modes/*` into the package
   data directory with no content change (mechanical commit; `hal-serve` and the awk rule in
   `hal` get their `modes/` path updated in the same commit so they keep working).
10. **`Add Hal sections and renderer for mode presets`**: `[hal]` sections in all 10 modes,
    `modes.py`, denied keys, `@MODES@`, `effort-xhigh.txt`, `SHA256SUMS`, manifest and
    template checks at render, `hal-gateway render` and `modes`.
11. **`Port hal-serve as managed llama-server router`**: `backends/llama_router.py`,
    `backends/llama_args.py`, `launchers.py` (process), startup wait, Ollama unload, adoption,
    legacy guard, crash budget, admin `mode`, `load`, `unload`, `router/stop`.
12. **`Port context overflow mapping from hal-route`**: `policies/overflow.py`,
    `policies/__init__.py`.
13. **`Port effort policy from hal-route`**: `policies/text.py`, `policies/effort.py`, both
    formats.
14. **`Port route and delegate policies from hal-route`**: `policies/route.py`,
    `policies/delegate.py`, LRU.
15. **`Port swap scheduler with KV slot save and restore`**: `policies/swap.py`, swap wait
    deadline, load-main-when-idle.
16. **`Add gpu-light v1 claims and idle GPU unload`**: `gpu/__init__.py`, `gpu/manager.py`,
    `gpu/lightv1.py`, off flag, `gpu/release` admin route, Ollama offloaded unload,
    `release-gpu` CLI.
17. **`Add peer GPU guard`**: `gpu/peers.py`.
18. **`Add gpu-light v2 claims and command reclaimer`**: `gpu/lightv2.py` (JSON-RPC per GL),
    per-target claims and classes, idle claim release, reclaimer registration,
    `--reclaim`.
19. **`Add session tokens with leases and admin routes`**: `sessions.py`, scope checks,
    concurrency caps, `/api/*` refusal for sessions, leases and the off-flag exemption.
20. **`Add per-session model socket forwarder`**: `forwarder.py`, REQ-SEC-66 allowlist,
    unix and TCP upstreams.
21. **`Add named client tokens and client mode`**: `clienttokens.py`, `token issue|revoke|
    list`, `[gateway].url` refusal in `serve`.
22. **`Add gateway admin client for halcore`**: `client.py`.
23. **`Add remote backend opt-in with warnings`**: remote detection, https rule, headers,
    `owned_by`, session and client exclusion.
24. **`Add hal-gateway CLI`**: `cli.py` commands over `GatewayClient`, exit codes.
25. **`Add hal-gateway systemd unit template`**: `data/hal-gateway.service` with `@BIN@`.
26. **`Add hal.conf import for gateway settings`**: `legacy.py`.
27. **`Add gateway user docs and how-tos`**: `docs/user/gateway.md`, `docs/user/modes.md`
    (MODES.md content moved), `docs/howto/connect-a-harness-to-the-gateway.md`,
    `docs/howto/run-a-second-hal-install.md`, `docs/howto/add-a-cloud-backend.md`,
    `docs/howto/pin-a-model.md`, index rows.
28. **`Switch hal launcher to the gateway`** (halcore, after its spec's `hal connect`
    exists): `hal serve/stop/status/mode` call `hal-gateway`; `power.py` calls
    `release_gpu`.
29. **`Remove hal-serve, hal-route.py and gpulight hook`**: delete the old files, the
    `MODES.md` server section, `HAL_ROUTE_URL`; CHANGELOG entry.
30. **`Add docker launcher for llama-server`** (optional, last): `launchers.py` docker
    branch with `--runtime=nvidia`, `docker`+`gpu` test.

Steps 1 to 8 give a usable, pinned gateway in front of Ollama; 9 to 15 replace `hal-serve`
and `hal-route.py`; 16 to 23 add GPU, sandbox and remote integration. Step 18 needs
gpu-light v2's daemon only for its manual check; its tests use `FakeGpuLightV2`.

## Decisions

1. **Stdlib only.** Measured overhead is 0.2 to 0.4 ms per request and about 40 us per
   streamed event (section "Measured proxy overhead"); no dependency is justified.
2. **No protocol translation.** Both local servers speak all three APIs; translation is a
   large surface (tool calls, streaming events) that would duplicate llama.cpp's own and
   could be wrong in subtle ways. A backend that lacks an API gets a clear 404.
3. **The managed router keeps 127.0.0.1:8080, now with a generated API key.** The brief
   fixes the port; the key makes "reached only through the gateway" true for local processes.
   The unix-socket listen mode is verified and available as `listen = "unix"`, but is not the
   default because the router's child model processes listen on TCP anyway
   (`LLAMA_SERVER_ROUTER_PORT` in the binary [unverified: whether children require the key;
   01-security names the check]).
4. **Modes carry their own policy wiring** in a `[hal]` section, so `hal-serve`'s and `hal`'s
   case statements (`hal-serve:142-145, 180-191`, `hal:127-139`, `hal:159-168`) become data.
   Since 0.2.0 no mode ships with Hal: `hal tune` writes `tuned` into `~/.config/hal/modes/`.
5. **Two kinds of built-in alias.** Role aliases (`hal-chat` ... `hal-vision`) follow the
   `[hal]` table, which stays the single source of truth for model roles (resolution D1);
   mode aliases (`hal/main`, `hal/agent`) follow the active router mode. The slash form cannot
   collide with a preset name, and `hal` is reserved as a backend name. Preset names stay
   valid for compatibility.
6. **Policies apply to `/v1/messages` and `/v1/chat/completions` only.** `hal-route.py`
   handled Anthropic only; Chat Completions is the agent's baseline, so both get the port.
   Responses and Ollama native pass through.
7. **Route memory is an LRU of 1024 conversations**; `hal-route.py`'s dict grew without
   bound.
8. **Swap waits are bounded** (`swap_wait_s = 7200`); today a request can wait forever.
9. **With gpu-light v2, at most two claims, one per class, held per use** (GL "Hal gateway"
   step 2, resolution D7, D8): `hal-gateway-interactive` and `hal-gateway-service` follow
   requests, are released two minutes after the last one,
   and models stay loaded until gpu-light reclaims them. With v1, one gateway-wide lease bound
   to the gateway's pid, as today. Either way the gateway, not each launcher, owns the claim;
   `hal-session-<pid>` claims go away.
10. **gpu-light v1 claims are written directly**, not through the `gpu-light` CLI: the
    protocol is specified for exactly this, and it removes a subprocess with a timeout from the
    request path.
11. **gpu-light v2 is reached with an own stdlib JSON-RPC client and frees the gateway's VRAM
    through a `command` reclaimer** (`hal-gateway release-gpu --reclaim`). Importing
    `gpu_light` would break the layering (nothing in Hal imports it, resolution A6); a
    `llama-router` reclaimer would unload models behind the swap scheduler's back
    (resolution D7). GL is the source of truth for the wire format.
12. **GPU coordination fails open** (proceed without a claim), matching today's hooks and
    v1's intent; peers that hold the GPU and the off flag fail closed. `on_busy = "fail"`
    exists for users who prefer closed.
13. **Admin mutations only on the unix socket.** The TCP port may be forwarded to a laptop;
    a remote client should use models, not switch modes or mint sessions.
14. **Sessions live in memory; client tokens on disk as hashes.** Sessions are per process
    and re-minted cheaply, so no token is at rest; client tokens must survive restarts for
    `hal remote` (resolution D6), so only their sha256 is stored.
15. **Remote backends must use https and be opted in per backend**, and sessions and client
    tokens exclude them by default. LAN servers go through an SSH tunnel (07-remote), which
    makes them loopback.
16. **`count_tokens` is estimated where the backend lacks it** (Ollama), and labelled as an
    estimate, because Claude Code calls it and a 404 would break it [unverified: how Claude
    Code reacts to a 404 there]. `/v1/responses/input_tokens` gets no estimate: its only Hal
    caller (03) handles a missing endpoint.
17. **Anthropic-shaped `/v1/models` when `anthropic-version` is present.** Anthropic SDKs
    send that header; OpenAI SDKs do not.
18. **The gateway unit is not `X-HalGPU`**; `hal off` frees VRAM through the admin API and
    the off flag keeps it free.
19. **Responses for streams close the connection**, as `hal-route.py` does; re-chunking would
    add code for no measured gain.
20. **Session request cap 8 MiB, owner cap 32 MiB.** A full 256K-token prompt is about 1 MiB
    of JSON (measured: 1,000 messages of 200 words plus 40 tools is 1.0 MiB).
21. **TCP listener off by default, foreign `Host` gets 403** (resolution D4, REQ-SYS-10).
    `hal connect` and `hal remote authorize` turn it on; the owner token file exists from the
    first start so `halkit.llm` and `hal connect` never race its creation.
22. **The manifest is mandatory for what the gateway loads or serves from Ollama**
    (resolution D2, REQ-SEC-79): router files by sha256, Ollama models by digest. External
    servers load their own files and are shown as unverified rather than refused, because the
    gateway cannot see their files; refusing them would only push users around the gateway.
23. **`models.toml` is user config, verification state is gateway state.** `serve` never
    writes the user's file; `verified_at`, size, mtime_ns and inode live in
    `<state>/gateway/verified.json`.
24. **Leases exempt from the off flag but not from reclaim** (resolution D9). The off flag
    is a person saying "stop the assistant"; a reclaim is gpu-light making room after an
    idle period, and reloading on the next request is the price GL already weighs.
25. **Claim class follows the principal, not a client header.** Clients cannot set it (every
    `X-Hal-*` header is stripped); halcore picks it when it mints a session, so a box cannot
    promote its own work to `interactive`.

## Open questions

1. **Idle unload with gpu-light v1 or none.** Unloading after 15 minutes (`idle_unload_s`)
   frees the GPU for batch jobs but makes the next request reload the mode (about 20 s for
   `gpu`, minutes for `hybrid`) and lose the prompt cache. With v2 this question goes away
   (gpu-light reclaims only on demand). Is 900 s right for v1 users, or should coding modes
   hold longer (for example 3600 s)?
2. **Remote backends over plain http.** This spec refuses `http://` to a non-loopback host
   even with `allow_remote`, pushing LAN servers (a second GPU box) through an SSH tunnel. Is a
   trusted-LAN exception wanted?
