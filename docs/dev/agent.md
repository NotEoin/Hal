# Agent developer notes

How `packages/hal-agent` is put together and how to change it. The requirements are in
[03-agent](../design/03-agent.md); this page says where the code stands against them.

## Modules

| Module | Role |
| --- | --- |
| `provider.py` | Chat Completions client over TCP or a unix socket, retry with backoff, `model_info()` from `GET /v1/models` |
| `loop.py` | The agent loop; `effective_settings()` merges settings, gateway and profile; overflow retry; `hal_find_tools` |
| `backends.py` | `ToolBackend` protocol and `BrokerBackend`; maps `_meta.hal.status` to `ok`, `error`, `denied`, `unavailable` |
| `mcp/client.py` | User MCP servers on the `mcp` SDK: config merge, `mcp__<server>__<tool>` names, untrusted envelope, one restart, backend `none` refusal |
| `mcp/lite.py` | Stdlib MCP client for the broker (4 MiB line cap) |
| `profiles/` | Shipped `model-profiles.toml` and the strict resolver |
| `assistant/` | Assistant toolset: `build_assistant()`, ported prompts, `ClaimsCheck` (REQ-AGT-51), per-turn broker tool choice (REQ-AGT-52) |
| `eval/` | `stats.py` (Wilson, McNemar, bootstrap, pass@k, promotion rule), `scenarios.py` (case file, checks, `AssistantRunner`), `report.py` (runs, reports, `compare`), `coding.py` (task format, `BoxRunner` protocol, attempt pipeline, gold/null check) |
| `agentfile.py` | Strict `agent.json` (schema 1) loader and its mapping to `AgentSettings` |
| `settings.py` | `[agent]` table, `AgentSettings`, `map_brain()` |
| `context.py`, `budget.py`, `repair/` | Masking and compaction, budgets and watchdog, leaked-call parsers and argument repair |
| `sessions.py` | Transcript writer, store, replay, export, `summary()` |
| `acp/server.py`, `acp/entry.py` | ACP server (`session/new`, `load`, `prompt`, `cancel`, `set_mode`, `_hal/record`) and the in-process `serve()` |
| `headless.py`, `cli.py`, `doctor.py`, `boot.py` | `-p` rendering and exit codes, the command line (the only module that prints), checks, shared start-up |

## Rules worth knowing

- Limit precedence: an explicit setting, then the gateway's `context_length` (window only), then
  the profile, then the default. A setting equal to its default counts as "not set".
- `ProviderError` with status 400 and `context_length_exceeded` in the message is the gateway's
  overflow rewrite; any other 400 is raised.
- With `clientCapabilities._meta.hal.records = true` the agent writes no transcript file and
  sends `_hal/record` notifications; the first one is the `session` header. Without it the agent
  writes `state_dir()/sessions/<sid>/transcript.jsonl` itself.
- `session/load` takes records from the request (`_meta.hal.records`) or the local store, and
  fails with -32602 when it has neither.
- `hal-agent --acp` and `acp.serve()` share one code path; a test compares their output.

## Tests

```sh
cd packages/hal-agent && uv run --offline pytest -q   # hermetic: fake model server and fake broker
```

The `server` fixture hides the agent's start-up `GET /v1/models` from `server.requests` and keeps
it in `server.model_list_requests`. Keep both autouse fixtures in `tests/conftest.py`
(`HAL_STATE_DIR`, `HAL_CONFIG`): without them a test reads the user's real config.

## Not built yet

Unverified items and gaps against the design, so a reader does not assume them:

- Anthropic and Responses providers, token counting endpoint, `messages.py` and `events.py`.
- Vendored ACP schema and conformance tests, path
  mapping between host and box, the `notice` sent when a boxed agent has no transcript path.
- Assistant: the claims check runs on whole completions, not on a token stream, so a voice client
  hears the reply only after the round ends; `history_turns` is not applied (the context manager
  trims instead). The scenario library has no runner here: halcore implements `AssistantRunner` (and `probe`, which the design leaves implicit). `hal eval` in halcore still uses `Brain`.
  The coding task set has 3 of the 12 planned tasks (`eval/coding/tasks/`: `csv-quote`, `leap-century`,
  `dedupe-stable`); the gold/null check runs locally with bash in the tests, not in docker. The
  `BoxRunner` is halcore's. `results.jsonl` has the counters the agent run can report; the repair,
  nudge, compaction and overflow counters of the design are not collected yet.
- Directive I1 (always boxed): the box and its `/scratch` come from 02-sandbox and halcore; this
  package only runs inside one when `hal` starts it so.
- Nothing has run against a real llama-server, Ollama, gateway or docker.

User MCP servers: only `agent.json` `mcp.servers`, a project `.mcp.json` (with a real box and
`project_servers = "box"`) and ACP `mcpServers` are read; `config.toml` is not. `notice` text goes to
the log and stderr, not to the ACP client. `pinned` is stored but every server tool is offered.
A call that crashes its server is not retried; the next call restarts it (once).

`agent.json` is consumed except for: `tools.read_*`, `grep_*`, `glob_max`, `run_*`, `write_max_chars`
(the tools keep their built-in limits), `workspace`, `host_root`, `scratch`, `channel` (only for the
assistant), `broker_call_timeout_s` and `api`. The first ACP session of a process takes
`session_id`; `permissions` sets the default mode of every session.
