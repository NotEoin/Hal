# Agent developer notes

How `packages/hal-agent` is put together and how to change it. The requirements are in
[03-agent](../design/03-agent.md); this page says where the code stands against them.

## Modules

| Module | Role |
| --- | --- |
| `messages.py`, `events.py` | Wire-independent `Message`, `ToolCall`, `ModelReply` (design name `Completion`; `provider.Completion` is the older shape), stream items, and the 12 validated turn `Event` types. Not yet used by `loop.py` or `provider.py` |
| `reasoning.py`, `streaming.py` | Inline reasoning splitter (think, Gemma 4, harmony; holds back at most 32 characters) and the SSE, Chat Completions, Anthropic and Responses stream readers. `anthropic.py` and `responses.py` use them for `stream = true`; the Chat Completions provider keeps its own reader |
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
- A `session/prompt` with several text blocks is joined for the model, but the last block is the
  request: tool search and the session title use it alone, so a client's channel instructions
  (hal-voice's persona) never become the search query or the title.

## State exposed to the TUI

Agent updates (`Agent.on_update`) and the ACP `session/update` they map to. A new stream line
must be listed here in the same commit.

| Agent update | ACP `sessionUpdate` | Fields |
| --- | --- | --- |
| `usage` | `usage_update` | `used` (tokens in context after the reply), `size` (window), `_meta.hal.promptTokens`, `completionTokens`, `tokensPerSecond` (completion tokens over reply wall time) |
| `compaction` | `hal_compaction` | `reason` (`auto` or `manual`), `method` (`mask`, `summary`, `note`), `beforeTokens`, `afterTokens` |
| `plan` | `plan` | the `todo` list as ACP plan entries |

`session/compact` takes an optional `focus` string that the summary keeps detail on.

`session/fork {sessionId}` copies a stored session under a new id (optional `_meta.hal.sessionId`), opens it, replays it as updates and returns `{sessionId, modes}`. It needs the local store: with host records the host copies its own and uses `session/load`.
Verified by `tests/test_context.py` and `tests/test_budget.py`.

## Sessions

`--resume ID` and `--continue` load a stored session; `--fork` with either copies it first
(`SessionStore.fork`, header gets `forked_from`) so the original stays unchanged. The first prompt
of a new session is written as a `title` record (ASCII, 60 characters) and shown by
`SessionStore.list`. Transcripts live under `state_dir()/sessions/<sid>/`. Verified by
`tests/test_sessions.py`.

## Tool reliability

- `patch` takes SEARCH/REPLACE blocks or a unified diff for one file. Each block uses the `edit`
  matcher (exact, line-number stripped, whitespace-insensitive); the patch is all-or-nothing and
  a failed block is named in the retry message.
- `repo_map` (query, max_tokens) lists source files with top-level symbols (Python `ast`, a regex
  for other languages), ranked by query words and cut at the token cap; output is deterministic.
- `git` (status, diff, log, commit) runs fixed git commands in the copy through `halkit.proc.run`
  with hooks disabled; it is left out when `run` is. Review mode asks before it (kind `execute`).
  Web fetch and search stay with the online pack, reached only through the broker.
- Profile key `tool_set = "compact"` offers only `COMPACT_TOOLS` (read, grep, edit, write, run,
  finish) so small models choose among fewer tools.
- Profile key `tool_constraint` (`none`, `grammar`, `json_schema`; `halagent.constrain`) applies only
  with `tool_format = "prompt"`. `grammar` sends a GBNF `grammar` that forces a `<tool_call>` block
  naming an offered tool (or plain text); `json_schema` sends `response_format` with a schema of a
  call or `{"answer": text}`, and the answer is unwrapped. Names are sorted so the request stays
  byte-stable. Servers that ignore the keys (Ollama) fall back to the repair parsers.

## Prompt-cache friendliness

The system prompt is built once per session and the tool list has a fixed order, so the prefix of
every request is byte-identical between compactions (history is append-only; masking and
summaries run as one sweep). Profiles pass `cache_prompt` (default true) and optionally `id_slot`
to llama.cpp; other servers ignore them. Verified by `tests/test_context.py`
(`test_consecutive_turns_share_an_identical_prompt_prefix_and_tool_list`).

## Tests

```sh
cd packages/hal-agent && uv run --offline pytest -q   # hermetic: fake model server and fake broker
```

The `server` fixture hides the agent's start-up `GET /v1/models` from `server.requests` and keeps
it in `server.model_list_requests`. Keep both autouse fixtures in `tests/conftest.py`
(`HAL_STATE_DIR`, `HAL_CONFIG`): without them a test reads the user's real config.

## Not built yet

Unverified items and gaps against the design, so a reader does not assume them:

- Moving `loop.py` and the Chat Completions provider onto `messages.py`, `events.py` and
  `streaming.py`. The Anthropic and Responses providers now stream (they fold the reader's
  items into the older `Completion`, so reasoning is dropped for now); their readers are tested
  against synthetic events only, and the Responses reasoning event names are unverified against
  a live llama-server.
- The upstream ACP schema `schema-v1.26.0` is not vendored: it needs the network and a
  maintainer's sha256. `tests/fixtures/acp/schema.subset.json` is a hand-written subset used by
  `tests/test_acp_conformance.py`; see `tests/fixtures/acp/README.md` to vendor the real file.
  Path mapping between host and box, the `notice` sent when a boxed agent has no transcript path.
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

## Subagent updates for the TUI

Each child reports to the parent's `on_update` as `{"kind": "subagent", "id", "agent", "phase"}`
with `phase` one of `start` (plus `task`), `tool` (plus `tool`, the name of a call the child made)
and `done`. ACP sends it as `sessionUpdate = "hal_subagent"` with `subagentId`, `agent`, `phase` and
the optional `task` and `tool`. A TUI nests the child's lines under the parent's `subagent` call.
Nesting depth is one. Definitions come from `halagent.subagents.discover_agents`.

## Model roles and ACP

`AgentSettings.models` holds the roles of `[agent.models]` (`main`, `fast`, `architect`, `editor`).
`Agent.resolve_model` maps a role to a model id, `switch_model` accepts either, and `_fast_provider`
serves compaction summaries and `_retitle`. `Agent.split` turns on `halagent.architect.run_split`.
ACP: `session/set_effort {sessionId, level}` sets the effort sent as `hal_effort` in each Chat Completions body
(the gateway reads it; the header form never crosses the model socket). `session/set_model` takes a role name, and `session/set_split {sessionId, enabled}` toggles the
split (an error when `architect` or `editor` is missing). While the architect plans, a `subagent`
update with `agent = "architect"` and id 0 is sent.

## Thinking blocks

When the server returns reasoning (`reasoning_content`, `reasoning`, or inline think markers that the
stream reader split off), the loop sends `{"kind": "thinking", "text"}` before the reply's text and
tool calls. ACP maps it to the standard `agent_thought_chunk`. The text is for display; this update does not change what goes into the history.
