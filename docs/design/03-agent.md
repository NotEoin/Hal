# halagent: agent library and coding agent

`halagent` is the part of Hal that talks to a model and turns its replies into tool calls. It
holds the providers, the agent loop, the tool-call repair layer, the coding toolset, context
management, sessions, prompts, the MCP client, the ACP agent server, headless mode, model
profiles and the evaluation library. The same loop drives `hal code` (the sandboxed coding
agent, `--toolset coding`) and the assistant that `hal chat`, hal-voice and the Telegram pack
reach through `hal acp --channel <name> --toolset assistant`. Every
front end talks to the agent over ACP; nothing outside Hal imports `halagent`.

Status: draft 2026-10-23, revised 2026-10-24 to match `RESOLUTIONS.md`.

Requirement ids in this document are `REQ-AGT-NN`. Withdrawn ids stay listed as withdrawn and
are never reused. Facts carry `[verified: how]` or `[unverified]`; "relayed" means a number
reported by the development runs from forum posts or papers and not reproduced here. Binding
inputs, in priority order: `~/src/.dev-notes/specs/RESOLUTIONS.md`, then
`~/src/.dev-notes/BRIEF.md` (2026-10-23, including "Host facts"). Where this
spec and either of them disagree, they win. Experiment scripts cited as `spec-tmp/agent/...`
lived in a scratch directory that was wiped on 2026-10-23; the results stand as recorded on
that date, and each such fact names how to re-check it.

## Contents

1. Scope and non-goals
2. Requirements
3. Design
   - 3.1 Package layout
   - 3.2 Internal message and event model
   - 3.3 Providers and transport
   - 3.4 Model profiles
   - 3.5 The agent loop
   - 3.6 Tool-call repair layer
   - 3.7 Coding toolset
   - 3.8 Assistant toolset
   - 3.9 Context management
   - 3.10 Sessions
   - 3.11 Prompts
   - 3.12 AGENTS.md, CLAUDE.md and Agent Skills
   - 3.13 MCP client and broker client
   - 3.14 ACP agent server
   - 3.15 Headless mode
   - 3.16 CLI synopsis
   - 3.17 Configuration keys
   - 3.18 Evaluation
4. Failure modes
5. Migration
6. Test plan
7. Implementation plan
8. Decisions
9. Open questions

## 1. Scope and non-goals

In scope (package `packages/hal-agent`, import name `halagent`):

- Providers for OpenAI Chat Completions (streaming SSE), OpenAI/Open Responses and Anthropic
  Messages, behind one internal message and event model, over a stdlib HTTP transport that
  also speaks HTTP over unix sockets. The agent reaches models only through the gateway and
  only with these wire formats (RESOLUTIONS D3, G5); there is no Ollama-native provider.
- The agent loop: events, cancellation, sequential execution of multiple calls, budgets,
  watchdog, nudges, and a hook for the assistant's claims check.
- The tool-call repair layer for leaked, truncated, misnamed and mistyped calls.
- Two toolsets, chosen with `--toolset`: `coding` (`read`, `grep`, `glob`, `edit`, `write`,
  `run`, `todo`, `finish`, plus broker and user MCP tools) with exact schemas and caps, and
  `assistant` (host tools through the broker only, selected per turn with `hal_find_tools`).
- Context management: token counting, window check, observation masking, summary compaction,
  a harness-maintained state note.
- Sessions: the transcript record schema (RESOLUTIONS E2/E3: 03 owns it), resume, export.
- Prompts for the coding agent and the assistant; AGENTS.md / CLAUDE.md loading; Agent Skills.
- MCP client for user servers (in the box only) and a stdlib client for Hal's broker.
- ACP v1 agent server (`hal-agent --acp`, and `halagent.acp.serve` for the unboxed fallback).
- Headless mode (`hal-agent -p`) and the headless output schema for `--json` and
  `--stream-json` (RESOLUTIONS F1: 03 owns the schema, 06 owns the flags and exit codes).
- A TOML model-profile catalogue (`model-profiles.toml`) seeded from the models on this
  machine.
- The evaluation library and its task formats (RESOLUTIONS F4): assistant scenarios, a small
  coding task set, statistics. `hal eval` (06-core-cli) wraps it.
- The `[brain]` to `[agent.assistant]` key mapping that `hal configure --migrate` applies.

Non-goals (owned elsewhere, or deliberately left out):

- The TUI, `hal` command, `hal code` and `hal -p` flags and exit codes, checkpoints, `/undo`,
  `/diff`, `--isolated`, assistant launch, rendering of `agent.json`, writing transcripts from
  `_hal/record` (06-core-cli, 02-sandbox). halagent provides the library calls they use.
- The sandbox, the broker, the policy engine, approvals and their surfaces, audit log, Prompt
  Guard and the untrusted-content envelope (02-sandbox, 01-security, 04-hub). halagent is a
  client of the broker and never decides host policy or carries an approval.
- Long jobs: the hub tool `core_long_task` in halcore's built-in module (RESOLUTIONS F2,
  06-core-cli). To the agent it is one more broker tool.
- The model gateway, presets, role aliases, routing, `hal-route.py` effort and swap policies
  (05-gateway). halagent is a client of the gateway.
- Sub-agents, multi-agent topologies and A2A. Evidence: no multi-agent topology beat a
  single budget-fit agent at matched compute (relayed, `understand/gemma-agent.md` 3.6;
  `dev-notes.md` lesson 3). Revisit only with an eval result.
- ACP v2 (still draft), ACP client `fs/*` and `terminal/*` methods, ACP authentication.
- Image and audio input. `promptCapabilities.image` and `.audio` are false in v1.
- Training, LoRA, fine-tuning.
- Cost accounting for cloud APIs beyond token counts.

## 2. Requirements

Each requirement names the test that proves it. Test kinds:

- **unit**: hermetic pytest, no network, no GPU, no docker; runs by default.
- **live**: needs a real model server reached through the gateway; marker `live`,
  deselected by default (markers are 00-overview's set `gpu docker network slow bwrap systemd
  live`, RESOLUTIONS E12).
- **docker**: needs docker; marker `docker`, deselected by default. Tests that touch the
  broker socket run twice, on `runc` and on `runsc` (runsc needs `--host-uds=open` to reach
  the bind-mounted sockets; brief "Host facts").
- **manual**: a person checks it with a written procedure in `docs/howto/`.

Tests cite the requirement ids they cover in their docstring, so a coverage script can list
MUST ids without a test.

### 2.1 Providers and transport

- **REQ-AGT-01** halagent MUST provide one `Provider` interface (section 3.3) with three
  implementations: `openai` (Chat Completions, streaming SSE), `responses` (Responses,
  stateless, streaming) and `anthropic` (Messages, streaming SSE). Test: unit, one
  fake-server fixture set per API.
- **REQ-AGT-02** Every provider MUST convert the internal `Message` list to its wire format and
  its stream back to `StreamItem`s and one `Completion`, so the loop, the repair layer and
  sessions never see an API-specific shape. Test: unit, golden request bodies and a round trip
  of a scripted conversation through every provider.
- **REQ-AGT-03** The transport MUST support `http://`, `https://` and `unix://` endpoint URLs
  using only the standard library, MUST send `Authorization: Bearer <token>` when a token is
  configured, and MUST NOT write the token to logs, events or transcripts. It MUST reject a
  unix socket path longer than 107 bytes with an error naming the limit. Test: unit, fake
  server on a unix socket; log capture asserts the token never appears.
- **REQ-AGT-04** Every request MUST carry four timeouts: connect (default 10 s), first byte
  (profile `first_token_timeout_s`, default 600 s), idle between stream chunks (default 120 s)
  and total (default 1800 s). Test: unit, a fake server that stalls at each phase.
- **REQ-AGT-05** The transport MUST retry connection errors and HTTP 408, 429, 500, 502, 503
  and 504 at most 4 attempts in total, with exponential backoff and full jitter (base 1 s,
  cap 16 s), honouring `Retry-After` up to 30 s. It MUST NOT retry any other 4xx, and MUST NOT
  retry once a visible text delta has been passed to the caller. Test: unit with a fake clock.
- **REQ-AGT-06** Reasoning MUST land in `Message.reasoning` and `ReasoningDelta`, never in
  visible text, from every source: `reasoning_content` and `reasoning` (OpenAI-compatible,
  including Ollama's OpenAI endpoint behind the gateway), thinking blocks (Anthropic),
  reasoning items and events (Responses),
  and inline `<think>...</think>`, Gemma 4 `<|channel>thought ... <channel|>` and harmony
  `<|channel|>analysis<|message|> ... <|end|>` text. Test: unit fixtures per source.
- **REQ-AGT-07** Providers MUST report `Usage` (prompt, completion, reasoning, cached tokens)
  when the server returns it, and MUST mark it `estimated = True` when it does not. Test:
  unit fixtures with and without usage.
- **REQ-AGT-08** A context overflow MUST raise `ProviderError(kind="overflow")` carrying the
  server's `n_ctx` and prompt token count when present. Sources: llama.cpp error type
  `exceed_context_size_error`, the gateway's rewritten forms (OpenAI `code:
  "context_length_exceeded"`, Anthropic `prompt is too long: N tokens > M maximum`;
  05-gateway REQ-GW-40), and HTTP 400 bodies matching the overflow patterns in 3.3.6. Test:
  unit fixtures.
- **REQ-AGT-09** Setting the cancel event MUST abort an in-flight request within 1 s by shutting
  the socket down. Test: unit, stalling fake server, measured with a monotonic clock.
- **REQ-AGT-10** Tool definitions and tool results MUST use each API's native shape: OpenAI
  `tools[].function` and `role: "tool"` with `tool_call_id`; Responses `function` tools and
  `function_call_output` items; Anthropic `tools[]` with `input_schema` and `tool_result`
  blocks. Test: unit golden JSON.
- **REQ-AGT-11** Providers MUST request only the routes of the forwarder allowlist defined in
  01-security (REQ-SEC-66 as amended by RESOLUTIONS D3: chat completions, responses and
  `input_tokens`, messages and `count_tokens`, embeddings, `GET /v1/models`) and never an
  `/api/*` path. Test: unit, every request a provider builds is checked against that list.
- **REQ-AGT-12** Providers SHOULD count prompt tokens with the server when it offers an endpoint
  (`/v1/responses/input_tokens`, `/v1/messages/count_tokens`, and
  `/v1/chat/completions/input_tokens` where the server and forwarder allow it), return `None`
  when the endpoint is missing, treat 403 like 404 and 405 (RESOLUTIONS D3), and remember a
  missing endpoint for the rest of the process. Test: unit (403 and 404 paths), live
  (llama-server through the gateway).

### 2.2 The loop

- **REQ-AGT-13** `Agent.run_turn` MUST be a generator of `Event`s of the types in 3.2.3 and MUST
  emit exactly one `turn_end` per turn, with a `stop_reason` from the set in 3.5.6. Test: unit.
- **REQ-AGT-14** The loop MUST run all tool calls of one model response sequentially in the
  order emitted, at most `max_calls_per_response` (default 8); calls beyond the limit get an
  `invalid_args` error result and do not run. Test: unit.
- **REQ-AGT-15** Every assistant tool call MUST get exactly one tool result message in
  history, including calls that were cancelled, denied, refused by the watchdog or over the
  per-response limit, so the next request is valid for APIs that require pairing. Test: unit,
  history validator run after every scripted scenario.
- **REQ-AGT-16** The harness MUST enforce `max_tool_calls`, `max_model_requests` and
  `max_wall_s` per turn, ending the turn with `max_tool_calls`, `max_model_requests` or
  `max_time`. Calls to `todo`, `finish` and `hal_find_tools` MUST NOT count toward
  `max_tool_calls`. Test: unit with a fake clock.
- **REQ-AGT-17** When remaining tool calls are at most `warn_calls` (default 10) or remaining
  time is at most 90 s, every tool result MUST end with a `[hal] budget_warning:` line that
  states both remainders. Test: unit.
- **REQ-AGT-18** Watchdog: the third consecutive call with the same tool name and the same
  canonical arguments MUST NOT run; it gets an `error_type: "loop"` result that says why. A
  fourth consecutive identical call MUST end the turn with `loop`. Six consecutive error
  results MUST end the turn with `tool_errors`. Test: unit.
- **REQ-AGT-19** A response with no tool call and no visible text, a truncated tool call, or a
  call the repair layer cannot read MUST get the matching fixed nudge (3.5.5). In task mode a
  text-only response without `finish` MUST get the finish nudge. After 3 consecutive nudges the
  turn MUST end with `nudge_limit`. Test: unit.
- **REQ-AGT-20** Cancellation MUST stop the model request and kill the process group of a
  running `run` call within 2 s, end the turn with `cancelled`, and leave a valid history.
  Test: unit (fake provider, `sleep` subprocess); live (real server).
- **REQ-AGT-21** `run_turn` MUST NOT raise for model, transport, tool or repair errors. An
  unexpected exception MUST be logged with its traceback and end the turn with `error`; the
  history and transcript written so far MUST be kept. Test: unit with fakes that raise.
- **REQ-AGT-22** For the coding agent, thinking MUST follow the profile (default on).
  Reasoning MUST NOT appear in visible text, in tool arguments or in exported Markdown unless
  requested. Prior reasoning is replayed to the server only as the profile's
  `reasoning_replay` says. Test: unit.
- **REQ-AGT-23** Path confinement, protected paths, review-mode permissions, budgets and the
  watchdog MUST be enforced in code. No policy may depend only on prompt text. Test: unit, a
  scripted model that is told not to edit a protected file and does so is refused.
- **REQ-AGT-24** After each model request the loop MUST emit a `usage` event with prompt
  tokens, completion tokens, context used and window size. Test: unit.
- **REQ-AGT-25** In `review` mode, calls whose kind is `edit`, `delete`, `move` or `execute`
  MUST call the permission function before running. `reject_*` gives `error_type: "denied"`;
  `allow_always` and `reject_always` are remembered per tool name for the session. Test: unit.
- **REQ-AGT-26** Results from user MCP servers running in the box MUST be wrapped in the
  untrusted-content envelope defined in 01-security ("Taint, guard and reviewer", untrusted
  result envelope) with `trust="untrusted"` and `guard="off"` before they reach the model.
  Broker results MUST be passed to the model unchanged (the hub already applied the envelope;
  RESOLUTIONS B11). Test: unit (`test_mcp_result_uses_security_envelope`,
  `test_broker_text_is_passed_through_unchanged`).

### 2.3 Tool-call repair

- **REQ-AGT-27** The repair layer MUST run on every completion before any call executes, and
  MUST only produce calls to tools offered in that request or found by `hal_find_tools` in the
  same turn. Test: unit, a leaked call to an unoffered tool is not executed.
- **REQ-AGT-28** The leak parsers MUST recognise the formats in 3.6.2: Gemma 4 native, Qwen and
  Hermes `<tool_call>` JSON, Qwen3-Coder XML, Mistral `[TOOL_CALLS]`, gpt-oss harmony, the
  `name{json}` form halcore parses today, and a bare JSON object with `name` and `arguments`
  or `parameters`. Test: unit fixtures built from the real chat templates.
- **REQ-AGT-29** Text recognised as a leaked call MUST be removed from visible content, and the
  repair MUST be recorded on `ToolCall.repair` and in a `notice` event. Test: unit.
- **REQ-AGT-30** A truncated call (finish reason `length` with an open call marker or with
  arguments that do not parse) MUST NOT run and MUST trigger the truncation nudge. Test: unit.
- **REQ-AGT-31** Tool names MUST be resolved in this order: exact, case-insensitive, known
  prefix stripped, alias map (3.6.4). An unresolved name MUST give an `unknown_tool` error
  result listing the offered tools, never an exception. Test: unit.
- **REQ-AGT-32** Arguments MUST be parsed leniently (3.6.5): JSON, then JSON with trailing
  commas removed, then Python literals, then one level of double encoding; per-tool argument
  aliases applied; values coerced per the table; unknown arguments dropped with a note; a
  missing required argument gives `invalid_args` naming the expected parameters. Test: unit.
- **REQ-AGT-33** Every parser MUST finish in bounded time on hostile input: 5,000 seeded random
  mutations of the fixtures complete in under 5 s in total with no exception. Test: unit.
- **REQ-AGT-34** The loop MUST count per turn: `repaired_calls`, `malformed_calls`,
  `unknown_tools`, `truncated_calls` and `nudges`, and report them in `turn_end`. Test: unit.
- **REQ-AGT-35** The arguments `_confirmed`, `confirmed` and `confirm` MUST be removed from every
  call before it runs or reaches the broker, and the removal logged. A model can never approve
  anything. Test: unit.

### 2.4 Coding toolset

- **REQ-AGT-36** The coding toolset MUST be exactly `read`, `grep`, `glob`, `edit`, `write`,
  `run`, `todo` and `finish` with the schemas in 3.7.2. Every built-in tool schema MUST be flat:
  `type: object`, properties of type `string` or `integer` only. Test: unit, schema-flatness
  check over all built-in tools.
- **REQ-AGT-37** File tools MUST resolve paths against the workspace root, follow symlinks
  with `realpath`, and refuse any result outside the root, the scratch dir and the read-only
  dirs (`outside_workspace`); writes into read-only dirs and into `protect` globs (default
  `.git/**`) MUST be refused (`protected`). With backend `none`, file tools MUST open every
  path through `halkit.fsafe.open_beneath` (the one implementation, RESOLUTIONS C10; see 02
  "Backend none") and MUST refuse a path whose resolved target matches a `masked` entry in
  `session.json` (`protected`). Test: unit, including a symlink escape and, for `none`,
  `test_none_mode_refuses_masked_path_and_symlink_to_it`.
- **REQ-AGT-38** `read` MUST return at most 200 lines and 12,000 characters per call, with
  numbered lines and a continuation hint, and MUST refuse binary files. Test: unit.
- **REQ-AGT-39** `grep` MUST use ripgrep with a 20 s timeout and return at most 100 matches,
  8,000 characters and 300 characters per line; an invalid regex MUST be retried as a fixed
  string and the retry stated; without ripgrep a Python fallback MUST give the same format.
  Test: unit (both paths, ripgrep path skipped when `rg` is absent).
- **REQ-AGT-40** `glob` MUST return at most 200 paths, sorted, skipping `.git` and ignored
  files. Test: unit.
- **REQ-AGT-41** `edit` MUST match in tiers (exact, line numbers stripped, whitespace-flexible,
  unescaped), require a unique match, return a unified diff capped at 40 lines and 3,000
  characters, return the closest region on `not_found`, and refuse an edit that breaks the
  syntax of a `.py`, `.json` or `.toml` file that parsed before. Test: unit, one case per tier
  and per error.
- **REQ-AGT-42** `write` MUST create parent directories, refuse content over 200,000
  characters, and report the line counts. Test: unit.
- **REQ-AGT-43** `run` MUST execute `bash -c` in the workspace in a new session and process
  group, with stdin from `/dev/null` and no inherited file descriptors; default timeout 120 s,
  at most 600 s; on timeout or cancel it MUST send SIGTERM to the group, SIGKILL after 5 s, and
  keep the partial output; output MUST be the first 2,000 and last 6,000 characters with ANSI
  escapes removed, plus exit code and duration. Test: unit.
- **REQ-AGT-44** `run` MUST NOT be offered when no sandbox backend is active (settings
  `sandbox = "none"`). There is no opt-out: `[agent].allow_host_run` does not exist, and the
  strict config loader rejects it as an unknown key (RESOLUTIONS G4, F4). Test: unit
  (`test_run_is_not_offered_without_sandbox`, `test_allow_host_run_key_is_rejected`, ATK-41).
- **REQ-AGT-45** The harness MUST cap every tool result at `max_result_chars` (default 12,000)
  after the tool's own caps, whatever the tool. Test: unit with an oversized fake tool.
- **REQ-AGT-46** Errors MUST reach the model as the JSON envelope
  `{"status":"error","error_type":"...","message":"..."}` followed by steering lines;
  successful results are plain text followed by steering lines. Test: unit.
- **REQ-AGT-47** `todo` MUST keep a plan of at most 20 items and emit a `plan` event. Test: unit.
- **REQ-AGT-48** `finish` MUST end the turn with `finished`, using its summary as the final
  text. It is offered only in task mode. Test: unit.
- **REQ-AGT-49** In box mode the agent process MUST make itself non-dumpable
  (`prctl(PR_SET_DUMPABLE, 0)`) before the first model request, so processes started by `run`
  cannot open its file descriptors through `/proc`. Test: docker, on runc and on runsc (a
  `run` command that tries to write to `/proc/<agent pid>/fd/1` fails).

### 2.5 Assistant

- **REQ-AGT-50** Withdrawn (RESOLUTIONS A1): there is no `halcore.brain.Brain` shim and no
  Python contract for hal-voice or Telegram; they are ACP clients. Replaced by REQ-AGT-102.
- **REQ-AGT-51** The claims check MUST behave as today: the first sentence is held until it
  matches neither `SUSPECT` nor `<think>`; a suspect reply with no tool run gets one silent
  retry with `RETRY`; the no-tools assistant retries on `CLAIM` with `NO_TOOLS_RETRY`; an `OK`
  reply restores the original. Test: unit, the 7 existing brain tests ported.
- **REQ-AGT-52** Per turn the assistant MUST offer at most `tools_per_turn` (default 8) host
  tools chosen by the broker's `hal_find_tools` on the user text, plus `hal_find_tools`.
  Tools found by the model during the turn MUST be added for the turn's remaining rounds, up
  to 16 offered tools. Test: unit.
- **REQ-AGT-53** Withdrawn (RESOLUTIONS B5, F1; security review F1): no yes matcher, no held
  calls, no grants from the agent. Replaced by REQ-AGT-98.
- **REQ-AGT-54** A failed or denied host tool result MUST carry the note "This did NOT happen.
  Fix the arguments and call again, or tell the user it failed." (for `denied`: "This did NOT
  happen: it was not approved. Tell the user; do not call it again unless they ask.").
  Test: unit.
- **REQ-AGT-55** Withdrawn (RESOLUTIONS F2): there is no agent built-in `deep_work`. Long jobs
  are the broker tool `core_long_task` (06-core-cli).
- **REQ-AGT-56** Host tools MUST go only through the broker named in the settings. halagent
  MUST NOT import `halhub` or run host tools in its own process; there is no local hub backend
  and no `tools_via` key (RESOLUTIONS A2). When the broker is unreachable, its tools fail with
  `unavailable` and the session continues. Test: unit with a fake broker; root
  `tests/test_layering.py` (00-overview REQ-SYS-04) finds no `halhub` import.

### 2.6 Context

- **REQ-AGT-57** Before every model request the harness MUST estimate the prompt tokens of the
  full request (messages and tools) and ensure that the estimate plus `max_output` plus
  `safety_tokens` (512) fits the window; if not, it MUST compact first. Test: unit.
- **REQ-AGT-58** Without a server count, the estimate MUST be characters divided by 4,
  replaced after the first response by the observed tokens-per-character ratio for that model,
  and multiplied by 1.15. Test: unit.
- **REQ-AGT-59** Observation masking MUST run first: above `mask_at` (0.60 of the input
  budget) the oldest tool results outside the newest `keep_recent` (3) are replaced by one-line
  stubs until the estimate is at most `mask_to` (0.40). Test: unit.
- **REQ-AGT-60** Summary compaction MUST run when the estimate is still above `summarize_at`
  (0.85) after masking: one model call with the compaction prompt (3.11.4) over the older
  history, replaced by one summary message plus the state note; the latest user message and
  the newest `keep_recent` tool exchanges are kept verbatim. If the call fails, the state note
  alone replaces the older history. Test: unit with a scripted provider.
- **REQ-AGT-61** The harness MUST maintain a deterministic state note (3.9.5), at most 3,000
  characters. Test: unit snapshot.
- **REQ-AGT-62** If the request still does not fit after compaction, the turn MUST end with
  `context_overflow`; the harness MUST NOT knowingly send an over-window request. Test: unit.
- **REQ-AGT-63** On a server overflow error the loop MUST record the server's `n_ctx`, compact,
  and retry once. Test: unit.
- **REQ-AGT-64** Every masking and compaction MUST emit a `compaction` event and a transcript
  record, so resume reproduces the compacted history exactly. Test: unit.

### 2.7 Sessions

- **REQ-AGT-65** Every transcript record MUST follow the schema of 3.10 (one JSON object per
  line, UTC `Z` timestamps), and whoever writes the file (REQ-AGT-66) MUST flush after each
  record. Test: unit.
- **REQ-AGT-66** When the ACP client advertises `clientCapabilities._meta.hal.records = true`
  (halcore always does), the agent MUST send every record as a `_hal/record` notification and
  write no transcript file; the host writes `state_dir()/sessions/<sid>/transcript.jsonl`
  (06-core-cli; RESOLUTIONS E2/E3). Without that capability the agent writes the same path
  itself, but only when `sandbox = "none"`; in box mode it keeps no transcript and says so in
  a `notice`. Test: unit (all three cases).
- **REQ-AGT-67** Resume MUST rebuild the exact message list, compactions included, and continue
  the session; ACP `session/load` MUST replay it as `session/update` notifications. Test: unit.
- **REQ-AGT-68** Export MUST produce `md` and `jsonl` (RESOLUTIONS E3); reasoning is left out
  unless asked for; argument names matching the secret pattern are masked. Test: unit.
- **REQ-AGT-69** Loading MUST skip a torn last line with a warning and fail loudly on any other
  invalid line. Test: unit.

### 2.8 Prompts, instructions and skills

- **REQ-AGT-70** The coding system prompt MUST be rendered from the templates in 3.11 with
  budget numbers computed from the actual budgets, in a task variant and an interactive
  variant, and the rendered base prompt MUST stay under 1,600 tokens by the 4-character
  heuristic. Test: unit snapshot.
- **REQ-AGT-71** AGENTS.md loading MUST follow 3.12.1: files from the workspace root down to the
  working directory, root first; `CLAUDE.md` used in a directory only when it has no
  `AGENTS.md`; `@path` imports inside the root to depth 2; the user-global file first; total
  cap 16,000 characters with a truncation marker. Test: unit.
- **REQ-AGT-72** Skill discovery MUST follow 3.12.2: `SKILL.md` frontmatter validated against
  the Agent Skills rules, at most 12 skills and 3,000 characters listed, invalid skills skipped
  with a warning, project skills overriding user skills of the same name. Test: unit.
- **REQ-AGT-73** No prompt text may present shell commands formatted like tool calls. Test:
  unit lint over all prompt templates.
- **REQ-AGT-74** The assistant prompts MUST be PERSONA, STYLE, TOOL_RULES and NO_TOOLS from
  `halcore/brain.py`, unchanged except the edits listed in 3.11.5. Test: unit snapshot.

### 2.9 MCP and the broker

- **REQ-AGT-75** The MCP client for user servers MUST use the official `mcp` SDK
  (`>=2.3,<3`) and connect stdio servers as subprocesses of the agent inside the box. With
  `sandbox = "none"` the agent MUST NOT start any user MCP server that is not marked
  `host = true` (those run on the host through the broker, RESOLUTIONS E6, F4), and MUST say
  so in a `notice`. Entries with a `url` (Streamable HTTP) MUST be rejected with a config
  error in v1, because the box has no network and on the host they would bypass the broker.
  The client MUST NOT advertise the `elicitation` capability. Test: unit with an in-process
  server (`test_backend_none_refuses_non_host_mcp_servers`, ATK-40,
  `test_url_mcp_entries_are_rejected`); docker with a stdio fake server.
- **REQ-AGT-76** MCP tools MUST be named `mcp__<server>__<tool>`, their results capped and
  wrapped as untrusted, each call bounded by a timeout (default 60 s). Non-flat schemas are
  passed through and reported once in a `notice`. Test: unit.
- **REQ-AGT-77** The broker client MUST be stdlib only and speak MCP JSON-RPC with newline
  framing over the broker's unix socket, using the `initialize` handshake at protocol
  `2025-11-25`; it MUST support `tools/list`, `tools/call`, `ping` and
  `notifications/cancelled`; it MUST refuse a message over 4 MiB in either direction (the
  broker cap, RESOLUTIONS E9) and MUST advertise no client capabilities, so the broker can
  never route an approval to the box (RESOLUTIONS B4). Test: unit with a fake broker.
- **REQ-AGT-78** A crashed or unreachable MCP server or broker MUST only make its own tools fail
  with `unavailable`; the session continues; a stdio server is restarted at most once per
  session. Test: unit.

### 2.10 ACP

- **REQ-AGT-79** `hal-agent --acp` (and `halagent.acp.serve`) MUST implement ACP v1 as agent:
  `initialize`, `session/new`, `session/load`, `session/prompt`, `session/cancel` and
  `session/set_mode`; it MUST send
  `session/update` and `session/request_permission`. Every message the agent sends or accepts
  in the test suite MUST validate against the vendored ACP schema `schema-v1.26.0`. Test: unit
  conformance suite.
- **REQ-AGT-80** The `initialize` result MUST advertise protocol version 1, `loadSession: true`,
  `promptCapabilities` with `image: false`, `audio: false`, `embeddedContext: true`,
  `mcpCapabilities` with `http: false` and `sse: false`, and empty `authMethods`. Test: unit.
- **REQ-AGT-81** Events MUST map to ACP as in 3.14.3, and stop reasons as in 3.14.4. Test: unit.
- **REQ-AGT-82** On `session/cancel` the agent MUST stop the turn, send any final updates, then
  answer the pending `session/prompt` with `stopReason: "cancelled"`. Test: unit.
- **REQ-AGT-83** In ACP mode stdout MUST carry only ACP messages; logs go to stderr. Test: unit,
  every stdout line parses as JSON-RPC.
- **REQ-AGT-84** The agent MUST NOT call client `fs/*` or `terminal/*` methods in v1. Test: unit.

### 2.11 Headless

- **REQ-AGT-85** `hal-agent -p TASK` MUST run one task in task mode, accept the flag names that
  06-core-cli fixes for `hal -p` where they apply (3.15.1), and exit with 06's exit codes
  (06-core-cli "Command reference", mapped in 3.15.4). `TASK` given as `-` is read from
  stdin. Test: unit.
- **REQ-AGT-86** Without a format flag the run MUST print only the final text; `--json` MUST
  print one result object; `--stream-json` MUST print one JSON object per line, flushed per
  line, each with a UTC `Z` `ts`, as specified in 3.15. `hal -p` renders the same schema on
  the host (RESOLUTIONS F1). Test: unit snapshots.
- **REQ-AGT-87** SIGINT or SIGTERM MUST cancel the run and exit 130 after printing a result
  marked `error_cancelled` (json and stream-json). Test: unit (signal sent to a subprocess).

### 2.12 Model profiles

- **REQ-AGT-88** Profiles MUST be read from the shipped `halagent/profiles/model-profiles.toml`
  and the user's `~/.config/hal/model-profiles.toml` with the schema of 3.4; unknown keys and
  wrong types MUST fail loudly with file and key. halagent MUST NOT read
  `~/.config/hal/models.toml`, which is the gateway's model manifest (RESOLUTIONS D2). Test:
  unit.
- **REQ-AGT-89** Resolution order MUST be: `[defaults]` < shipped entry < user entry <
  `config.toml` overrides < CLI flags. The entry is chosen by the gateway's
  `meta.hal_profile` for the model when present (05-gateway `GET /v1/models`, RESOLUTIONS
  D5), else by the first glob match within each file. `hal-agent profiles show MODEL` MUST
  print each value with its source. Test: unit.
- **REQ-AGT-90** The shipped catalogue MUST contain the seeded entries of 3.4.3, and these
  models from the recorded `ollama list` fixture MUST resolve to their family entry, not the
  defaults: `gemma4:12b-it-qat`, `gemma4:26b-a4b-it-qat`, `gemma4:31b-it-qat`, `qwen3.5:9b`,
  `qwen3.5:4b`, `qwen3.6:27b-coding-mtp-q4_K_M`, `qwen3.8:27b`, `qwen3-coder:30b`,
  `gpt-oss:20b`, `ministral-3:14b`, `mistral-small3.2:24b`, `devstral-small-2:24b`,
  `magistral:24b`, `glm-4.7-flash:latest`, `granite4.2:8b`, `phi4-mini:latest`. Gateway role
  aliases (`hal-chat`, `hal-code`, ...; RESOLUTIONS D1) resolve through their
  `meta.hal_profile`, and to the defaults when the gateway gives none. Test: unit.
- **REQ-AGT-91** An unknown model MUST run with `[defaults]` and one `notice` saying so. Test:
  unit.
- **REQ-AGT-92** A profile with `tool_format = "prompt"` MUST describe the tools in the system
  prompt and parse calls from text with the Hermes parser; `auto` MUST choose it when the
  server reports that the model lacks tool support. Test: unit.

### 2.13 Evaluation

- **REQ-AGT-93** The assistant scenario library MUST accept `eval/assistant/scenarios.toml`
  with the fields and check semantics of 3.18.1, run each case through an `AssistantRunner`
  that halcore implements with an in-process broker (RESOLUTIONS A2; 06-core-cli "Eval"),
  pass `confirm_first` only when the broker requested approval for that tool (06 REQ-CLI-79),
  and write a report with per-case results and Wilson intervals. Test: unit with a scripted
  runner.
- **REQ-AGT-94** The coding task set MUST have at least 12 tasks, each with a gold patch that
  passes and an empty patch that fails its check. Test: docker (gold/null check over all tasks).
- **REQ-AGT-95** The coding runner MUST run each attempt in a fresh box, grade in a second fresh
  box with protected paths reset, and write the metrics of 3.18.3. Test: docker.
- **REQ-AGT-96** `halagent.eval.stats` MUST implement Wilson intervals, the exact McNemar test,
  a paired cluster bootstrap, pass^k, pass@k, the flip rate and the promotion rule, each checked
  against known values. Test: unit.
- **REQ-AGT-97** A comparison MUST pair results on identical case ids and repeat indices, and
  MUST NOT name a winner unless the promotion rule holds. Test: unit.

### 2.14 Approvals, toolsets, sessions and settings

- **REQ-AGT-98** The agent MUST NOT hold, replay, grant or deny host tool calls, and MUST NOT
  read approval from any text, model output or user message. A broker call that policy sends
  to `ask` simply waits until the broker answers; the approval is a host-originated request
  rendered by a host surface (01-security "Approval surfaces and protocol", RESOLUTIONS B5).
  The model may say it asked; it never carries the approval. Test: unit
  (`test_broker_ask_call_waits_for_broker_answer`, `test_user_yes_text_is_not_an_approval`,
  ATK-36).
- **REQ-AGT-99** `hal-agent --acp --toolset coding|assistant` and
  `halagent.acp.serve(reader, writer, *, toolset, workdir, settings)` (3.14.8) MUST run the
  same server code, so the unboxed fallback (RESOLUTIONS A1) behaves like the box apart from
  the `sandbox = "none"` rules. Modes are `auto` and `review` only (RESOLUTIONS F3). Test:
  unit (`test_serve_and_cli_send_identical_messages`).
- **REQ-AGT-100** Every session id MUST be 12 lowercase hex characters, and the same string
  MUST serve as the ACP `sessionId`, the transcript directory name and the checkpoint ref
  component (RESOLUTIONS E2). The agent MUST take it from `session/new` `_meta.hal.sessionId`
  when present, else from `agent.json` `session_id` for the first session of the process,
  else generate it with `secrets.token_hex(6)`. Test: unit.
- **REQ-AGT-101** `halagent.settings.map_brain(brain: Mapping[str, object]) -> MappedConfig`
  MUST implement the `[brain]` to `[agent.assistant]` mapping of 3.17.3 as a pure function
  that `hal configure --migrate` (06-core-cli) calls; every dropped key MUST come back with a
  reason. Test: unit, the user's live `[brain]` table shape as a synthetic fixture.
- **REQ-AGT-102** With `--toolset assistant` the agent MUST offer no coding tool and start no
  user MCP server in the box; its only tools are broker tools (including `host = true` MCP
  servers, which the broker serves) and `hal_find_tools`. Test: unit
  (`test_assistant_toolset_has_only_broker_tools`).

## 3. Design

### 3.1 Package layout

```text
packages/hal-agent/
  pyproject.toml            name "hal-agent"; requires-python ">=3.11";
                            dependencies: hal-kit>=0.1,<0.2 (RESOLUTIONS C3), mcp>=2.3,<3
                            scripts: hal-agent = "halagent.cli:main"
  src/halagent/
    __init__.py             version, public re-exports (Agent, Event, ...)
    __main__.py             python -m halagent -> cli.main
    cli.py                  hal-agent entry point (argparse); the only module that prints
    messages.py             Message, ToolCall, ToolSpec, Usage, ChatRequest, Completion, StreamItem
    events.py               Event and its JSON encoding
    settings.py             AgentSettings: resolved settings; to and from agent.json; map_brain()
    budget.py               Budget, BudgetView, warning text
    loop.py                 Agent: run_turn, hooks, watchdog, nudges
    context.py              token estimate, window check, masking, compaction, state note
    sessions.py             SessionWriter, SessionStore, replay, export
    instructions.py         AGENTS.md / CLAUDE.md loading
    skills.py               Agent Skills discovery and listing
    headless.py             -p mode and output formats
    profiles/
      __init__.py           load, validate, resolve
      model-profiles.toml   shipped catalogue (data file)
    providers/
      __init__.py           make_provider(endpoint, profile)
      transport.py          HttpTransport (tcp, tls, unix), SSE reader, retry
      errors.py             ProviderError
      openai_chat.py        Chat Completions
      responses.py          Responses
      anthropic.py          Messages
      reasoning.py          inline reasoning splitters (think tags, Gemma channel, harmony)
      tokens.py             server counts and the calibrated heuristic
    repair/
      __init__.py           repair_completion()
      leaks.py              per-family leaked-call parsers
      aliases.py            tool-name and argument-name alias tables
      coerce.py             lenient argument parsing and coercion
    tools/
      __init__.py           Tool protocol, ToolResult, ToolRegistry, render_for_model
      paths.py              Workspace: confinement and protected paths
      read.py grep.py glob.py edit.py write.py run.py todo.py finish.py
      search.py             hal_find_tools over broker and MCP tools
    backends.py             ToolBackend protocol, BrokerBackend (the only implementation)
    mcp/
      __init__.py
      lite.py               stdlib MCP client used for the broker
      client.py             SDK-based manager for user servers in the box (asyncio loop thread)
    acp/
      __init__.py           serve() (3.14.8)
      jsonrpc.py            newline-delimited JSON-RPC peer over a pair of byte streams
      server.py             ACP agent
      mapping.py            Event -> session/update, stop reasons, paths
    assistant/
      __init__.py           build_assistant(): the assistant toolset and its Agent hooks
      prompts.py            PERSONA, STYLE, TOOL_RULES, NO_TOOLS, RETRY, NO_TOOLS_RETRY
      claims.py             SUSPECT, CLAIM, ClaimsGate
    prompts/
      code_task.md          coding system prompt, task mode (template)
      code_interactive.md   coding system prompt, interactive mode (template)
      compaction.md         summary prompt
      nudges.toml           nudge and steering texts
      tool_prompt.md        tool description block for tool_format = "prompt"
    eval/
      __init__.py
      scenarios.py          assistant scenario loader, checker, AssistantRunner protocol, runner
      coding.py             coding task loader, BoxRunner protocol, runner, grader
      stats.py              Wilson, McNemar, bootstrap, pass^k, flip rate, promotion rule
      report.py             report schema, compare
  tests/
    test_<module>.py        one file per module (see section 6)
    fixtures/               SSE streams, templates, ACP schema, repos
eval/                       at the monorepo root
  assistant/scenarios.toml  moved from core/hal/eval/scenarios.toml
  coding/tasks/<id>/        coding tasks (3.18.2)
  cards/                    experiment cards (3.18.5)
```

Dependencies: the standard library, `hal-kit` (config loading, XDG paths, atomic writes, UTC
timestamps, logging setup) and `mcp` (only `halagent.mcp.client` imports it, lazily). Tests
also use `jsonschema`, which is already in the dependency closure of `mcp` [verified: `mcp`
2.3.0 `requires_dist` lists `jsonschema>=4.20.0`, PyPI JSON API 2026-10-23].

Layering (00-overview "Allowed imports"): of Hal's packages, `halagent` imports only
`halkit`. It never imports `halhub`, `halsandbox`, `halgateway` or `halcore`, not even lazily
inside a function
(RESOLUTIONS A2; REQ-SYS-04 scans function-level imports). halcore spawns `hal-agent --acp`
(RESOLUTIONS A7) or, for the unboxed fallback, calls `halagent.acp.serve`; no package outside
Hal imports `halagent` (RESOLUTIONS A1, A3).

### 3.2 Internal message and event model

#### 3.2.1 Messages

```python
Role = Literal["system", "user", "assistant", "tool"]
ToolKind = Literal["read", "edit", "delete", "move", "search", "execute", "think", "fetch",
                   "other"]                     # the ACP ToolKind values


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool as offered to the model."""
    name: str
    description: str
    parameters: dict[str, Any]          # JSON Schema object
    kind: ToolKind = "other"
    source: str = "builtin"             # "builtin" | "broker" | "mcp:<server>"
    counts: bool = True                 # False: does not count toward max_tool_calls


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One call the model asked for, after repair."""
    id: str                             # provider id, or "call_<turn>_<n>" when absent
    name: str
    arguments: dict[str, Any]
    repair: tuple[str, ...] = ()        # e.g. ("leaked:gemma4", "alias:bash->run")


@dataclass(slots=True)
class Message:
    """One message of the conversation, independent of the wire API."""
    role: Role
    content: str = ""
    reasoning: str = ""                 # assistant only; never shown as text
    tool_calls: list[ToolCall] = field(default_factory=list)   # assistant only
    tool_call_id: str = ""              # tool only
    tool_name: str = ""                 # tool only
    meta: dict[str, Any] = field(default_factory=dict)
    # meta keys: "masked" (stub text replaced the content), "incomplete" (stream broke),
    # "summary" (compaction summary), "error_type" (tool)


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class Sampling:
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    repeat_penalty: float | None = None
    seed: int | None = None


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model: str
    messages: Sequence[Message]
    tools: Sequence[ToolSpec] = ()
    max_output: int = 8192
    sampling: Sampling = Sampling()
    thinking: str = "on"                # "on" | "off" | an effort name such as "low"
    reasoning_replay: str = "tool_turns"  # "none" | "tool_turns" | "all"
    parallel_tool_calls: bool = False
    stop: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)   # profile passthrough per API


FinishReason = Literal["stop", "tool_calls", "length", "filtered", "refusal", "cancelled"]


@dataclass(slots=True)
class Completion:
    message: Message                    # role "assistant"
    finish: FinishReason
    usage: Usage
    raw_finish: str = ""                # the server's own value, for logs
    first_token_s: float | None = None
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str

@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str

@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    index: int
    id: str
    name: str
    arguments_fragment: str

@dataclass(frozen=True, slots=True)
class Done:
    completion: Completion

StreamItem = TextDelta | ReasoningDelta | ToolCallDelta | Done
```

`ToolCall.arguments` is always a `dict` after repair. Providers keep the raw argument string
in the `Completion` only until repair has run; history stores the repaired dict.

#### 3.2.2 Content from ACP prompts

ACP prompt content blocks become one user `Message.content` string. `text` is used as is.
`resource` with `text` becomes a fenced block headed `File: <uri>`. `resource_link` becomes
the line `Referenced file: <path>` (the model reads it with `read`). `image` and `audio` are
refused at `initialize` by capability, so they never arrive.

#### 3.2.3 Events

```python
EventType = Literal["turn_start", "text", "reasoning", "tool_call", "tool_start",
                    "tool_result", "plan", "usage", "compaction", "notice", "permission",
                    "turn_end"]


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened during a turn; JSON-serialisable through to_json()."""
    type: EventType
    session: str
    turn: int
    data: dict[str, Any]
    ts: str                             # UTC ISO 8601 with Z, from halkit
```

| `type` | `data` fields |
| --- | --- |
| `turn_start` | `prompt` (str, capped 2,000 chars), `mode` ("task" or "interactive"), `budget` (object, 3.5.4) |
| `text` | `delta` (str), `message_id` (str, one per assistant message) |
| `reasoning` | `delta` (str), `message_id` |
| `tool_call` | `id`, `name`, `arguments` (object), `kind`, `title` (str), `locations` (list of `{path, line}`), `repair` (list of str) |
| `tool_start` | `id` |
| `tool_result` | `id`, `ok` (bool), `error_type` (str), `title`, `text` (str, capped 4,000 chars for display), `diff` (null or `{path, old_text, new_text}` when the file is at most 64 KiB), `data` (object, for example `exit_code`) |
| `plan` | `entries`: list of `{content, status, priority}`; status `pending`, `in_progress` or `completed`; priority `high`, `medium` or `low` |
| `usage` | `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `estimated` (bool), `context_used`, `context_size` |
| `compaction` | `id`, `kind` ("mask" or "summary" or "note"), `status` ("in_progress", "completed", "failed"), `before_tokens`, `after_tokens`, `summary` (str or null) |
| `notice` | `level` ("info", "warning", "error"), `code` (str, for example `repaired_call`, `unknown_profile`, `mcp_down`), `text` |
| `permission` | `id`, `decision` ("allow_once", "allow_always", "reject_once", "reject_always", "cancelled") |
| `turn_end` | `stop_reason` (3.5.6), `final_text` (str), `stats` (3.5.7) |

### 3.3 Providers and transport

#### 3.3.1 Interface

```python
class Provider(Protocol):
    """A model API. Implementations are stateless apart from caches."""
    api: str                            # "openai" | "responses" | "anthropic"

    def stream(self, request: ChatRequest, cancel: threading.Event) -> Iterator[StreamItem]:
        """Yields deltas and finally one Done; raises ProviderError."""

    def count_tokens(self, request: ChatRequest) -> int | None:
        """Returns the server's prompt token count, or None when it cannot count."""

    def context_window(self, model: str) -> int | None:
        """Returns `context_length` from the gateway's GET /v1/models entry, or None."""


def make_provider(endpoint: Endpoint, profile: Profile) -> Provider: ...


@dataclass(frozen=True, slots=True)
class Endpoint:
    url: str                            # "unix:///run/hal/model.sock", "http://127.0.0.1:8741"
    api: str                            # see Provider.api
    token: str = field(default="", repr=False)
    timeouts: Timeouts = Timeouts()


@dataclass(frozen=True, slots=True)
class Timeouts:
    connect_s: float = 10.0             # a local socket answers in milliseconds
    first_byte_s: float = 600.0         # prefill of 30k tokens with CPU offload takes minutes
    idle_s: float = 120.0               # a healthy stream sends a chunk at least per token
    total_s: float = 1800.0             # 8,192 tokens at 5 tok/s is about 27 minutes


class ProviderError(Exception):
    kind: Literal["connect", "timeout", "http", "overflow", "protocol", "cancelled"]
    status: int | None                  # HTTP status when there was one
    retryable: bool
    n_ctx: int | None                   # overflow only
    n_prompt: int | None                # overflow only
```

The `first_byte_s` default matches the slow-prefill reason `hal-route.py` raised
`API_TIMEOUT_MS` for the orchestrate modes (`understand/hal-core.md` section 3); the profile
can raise it.

#### 3.3.2 Transport

`HttpTransport` wraps `http.client`. For `unix://` URLs a subclass of
`http.client.HTTPConnection` overrides `connect()` to open an `AF_UNIX` socket; the HTTP
`Host` header is `localhost`. Path prefixes after the authority are kept (for
`http://127.0.0.1:8741/v1`). Socket paths longer than 107 bytes are rejected before connecting
with `halkit.paths.check_socket_path` [verified: `OSError: AF_UNIX path too long` binding a
130-byte path, experiment `spec-tmp/agent/uds/exp.py`, 2026-10-23; script lost in the scratch
wipe, re-check with `test_unix_socket_path_over_107_bytes_fails_loudly`].

Streaming reader (SSE): read lines; `data: ` lines carry JSON; `event: ` lines name the event
(Anthropic, Responses); a blank line ends an event; `data: [DONE]` ends the stream; comment
lines (starting with `:`) are ignored.

The socket timeout is set to `first_byte_s` until the first byte and to `idle_s` afterwards; a
wall-clock check enforces `total_s`. A stdlib client over a unix socket reads chunked SSE line
by line and raises `TimeoutError` when the server stalls past the socket timeout [verified:
`spec-tmp/agent/uds/exp.py`, 2026-10-23; re-checked by the stall cases of `test_transport.py`].

Cancellation: the provider registers a callback that calls `socket.shutdown(SHUT_RDWR)` when the
cancel event is set; the reader then raises, and the provider raises
`ProviderError(kind="cancelled")`. A watcher thread per request polls the event every 100 ms.

Retry (REQ-AGT-05): `RetryPolicy(attempts=4, base_s=1.0, cap_s=16.0, retry_after_cap_s=30.0)`.
Delay before attempt `n` (from 1) is `random.uniform(0, min(cap_s, base_s * 2 ** (n - 1)))`
unless `Retry-After` gives a value, which is used up to the cap. A stream that breaks after
reasoning deltas only, with no visible text and no tool-call deltas, is retried as a new
attempt; the loop is told with a `notice` so a UI can clear the partial reasoning.

#### 3.3.3 Wire mapping

| Concept | `openai` | `responses` | `anthropic` |
| --- | --- | --- | --- |
| Path | `POST /v1/chat/completions` | `POST /v1/responses` | `POST /v1/messages` |
| Stream | SSE, `stream: true`, `stream_options: {include_usage: true}` | SSE, `stream: true` | SSE, `stream: true` |
| System | message role `system` (or profile `system_role`) | `instructions` | top-level `system` |
| Tools | `tools: [{type: "function", function: {name, description, parameters}}]` | `tools: [{type: "function", name, description, parameters}]` | `tools: [{name, description, input_schema}]` |
| Assistant call | `tool_calls: [{id, type: "function", function: {name, arguments: <JSON string>}}]` | output item `{type: "function_call", call_id, name, arguments}` | content block `{type: "tool_use", id, name, input}` |
| Tool result | `{role: "tool", tool_call_id, content}` | input item `{type: "function_call_output", call_id, output}` | user content block `{type: "tool_result", tool_use_id, content, is_error}` |
| Max output | `max_tokens` | `max_output_tokens` | `max_tokens` |
| Sampling | top-level `temperature`, `top_p`; `top_k`, `min_p`, `presence_penalty`, `repeat_penalty` passed as top-level extras (llama.cpp accepts them) | `temperature`, `top_p` | `temperature`, `top_p`, `top_k` |
| Thinking on/off | `reasoning_effort` (`"none"` for off) and `chat_template_kwargs.enable_thinking` per profile | `reasoning: {effort}` | `thinking: {type: "enabled", budget_tokens}` or omitted |
| Reasoning out | `delta.reasoning_content` or `delta.reasoning` | events `response.reasoning*.delta` | `thinking_delta` |
| Parallel calls | `parallel_tool_calls` | `parallel_tool_calls` | `tool_choice.disable_parallel_tool_use` |
| Finish | `finish_reason`: stop, tool_calls, length, content_filter | `response.completed` / `response.incomplete` (`max_output_tokens`) | `stop_reason`: end_turn, tool_use, max_tokens, refusal |
| Usage | final chunk `usage` | `response.completed.response.usage` | `message_start.message.usage`, `message_delta.usage` |
| State | stateless | stateless: `store: false`, full `input` each time | stateless |

`content_filter` and `refusal` become `FinishReason` `filtered` and `refusal`; the loop ends
the turn with stop reason `refusal` (3.5.6).

Sources: llama-server README (downloaded 2026-10-23 into `understand/dl-interop/`, which is no
longer kept; re-check against the README of the pinned llama.cpp release), Open Responses spec
(`openresponses.org/specification`, fetched 2026-10-23). The Responses reasoning event names
were not confirmed against a live llama-server, so the provider treats any event whose type
starts with `response.reasoning` and ends with `.delta` as reasoning [unverified against
llama-server b11512]. A provider assigns `call_<turn>_<n>` when a server sends a call without
an id.

Models served by Ollama are reached through the gateway's OpenAI-compatible routes like any
other backend; window size and residency (`num_ctx`, `keep_alive`) are the gateway's
business (05-gateway), not the agent's.

Gemma 4's template raises on string arguments [verified: `chat_template.jinja` of
`gemma-4-31B-it-qat-w4a16-ct` lines 248-264, local copy on the build machine], so the server must turn
the Chat Completions JSON string into an object before rendering. llama-server parses tool
call arguments before applying the template [unverified for build b11512: check with the live
test `test_gemma4_history_with_tool_calls_renders`]; providers always send each API's
documented type and never pre-parse.

#### 3.3.4 Reasoning handling

Server-separated reasoning goes straight to `ReasoningDelta`. Inline reasoning in `content` is
split by `providers/reasoning.py`, a streaming state machine that holds back at most 32
characters (the longest marker) at chunk edges:

| Family marker | Start | End |
| --- | --- | --- |
| think tags | `<think>` | `</think>` |
| Gemma 4 | `<\|channel>thought` | `<channel\|>` |
| harmony | `<\|channel\|>analysis<\|message\|>` | `<\|end\|>` |

A start marker seen in `content` switches the stream into reasoning until the end marker. An
unclosed block at the end of the stream stays reasoning and the visible text is empty, which
the loop handles as an empty reply (nudge). Gemma 4 markers [verified: local Gemma 4
`chat_template.jinja` lines 158-160 and 242]; harmony markers [verified: Ollama `gpt-oss:20b`
template via `/api/show`, 2026-10-23]; llama-server separates reasoning into
`reasoning_content` by default (`--reasoning-format` default `auto`, `deepseek` puts thoughts
in `message.reasoning_content`) [verified: `llama-server --help`, build 11512].

Replay of prior reasoning (`reasoning_replay`):

- `none`: never sent back.
- `tool_turns` (default): sent back only on assistant messages after the latest user message
  that carry tool calls. This matches the Gemma 4 template's `thinking_gate`
  (`loop.index0 > last_user_idx`) [verified: template line 240].
- `all`: every assistant message.

Wire field for replay: `reasoning_content` (openai; llama-server accepts it for templates that
support preserved reasoning, README "Reasoning support"), thinking blocks (anthropic),
reasoning items (responses). Whether Ollama's OpenAI-compatible endpoint replays
`reasoning_content` into the gpt-oss template's `$msg.Thinking` is [unverified]; the live
test for `gptoss` checks it.

#### 3.3.5 Token counting

`providers/tokens.py`:

```python
class TokenCounter:
    def __init__(self, provider: Provider, model: str) -> None: ...
    def count(self, request: ChatRequest) -> tuple[int, bool]:
        """Returns (tokens, exact). Uses the server when it can, else the calibrated estimate."""
    def observe(self, request_chars: int, prompt_tokens: int) -> None:
        """Updates the tokens-per-character ratio from a response's real prompt count."""
```

- Server counts: `openai` -> `POST /v1/chat/completions/input_tokens` with the same body;
  `responses` -> `POST /v1/responses/input_tokens`; `anthropic` ->
  `POST /v1/messages/count_tokens`. All three llama-server endpoints exist in the installed
  build [verified: `strings libllama-server-impl.so` in
  `~/.local/share/hal/llama.cpp/llama-b11512`, 2026-10-23], but the forwarder allowlist
  (01-security REQ-SEC-66, RESOLUTIONS D3) names only the `responses` and `messages` count
  routes, so in the box the `openai` count gets 403. A 403, 404 or 405 marks the endpoint
  missing for the process (RESOLUTIONS D3). Counting is skipped (estimate used) when the last
  exact count is less than 50% of the window, because each count costs a template render on
  the server.
- Estimate: `ceil(chars * ratio * 1.15)`, `ratio = 0.25` until the first response, then
  `prompt_tokens / request_chars` of the latest response for that model. The 1.15 factor
  covers template overhead and tool schemas the character count misses; it is a judgement,
  checked by the live test that compares estimates with server counts on recorded sessions
  [unverified until that test runs].

#### 3.3.6 Overflow detection

`ProviderError(kind="overflow")` is raised when:

- the body is JSON with `error.type == "exceed_context_size_error"`; `n_ctx` and
  `n_prompt_tokens` are read from it [verified: the type string is in the installed build, and
  `hal-route.py` `anthropic_overflow()` parses these fields];
- the body is JSON with `error.code == "context_length_exceeded"` (the gateway's OpenAI-route
  rewrite, 05-gateway REQ-GW-40); `n_ctx` and the prompt count are parsed from "maximum
  context length is N tokens. However, your messages resulted in M tokens" when present;
- HTTP 400 with a body matching (case-insensitive)
  `context (length|size|window)|maximum context|too many tokens|prompt is too long|exceeds the available context`;
  for the gateway's Anthropic-route rewrite `prompt is too long: M tokens > N maximum`, `M`
  and `N` are read.

#### 3.3.7 Endpoint resolution

The agent reaches models only through the gateway (RESOLUTIONS G5). In box mode the endpoint
comes from `agent.json` (3.16.3): `unix:///run/hal/model.sock`, no token (the host forwarder
adds the session token; 02-sandbox "Model socket and forwarder"). Out of the box (backend
`none` started by halcore, the unboxed assistant fallback, or a standalone `hal-agent` run),
the endpoint and token follow the same rule as `halkit.llm` (00-overview "halkit.llm"):
`runtime_dir()/gateway.sock`, authenticated by peer uid (no token), or `[gateway].url`
with the client token of `[gateway].token_file` when set (laptop client mode, 05-gateway). There is no `[agent].endpoint` key
and no direct Ollama URL. When halcore starts the agent it always writes the endpoint into
`agent.json`, so only a standalone run uses this rule. No gateway reachable is a
configuration error that names `hal on` and `[gateway].url`.

### 3.4 Model profiles

#### 3.4.1 Schema (`model-profiles.toml`, schema 1)

The file name is `model-profiles.toml` in both places (shipped data file and
`~/.config/hal/model-profiles.toml`), because `~/.config/hal/models.toml` is the gateway's
model manifest (RESOLUTIONS D2, 05-gateway).

```toml
schema = 1                    # required; any other value fails loudly

[defaults]                    # applies to every model; every key below is allowed here
context = 32768
max_output = 8192
temperature = 0.6
top_p = 0.95
thinking = "on"
reasoning_replay = "tool_turns"
tool_format = "native"
leak_parsers = ["hermes", "json", "name_json"]
parallel_tool_calls = false
first_token_timeout_s = 600
system_role = "system"
max_tools_hint = 12

[[model]]
match = ["gemma4:*", "gemma-4-*"]   # required: globs on the model id, case-insensitive
family = "gemma4"                   # required: short name used in reports
# any [defaults] key may be overridden here
notes = "free text"
evidence = "free text: what was measured, when, how"
```

| Key | Type | Default | Meaning and reason |
| --- | --- | --- | --- |
| `context` | int | 32768 | Window Hal plans with; the gateway's `context_length` caps it (3.9.1). 32k ran end to end without overflow where 12k overflowed both tasks [verified locally, `understand/gemma-agent.md` section 2]. |
| `max_output` | int | 8192 | Output cap per request. 8,192 of 32,768 gave fewer overflows than 16k or 32k [relayed]. |
| `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `repeat_penalty` | float/int | vendor values per family | Low temperature caused "spinning" [relayed], so families keep their published defaults. |
| `thinking` | str | `"on"` | `on`, `off`, or an effort name. Thinking on solved 48 vs 23 of 129 tasks [relayed]. |
| `thinking_budget` | int | 0 | Anthropic-style budget tokens; 0 = server default. |
| `reasoning_replay` | str | `"tool_turns"` | 3.3.4. |
| `tool_format` | str | `"native"` | `native`: server parser; `prompt`: Hal describes tools in the system prompt and parses text (REQ-AGT-92); `auto`: `prompt` when the server says the model has no tool capability. |
| `leak_parsers` | list[str] | see above | Parsers tried on text, in order (3.6.2). |
| `parallel_tool_calls` | bool | false | Gemma made 0 multi-call responses in 2,872 steps [relayed, `dev-notes.md`]; asking for one call at a time keeps parsers simple. |
| `first_token_timeout_s` | int | 600 | 3.3.1. |
| `system_role` | str | `"system"` | `system`, `developer` or `user` (prepended to the first user message) for templates that reject a system role. |
| `strict_alternation` | bool | false | Merge consecutive same-role messages for templates that require alternation. |
| `max_tools_hint` | int | 12 | Above this many offered tools the loop moves extra tools behind `hal_find_tools`. Gemma 4 26B "cannot reliably produce" JSON calls with 10 or more tools [relayed]. |
| `stop` | list[str] | [] | Extra stop strings. |
| `extra_openai`, `extra_anthropic`, `extra_responses` | table | {} | Merged into the request body for that API, for example `chat_template_kwargs`. |
| `match`, `family`, `notes`, `evidence` | | | Entry metadata. |

Validation is strict: unknown keys, wrong types, `match` missing or empty, and values outside
the listed enums fail loudly with file path and key.

#### 3.4.2 Resolution

```python
@dataclass(frozen=True, slots=True)
class Profile:
    model: str
    family: str
    values: Mapping[str, Any]
    sources: Mapping[str, str]          # key -> "defaults" | "shipped:<family>" | "user:<family>"
                                        #        | "config" | "cli"

def resolve_profile(model: str, *, user_file: Path | None, overrides: Mapping[str, Any],
                    cli: Mapping[str, Any]) -> Profile: ...
```

Order (REQ-AGT-89): `[defaults]` of the shipped file, then `[defaults]` of the user file, then
the matching shipped entry, then the matching user entry, then `[agent.overrides]` (and
`[agent.assistant]` keys for the assistant, 3.17.3), then CLI flags. The gateway's `GET
/v1/models` entry for the model carries `context_length` and an optional `meta.hal_profile`
(05-gateway, RESOLUTIONS D5); when `meta.hal_profile` names a family, the entry with that
`family` is used instead of glob matching. This is how role aliases such as `hal-code`, whose
backend model is a local choice, get the right profile. In box mode halcore resolves the
profile on the host and writes the resolved values into `agent.json`, so the box never reads
a profile file.

#### 3.4.3 Seeded catalogue

Seeded from `ollama list`, `/api/show` and `~/.local/state/hal/eval/*.json` on this machine
[verified: `ollama list` and `POST /api/show` against Ollama 0.40.1 on 127.0.0.1:11434,
read-only, 2026-10-23; eval JSON files read 2026-10-23]. Sampling values are the ones Ollama
publishes in each model's parameters. `ctx max` is the model's trained maximum from
`model_info`; `context` stays at 32k unless measured otherwise, because VRAM, not the model,
sets the practical limit on a 24 GB card.

| Family | `match` | Caps (`/api/show`) | Sampling | `thinking` | `leak_parsers` | ctx max | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `gemma4` | `gemma4:*`, `gemma-4-*` | tools, thinking (12b, 26b) | T 1.0, top_k 64, top_p 0.95 | on | `gemma4`, `json`, `name_json` | 262144 | assistant eval 2026-10-10: 12b-it-qat 33/38 and 74/76, 26b-a4b 32/38 (single runs, think off) |
| `qwen35` | `qwen3.5:*`, `qwen3.6:*`, `qwen3.8:*`, `*qwen3.8*`, `*qwen3.5*` | tools, thinking | qwen3.5: T 1.0, top_k 20, top_p 0.95, presence_penalty 1.5; qwen3.6 coding: T 0.6, top_k 20, top_p 0.95 | on | `hermes`, `qwen_xml`, `json` | 262144 | 9b 33/38 and 73/76, 4b 31/38 (2026-10-10, single runs) |
| `qwen3coder` | `qwen3-coder*` | tools | T 0.7 [unverified: not in Ollama params] | off | `qwen_xml`, `hermes` | n/a | Ollama parser `qwen3-coder` |
| `gptoss` | `gpt-oss:*`, `gpt-oss-*` | tools, thinking | T 1.0 | `"medium"` | `harmony`, `json` | 131072 | none in Hal yet |
| `mistral3` | `ministral-3:*`, `mistral-small3.2:*`, `devstral-small-2:*`, `magistral:*` | tools (no thinking for ministral, mistral-small, devstral) | T 0.15 | off (magistral: on [unverified]) | `mistral`, `json` | 131072 to 393216 | ministral-3:14b 28/38, mistral-small3.2:24b 30/38 (2026-10-10) |
| `glm47` | `glm-4.7*` | n/a | vendor default | on | `glm`, `hermes` | n/a | Ollama parser `glm-4.7` |
| `granite` | `granite4*` | n/a | vendor default | off | `hermes`, `json` | n/a | none |
| `small` | `phi4-mini*`, `llama3.2*` | n/a | vendor default | off | `json`, `name_json` | n/a | none; `context` 8192 |

Ollama declares a built-in renderer and parser for `gemma4`, `qwen3.5` (also used by
`qwen3.6:27b-coding-mtp`), `qwen3-coder` and `glm-4.7` [verified: `RENDERER`/`PARSER` lines in
`/api/show` `modelfile`]. Mistral-family templates emit `[TOOL_CALLS]name[ARGS]{arguments}`
[verified: `ministral-3:14b` and `devstral-small-2:24b` templates]. The assistant numbers were
measured with `think: false` at temperature 0.4 and `num_ctx` 8192, single runs, so they carry
the noise discussed in 3.18.5.

The catalogue was seeded by reading Ollama directly, read-only, on 2026-10-23; at run time
the agent never calls Ollama and learns about a model only from the gateway. Hal role models
(`hal-coder`, `hal-writer`, `hal-fast`, built by `models/build-roles.sh`) and the gateway role
aliases are matched through `meta.hal_profile`, or in the user's `model-profiles.toml`, not
the shipped file, because their base model is a local choice.

### 3.5 The agent loop

#### 3.5.1 Interface

```python
PermissionFn = Callable[[ToolCall, ToolSpec],
                        Literal["allow_once", "allow_always", "reject_once", "reject_always",
                                "cancelled"]]


@dataclass(slots=True)
class Hooks:
    """Extension points the assistant (and tests) use; all optional."""
    gate: TextGate | None = None                      # holds back visible text (claims check)
    after_round: Callable[[RoundInfo], RoundDecision] | None = None
    before_tool: Callable[[ToolCall, ToolSpec], ToolCall | ToolResult | None] | None = None
    # before_tool is for tests and argument normalisation; it is never an approval path


class Agent:
    """One conversation: history, tools, budgets, and the turn loop."""

    def __init__(self, settings: AgentSettings, provider: Provider, tools: ToolRegistry,
                 *, recorder: Recorder | None = None, hooks: Hooks | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None: ...

    @property
    def session_id(self) -> str: ...
    @property
    def messages(self) -> list[Message]: ...

    def run_turn(self, prompt: str, *, mode: Literal["task", "interactive"] = "interactive",
                 cancel: threading.Event | None = None,
                 permission: PermissionFn | None = None) -> Iterator[Event]:
        """Runs one user turn to completion; yields events; never raises for model or tool
        errors (REQ-AGT-21)."""

    def replay(self, records: Iterable[dict[str, Any]]) -> Iterator[Event]:
        """Rebuilds history from transcript records (resume) and yields the events a client
        needs to redraw it."""
```

Supporting types:

```python
class Recorder(Protocol):
    """Transcript sink: SessionWriter out of the box, the ACP _hal/record emitter in the box."""
    def record(self, rec: dict[str, Any]) -> None: ...


class TextGate(Protocol):
    """Decides when streamed text becomes visible (the assistant's claims check)."""
    def feed(self, delta: str) -> str: ...      # text to release now; "" while holding
    def held(self) -> str: ...                  # text still held at the end of a round
    def release(self) -> str: ...               # releases and returns the held text
    def reset(self) -> None: ...                # new round


@dataclass(frozen=True, slots=True)
class RoundInfo:
    completion: Completion
    calls: Sequence[ToolCall]                   # after repair
    held_text: str                              # from the gate, "" without a gate
    tools_run: int                              # tool calls run so far in this turn
    retried: bool                               # a retry already happened in this turn


@dataclass(frozen=True, slots=True)
class RoundDecision:
    action: Literal["accept", "retry"]
    system_message: str = ""                    # retry: replaces the reply with this message
    replace_text: str | None = None             # accept: release this text instead of held_text
```

Without hooks every round is accepted and text is released as it streams. With a gate, the
loop calls `after_round` when a round ends with no tool call; `retry` drops the held reply from
history, appends `system_message` as a system message and runs another round (counted against
`max_model_requests`).

`ToolRegistry` holds the `Tool`s and decides which `ToolSpec`s are offered on each request:

```python
class ToolRegistry:
    def __init__(self, tools: Iterable[Tool], *, search: Tool | None = None,
                 pinned: Iterable[str] = (), max_tools_hint: int = 12) -> None: ...
    def start_turn(self, user_text: str) -> None: ...   # resets tools found in the last turn
    def offered(self) -> list[ToolSpec]: ...            # specs for the next request
    def add_found(self, names: Iterable[str]) -> None: ...   # after hal_find_tools
    def get(self, name: str) -> Tool | None: ...         # offered or found this turn only
```

#### 3.5.2 One turn

```text
turn_start
loop:
  check budgets (model requests, wall time)          -> turn_end on exhaustion
  build ChatRequest (system prompt, history, offered tools, profile)
  context.fit(request)                               -> compaction events; overflow -> turn_end
  provider.stream(request)                           -> text / reasoning events
      transient error: retried inside the transport
      overflow error: compact once and retry (REQ-AGT-63)
      other error: turn_end(error)
  usage event
  repair_completion(completion, offered)             -> notice events, repaired calls
  hooks.after_round                                  -> may replace the reply (claims check)
  if calls:
      for call in calls[:max_calls_per_response]:
          watchdog check                             -> loop error result, or turn_end(loop)
          hooks.before_tool                          -> may substitute a result (tests)
          review-mode permission                     -> permission event, maybe denied
          tool_call, tool_start, run, tool_result events
          finish tool -> turn_end(finished)
      budget checks on tool calls                    -> turn_end(max_tool_calls)
      continue
  no calls:
      nudge cases (3.5.5)                            -> append nudge, continue, or nudge_limit
      else turn_end(end_turn)
```

History stays valid at every exit: an assistant message with tool calls is always followed by
one tool message per call (REQ-AGT-15). On cancel or error between a call and its result, the
loop appends a synthetic result `{"status":"error","error_type":"cancelled","message":"The
call was cancelled before it finished."}`.

#### 3.5.3 Multiple calls per response

A response may contain several calls. They run one at a time in emitted order, never
concurrently, because concurrent edits and commands on one working tree have no safe order and
small models rarely emit several calls (Gemma: 0 multi-call responses in 2,872 steps,
relayed). `finish` runs after the other calls of its response. Calls past
`max_calls_per_response` (8) get `invalid_args` "Too many tool calls in one reply; make at
most 8." `parallel_tool_calls` is sent as the profile says (default false).

#### 3.5.4 Budgets

```python
@dataclass(frozen=True, slots=True)
class Budget:
    max_tool_calls: int
    max_model_requests: int
    max_wall_s: float
    warn_calls: int = 10                # 10 of 28 in the gemma harness (F-TOOL-08)
    warn_seconds: float = 90.0          # the gemma prompt's "submit when <90 s remain"
    max_calls_per_response: int = 8
    max_consecutive_nudges: int = 3     # the gemma harness ends a task after 3 nudges
    loop_repeat_limit: int = 3          # third identical call refused
    max_consecutive_tool_errors: int = 6
```

| Setting | Task mode default | Interactive default | Reason |
| --- | --- | --- | --- |
| `max_tool_calls` | 50 | 100 | About 50 calls beat 25 (50 vs 36 of 129, relayed); interactive turns are user-steered and longer. |
| `max_model_requests` | 120 | 200 | Repairs and nudges cost requests but not tool calls (gemma: 31.2 model calls per 25.4 tool calls, verified run). |
| `max_wall_s` | 900 | 1800 | 15-minute cap with 50 calls (relayed); interactive users can cancel. |

`BudgetView` (passed to tools and prompts) exposes `calls_used`, `calls_left`,
`seconds_left` and `warning: bool`. The warning line (REQ-AGT-17):

```text
[hal] budget_warning: 9 tool calls and 4 min left. Start no new exploration: finish the change you have.
```

#### 3.5.5 Nudges

Fixed texts in `prompts/nudges.toml`, sent as a user message prefixed `[hal]`:

| Case | Text |
| --- | --- |
| `empty` | `[hal] Your last reply had no text and no tool call. Continue: call one of your tools, or give your answer.` |
| `truncated` | `[hal] Your last tool call was cut off at the output limit and did not run. Make it smaller: change one small part at a time with edit, and write a long new file in parts.` |
| `malformed` | `[hal] Your last tool call could not be read ({reason}) and did not run. Call {tool} again with a JSON object of arguments: {params}.` |
| `finish` (task mode only) | `[hal] You replied without a tool call. If the task is done, call finish with a summary. Otherwise continue with a tool call.` |

Consecutive nudges are counted; any executed tool call resets the count. The third
consecutive nudge is not sent: the turn ends with `nudge_limit` (REQ-AGT-19). Unknown tool
names produce an error result, not a nudge, because the model needs the list of tools.

#### 3.5.6 Stop reasons

`end_turn`, `finished`, `max_tool_calls`, `max_model_requests`, `max_time`, `max_tokens`
(text cut at the output limit with no call), `refusal` (the server refused or filtered the
reply), `loop`, `nudge_limit`, `tool_errors`, `context_overflow`, `cancelled`, `error`.

#### 3.5.7 Turn statistics

`turn_end.data.stats`: `model_requests`, `tool_calls`, `repaired_calls`, `malformed_calls`,
`unknown_tools`, `truncated_calls`, `nudges`, `compactions`, `overflows`, `prompt_tokens`
(sum), `completion_tokens` (sum), `peak_prompt_tokens`, `wall_s`, `first_edit_call` (index of
the first successful `edit` or `write`, or null), `loop_refusals`, `denied_calls`. These are
the per-run metrics the gemma harness logged (`understand/gemma-agent.md` 3.9).

#### 3.5.8 Watchdog

Canonical arguments: `json.dumps(arguments, sort_keys=True, separators=(",", ":"))`. The
watchdog keeps the last call key `(name, canonical)` and a repeat count. On the third
consecutive identical call it returns, without running:

```json
{"status":"error","error_type":"loop","message":"You have made this exact call 3 times in a row (run {\"command\":\"pytest -x\"}). It did not run again: the result will not change. Try something different: read the code again, or change the command."}
```

A fourth identical call ends the turn with `loop`. Six consecutive error results end it with
`tool_errors`. Repeated identical calls were the dominant failure in public Gemma runs (one
command up to 69 times; relayed), and once a loop starts prompt rules stop binding.

#### 3.5.9 Cancellation

`cancel` is a `threading.Event`. The provider aborts its socket (3.3.2); `run` kills its
process group (3.7.7); MCP and broker calls send `notifications/cancelled` and stop waiting;
the loop appends synthetic results and ends with `cancelled` (REQ-AGT-20).

#### 3.5.10 Review mode and permission

`settings.permissions` is `auto` (default) or `review`. In `auto`, in-box tools run without
prompts: the box is the permission boundary (brief). In `review`, calls whose `ToolSpec.kind`
is `edit`, `delete`, `move` or `execute` call `permission(call, spec)` first. In ACP mode that
function sends `session/request_permission` with options `allow-once`, `allow-always`,
`reject-once` (kinds `allow_once`, `allow_always`, `reject_once`); ACP's `allow_always` kind
here means "for this session" and covers in-box actions only. It is unrelated to broker
approval scopes, which offer only `once` and `session` (RESOLUTIONS B2). Review mode is a
usability feature for watching the agent work; it is not a security boundary, because
everything it guards is already inside the box. Host exits are guarded by the broker's own approval
request, which the host renders (01-security "Approval surfaces and protocol"); review mode
never sees it.

### 3.6 Tool-call repair layer

#### 3.6.1 Pipeline

```python
@dataclass(slots=True)
class RepairResult:
    calls: list[ToolCall]
    visible_text: str                   # content with leaked call text removed
    problems: list[RepairProblem]       # unreadable calls -> malformed nudge or error result
    truncated: bool
    notes: list[str]                    # for notice events


def repair_completion(completion: Completion, offered: Mapping[str, ToolSpec],
                      profile: Profile) -> RepairResult: ...
```

1. Native calls: for each call in `completion.message.tool_calls`, resolve the name (3.6.4)
   and parse the arguments (3.6.5).
2. If there are no native calls, run the profile's `leak_parsers` in order over the visible
   content (never over reasoning). The first parser that yields at least one call whose name
   resolves to an offered tool wins; its matched text is cut from the content.
3. Truncation: if `finish == "length"` and the content contains an unclosed call marker, or a
   native call's arguments do not parse, mark `truncated` and drop those calls.
4. Strip `_confirmed`, `confirmed`, `confirm` from every argument dict (REQ-AGT-35).
5. Record every applied repair on `ToolCall.repair`.

The repair layer only ever produces calls to offered tools; leaked calls then pass through
exactly the same policy, permission and broker checks as native ones, so repair never widens
what the model can do.

#### 3.6.2 Leak parsers

| Parser | Shape (regex sketch; `re.S`) | Source |
| --- | --- | --- |
| `gemma4` | `<\|tool_call>call:(?P<name>[\w.\-]+)\{(?P<args>.*?)\}<tool_call\|>`; args use bare keys and `<\|"\|>` as the string quote: `key:<\|"\|>text<\|"\|>,n:3,flag:true,obj:{k:<\|"\|>v<\|"\|>}` | [verified: local Gemma 4 `chat_template.jinja` `format_argument` and lines 248-264] |
| `hermes` | `<tool_call>\s*(\{.*?\})\s*</tool_call>` with `{"name": ..., "arguments": {...}}` | Qwen 2.5/3 and Hermes templates [unverified here: qwen3.5 uses Ollama's built-in renderer, template text not visible] |
| `qwen_xml` | `<tool_call>\s*<function=(?P<name>[\w.\-]+)>(?P<body>.*?)</function>\s*</tool_call>` with `<parameter=(?P<k>\w+)>\n?(?P<v>.*?)\n?</parameter>` pairs | Qwen3-Coder format [unverified here: Ollama's `qwen3-coder` parser hides the template] |
| `mistral` | `\[TOOL_CALLS\](?P<name>[\w.\-]+)\[ARGS\](?P<args>\{.*\})` repeated; also the older `\[TOOL_CALLS\]\s*(\[.*\])` list of `{name, arguments}` | [verified: `ministral-3:14b` template] |
| `harmony` | `(?:<\|start\|>assistant)?<\|channel\|>commentary to=(?:functions\.)?(?P<name>[\w.\-]+)\s*(?:<\|constrain\|>json)?<\|message\|>(?P<args>\{.*?\})(?:<\|call\|>\|$)` | [verified: `gpt-oss:20b` template] |
| `glm` | `<tool_call>(?P<name>[\w.\-]+)\s*(?:<arg_key>(?P<k>.*?)</arg_key>\s*<arg_value>(?P<v>.*?)</arg_value>\s*)*</tool_call>` | GLM 4.x format [unverified here: Ollama `glm-4.7` parser hides the template] |
| `name_json` | `^\s*(?:\[TOOL_CALLS\])?\s*(?P<name>[a-z_][\w.\-]*)\s*(?P<args>\{.*\})\s*$` (whole content) | halcore `LEAKED` regex (`brain.py:97`) |
| `json` | the whole content (after stripping a code fence) is one JSON object, or a list of objects, with `name` and `arguments` or `parameters` | Llama 3 style |

Argument bodies use the matching value parser: JSON for `hermes`, `mistral`, `harmony`, `json`,
`name_json`; the Gemma value grammar for `gemma4` (a small recursive-descent parser over
`<|"|>`-quoted strings, numbers, `true`, `false`, `null`, `{...}`, `[...]`, bare keys); raw
strings for `qwen_xml` and `glm` values, coerced by schema (3.6.5). Parsers are linear-time
scanners or regexes without nested quantifiers, and every parser input is capped at 200,000
characters (REQ-AGT-33). The vLLM gemma4 parser hung on malformed arrays in 85 of 60,000 fuzzed
strings (relayed), which is the reason for the fuzz requirement.

#### 3.6.3 Truncation markers

Open markers that make a `length` stop a truncated call: `<|tool_call>` without
`<tool_call|>`; `<tool_call>` without `</tool_call>`; `[TOOL_CALLS]` followed by unbalanced
braces; `to=functions.` without `<|call|>` and with unbalanced braces; a native call whose
argument string fails every parse in 3.6.5.

#### 3.6.4 Names and aliases

Resolution order: exact; case-insensitive; strip one prefix of `functions.`, `default_api.`,
`tools.`, `tool.`, `mcp.`; alias table. Aliases apply only when the target is offered.

| Target | Aliases |
| --- | --- |
| `run` | `bash`, `shell`, `sh`, `run_command`, `run_shell_command`, `execute`, `execute_command`, `exec`, `exec_shell_command`, `terminal`, `cmd`, `command`, `shell_exec`, `container.exec` |
| `read` | `read_file`, `view`, `view_file`, `open`, `open_file`, `cat`, `file_read`, `readfile`, `get_file` |
| `edit` | `str_replace`, `str_replace_editor`, `str_replace_based_edit_tool`, `edit_file`, `replace`, `replace_in_file`, `search_replace`, `apply_edit`, `file_edit`, `multiedit` |
| `write` | `write_file`, `create_file`, `create`, `save_file`, `file_write` |
| `grep` | `search`, `search_code`, `grep_search`, `rg`, `ripgrep`, `find_in_files`, `code_search`, `git_grep` |
| `glob` | `find_files`, `list_files`, `ls`, `list_dir`, `list_directory`, `file_glob_search`, `find` |
| `finish` | `submit`, `submit_patch`, `done`, `complete`, `task_complete`, `attempt_completion`, `final_answer` |
| `todo` | `update_plan`, `plan`, `todo_write`, `todowrite`, `set_plan` |
| `hal_find_tools` | `find_tools`, `search_tools`, `tool_search`, `hal_search` |

`str_replace_editor` with `command` other than `str_replace` maps by `command`: `view` ->
`read`, `create` -> `write`. An unresolved name returns:

```json
{"status":"error","error_type":"unknown_tool","message":"There is no tool named 'apply_patch'. Your tools are: read, grep, glob, edit, write, run, todo, finish."}
```

#### 3.6.5 Arguments

Parsing a string of arguments, in order, stopping at the first success that yields a dict:
`json.loads`; JSON after removing trailing commas before `}` or `]`; `ast.literal_eval` (Python
literals with single quotes, `True`, `None`); one more `json.loads` if the result is itself a
string (double encoding). An empty string is `{}`.

Argument-name aliases per tool (applied when the canonical name is absent):

| Tool | Aliases -> canonical |
| --- | --- |
| `read` | `file_path`, `filepath`, `filename`, `file` -> `path`; `offset`, `start`, `line_start`, `from_line`, `line` -> `start_line`; `end`, `line_end`, `to_line` -> `end_line`; `limit`, `lines`, `count` -> `end_line = start_line + value - 1` |
| `edit` | `file_path`, `file` -> `path`; `old_str`, `old`, `search`, `find`, `old_text`, `target` -> `old_string`; `new_str`, `new`, `replace`, `replacement`, `new_text` -> `new_string` |
| `write` | `file_path`, `file` -> `path`; `file_text`, `text`, `contents`, `data`, `file_content`, `code` -> `content` |
| `run` | `cmd`, `script`, `code`, `bash_command`, `shell_command`, `commands` -> `command`; `timeout_s`, `timeout_seconds` -> `timeout` |
| `grep` | `query`, `regex`, `search`, `text`, `pattern_text` -> `pattern`; `include`, `file_pattern`, `files`, `glob_pattern` -> `glob`; `dir`, `directory`, `root`, `cwd` -> `path` |
| `glob` | `glob`, `query`, `name`, `file_pattern` -> `pattern`; `dir`, `directory`, `root` -> `path` |
| `todo` | `todos`, `items`, `steps`, `list` -> `plan` |
| `finish` | `message`, `result`, `answer`, `text` -> `summary` |

Coercion against the schema type:

| Schema type | Given | Result |
| --- | --- | --- |
| `string` | number or bool | `str(value)` (`true`/`false` lower case) |
| `string` | list of strings | joined: `run.command` with `" && "` when every item looks like a whole command, else `shlex.join`; `todo.plan` with newlines (each item prefixed `- [ ] ` when it has no checkbox); others with newlines |
| `string` | dict | `json.dumps(value)` |
| `integer` | string of digits (optionally signed) | `int(value)` |
| `integer` | float with no fractional part | `int(value)` |
| `integer` | bool | `int(value)` |
| any | `null` | argument dropped |

Unknown argument names left after aliasing are dropped and reported in the result's steering
text (`[hal] ignored unknown argument 'foo'`). A missing required argument gives
`invalid_args`: `"edit needs path, old_string and new_string; got path."` Host tools from the
broker use the same rules with the broker's schemas. The agent is the only place where
near-miss names are repaired: the broker applies only the `aliases` a manifest declares and
logs nothing else as a repair (01-security REQ-SEC-05, RESOLUTIONS B7), so a call that reaches
the broker already has its final argument names, and the agent coerces only to the JSON types
the schema names.

### 3.7 Coding toolset

#### 3.7.1 Common contract

```python
class Tool(Protocol):
    spec: ToolSpec
    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: Workspace
    cancel: threading.Event
    budget: BudgetView
    deadline: float                     # monotonic seconds; tools stop before it


@dataclass(frozen=True, slots=True)
class Location:
    path: str
    line: int | None = None


@dataclass(frozen=True, slots=True)
class FileDiff:
    path: str
    old_text: str | None                # None for a new file
    new_text: str
    unified: str                        # capped unified diff for the model


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    text: str                           # body when ok, message when not
    error_type: str = ""                # see the list below
    title: str = ""
    locations: tuple[Location, ...] = ()
    diff: FileDiff | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    untrusted: str = ""                 # tool name for the 01 envelope (user MCP results only)


def render_for_model(result: ToolResult, steering: Sequence[str], cap: int) -> str: ...
```

`error_type` values: `not_found`, `ambiguous`, `invalid_args`, `unknown_tool`, `protected`,
`outside_workspace`, `binary`, `too_large`, `syntax`, `timeout`, `denied`, `unavailable`,
`loop`, `cancelled`, `internal`.

`render_for_model`: success -> `text`; error -> `json.dumps({"status": "error", "error_type":
..., "message": ...})`; then the steering lines, each on its own line, at the end, because the
tail of a tool result is the part every layer keeps (relayed, `dev-notes.md` lesson 4). The
whole string is cut to `max_result_chars` (12,000) keeping the first 4,000 and last 8,000
characters with a `[... N characters cut ...]` line (REQ-AGT-45).

`Workspace`:

```python
@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path                          # /work in the box
    scratch: Path                       # /tmp
    readonly: tuple[Path, ...]          # skills dirs, /run/hal/user
    protect: tuple[str, ...]            # globs relative to root; default (".git/**", ".git")
    confined: bool                      # True with sandbox "none": path rules are the only guard

    def resolve(self, path: str, *, write: bool) -> Path:
        """Returns the real path; raises ToolFailure(outside_workspace | protected)."""

    def display(self, path: Path) -> str:
        """Returns the path relative to root when inside it, else absolute."""
```

Relative paths resolve against `root`. `realpath` is applied before the containment check, so
a symlink out of the workspace is refused. Writes into `readonly` dirs or `protect` globs are
refused. In the box `.git` is also mounted read-only (brief); `protect` makes the refusal
explicit and covers the no-sandbox mode. Protected-path checks bind file tools only; a `run`
command can still change files the mounts allow, which is why the eval grader resets protected
paths itself (3.18.3) and why `.git` is a read-only mount.

With `sandbox = "none"` (`confined = True`), file tools open paths with
`halkit.fsafe.open_beneath` (02-sandbox "Backend none"; one implementation, RESOLUTIONS C10);
`resolve` only computes the display path and the `readonly`/`protect` checks and has no path
walker of its own. The shared vectors are
`packages/hal-kit/tests/data/path_cases.json`. Files that 02-sandbox
masks in the box (`[sandbox].mask`, for example `.env`; RESOLUTIONS "Masking") appear to the
file tools as empty read-only files; the tools treat them like any read-only path.

#### 3.7.2 Schemas (exact)

Offered in this order. All are flat (REQ-AGT-36).

```json
[
  {"name": "read", "kind": "read",
   "description": "Read part of a text file. Returns numbered lines, at most 200 lines or 12000 characters per call. Give start_line and end_line to read the part you need.",
   "parameters": {"type": "object", "properties": {
     "path": {"type": "string", "description": "File path, relative to the project root"},
     "start_line": {"type": "integer", "description": "First line to show, from 1 (default 1)"},
     "end_line": {"type": "integer", "description": "Last line to show (default start_line + 199)"}},
     "required": ["path"]}},
  {"name": "grep", "kind": "search",
   "description": "Search file contents with a regular expression (ripgrep syntax). Returns path:line: text for each match, at most 100 matches.",
   "parameters": {"type": "object", "properties": {
     "pattern": {"type": "string", "description": "Regular expression or exact text to find"},
     "path": {"type": "string", "description": "File or directory to search (default: the project root)"},
     "glob": {"type": "string", "description": "Only search files matching this glob, for example *.py"}},
     "required": ["pattern"]}},
  {"name": "glob", "kind": "search",
   "description": "List files whose path matches a glob pattern, for example src/**/*.py. Returns at most 200 paths.",
   "parameters": {"type": "object", "properties": {
     "pattern": {"type": "string", "description": "Glob pattern"},
     "path": {"type": "string", "description": "Directory to search (default: the project root)"}},
     "required": ["pattern"]}},
  {"name": "edit", "kind": "edit",
   "description": "Replace one exact piece of text in a file. old_string must appear exactly once; copy it from read output without the line numbers. Returns a short diff.",
   "parameters": {"type": "object", "properties": {
     "path": {"type": "string", "description": "File to change"},
     "old_string": {"type": "string", "description": "Exact text to replace, unique in the file"},
     "new_string": {"type": "string", "description": "Replacement text"}},
     "required": ["path", "old_string", "new_string"]}},
  {"name": "write", "kind": "edit",
   "description": "Create a file, or replace a whole file, with the given content. Use edit to change part of an existing file.",
   "parameters": {"type": "object", "properties": {
     "path": {"type": "string", "description": "File to write"},
     "content": {"type": "string", "description": "The complete file content"}},
     "required": ["path", "content"]}},
  {"name": "run", "kind": "execute",
   "description": "Run a bash command in the project directory, offline. Returns the exit code and the output (the first 2000 and the last 6000 characters). Default timeout 120 seconds.",
   "parameters": {"type": "object", "properties": {
     "command": {"type": "string", "description": "The bash command to run"},
     "timeout": {"type": "integer", "description": "Seconds before the command is stopped (default 120, at most 600)"}},
     "required": ["command"]}},
  {"name": "todo", "kind": "think",
   "description": "Record your plan as a checklist, one step per line: '- [ ] step' to do, '- [~] step' in progress, '- [x] step' done. Send the whole list each time. Free: does not use a tool call.",
   "parameters": {"type": "object", "properties": {
     "plan": {"type": "string", "description": "The whole checklist"}},
     "required": ["plan"]}},
  {"name": "finish", "kind": "other",
   "description": "End the task. Give a two-sentence summary of what you changed and how you checked it. Free: does not use a tool call.",
   "parameters": {"type": "object", "properties": {
     "summary": {"type": "string", "description": "What you changed and how you checked it"}},
     "required": ["summary"]}}
]
```

(`kind` is Hal metadata for ACP; it is not sent to the model.) Interactive mode offers the
first seven; task mode offers all eight. Without a sandbox backend `run` is left out
(REQ-AGT-44). The `assistant` toolset offers none of them (REQ-AGT-102).

#### 3.7.3 `read`

- `start_line` defaults to 1, `end_line` to `start_line + 199`; `end_line - start_line + 1` is
  cut to 200. `start_line` past the end gives `not_found` "The file has 120 lines."
- Binary: a NUL byte in the first 8,192 bytes gives `binary`. Text decodes as UTF-8 with
  `errors="replace"`.
- Lines render as `f"{n:>6}\t{line}"`; a line longer than 500 characters is cut with
  `[... line cut ...]`. Output stops at 12,000 characters.
- Footer when more remains: `[hal] lines 1-200 of 950. Continue with start_line=201.`
- Title `Read src/app.py:1-200`; location `(path, start_line)`.

#### 3.7.4 `grep` and `glob`

`grep` runs `rg --line-number --no-heading --color never --max-columns 300
--max-columns-preview --max-count 20 --max-filesize 1M --regexp PATTERN [--glob GLOB] --
PATH` with a 20 s timeout. Exit 2 with a regex parse error retries once with
`--fixed-strings` and adds `[hal] the pattern is not a valid regular expression; searched for
the exact text instead.` Output: header `23 matches in 5 files` then `path:line: text`, at
most 100 matches and 8,000 characters. No match is a success: `No matches for 'x' in src.
Try a shorter or different identifier.` (an error here would invite retries). Without `rg`
on `PATH`, a Python walk with `re` gives the same format and skips `.git`, `node_modules`,
`.venv`, `__pycache__` and files over 1 MiB.

`glob` runs `rg --files --glob PATTERN -- PATH` (respects `.gitignore`), sorts the paths, and
returns at most 200 with a header `37 files`. Python fallback: `Path.glob` with the same skip
list.

#### 3.7.5 `edit`

Tiers, in order; each tier must find exactly one match:

1. exact substring;
2. line numbers stripped: when every non-empty line of `old_string` starts with a `read`-style
   prefix (`^\s*\d+\t` or `^\s*\d+[:|] ?`), strip it from `old_string` and from `new_string`;
3. whitespace-flexible: compare line windows with each line's leading and trailing whitespace
   removed; on a unique match, re-indent `new_string` by the indentation difference between
   the first line of `old_string` and the first matched line;
4. unescaped: when `old_string` has no newline but contains `\n`, decode `\n`, `\t`, `\"`
   and `\\` in both strings and retry tiers 1 to 3 (fix for double-encoded arguments,
   relayed).

Errors: more than one match at a tier gives `ambiguous` with the first 5 line numbers and "add
more surrounding lines to old_string"; no match at any tier gives `not_found` with the closest
window (difflib ratio over line windows of the same length, shown as up to 12 numbered lines)
and "read the lines again and copy them exactly"; `old_string == new_string` or empty
`old_string` gives `invalid_args`.

Syntax guard (`[agent.tools].syntax_guard`, default true): for `.py` (`compile(text, path,
"exec")`), `.json` (`json.loads`) and `.toml` (`tomllib.loads`), when the file parsed before
the edit and fails after, the edit is not written and the result is `syntax` with the parser's
line and message. tk's `edit` refuses syntax-breaking edits for the same reason (relayed,
`dev-notes.md`).

Success: `Edited src/app.py (tier: whitespace).` followed by a unified diff with 3 context
lines, capped at 40 lines and 3,000 characters. Writes are atomic (temp file in the same
directory, `os.replace`), keeping the file mode.

#### 3.7.6 `write`

Creates parent directories. Refuses a directory path (`invalid_args`), content over 200,000
characters (`too_large`: "write the file in parts: write the first part, then add the rest
with edit"), protected and read-only paths. Atomic write. Result: `Created src/new.py (42
lines).` or `Replaced src/app.py (120 lines, was 118).` For `.py` a syntax error is reported
as a warning line, not a refusal, because a new file may be written in parts.

#### 3.7.7 `run`

```python
subprocess.Popen(["bash", "-c", command], cwd=workspace.root, stdin=subprocess.DEVNULL,
                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                 close_fds=True, env=clean_env)
```

- `clean_env`: the box environment that 02-sandbox sets (already an allowlist, "Environment";
  it carries for example `HOME` on the box tmpfs and the dependency-prep paths such as
  `UV_PROJECT_ENVIRONMENT=/work/.hal/venv`, RESOLUTIONS F3 architect) minus every variable
  that starts with `HAL_`, plus `LANG=C.UTF-8`, `TERM=dumb`, `PYTHONDONTWRITEBYTECODE=1`,
  `PAGER=cat`, `GIT_PAGER=cat`, `NO_COLOR=1`, `CI=1`. Inheriting 02's set keeps a venv built
  by `hal code --deps` usable by `run`; dropping `HAL_` keeps socket paths and settings out of
  model-run commands.
- `timeout` default 120, clamped to 1..600, and to the seconds left in the budget. On timeout
  or cancel: `os.killpg(pgid, SIGTERM)`, then `SIGKILL` after 5 s.
- Output is read with `selectors` into a head buffer (first 2,000 characters) and a ring
  buffer (last 6,000), so memory stays bounded whatever the command prints. ANSI escape
  sequences are removed; `\r`-overwritten progress lines collapse to their last version.
- Result text:

```text
exit code 1 after 3.2 s
[output: 14512 characters; the first 2000 and the last 6000 are shown]
...head...
[... 6512 characters cut ...]
...tail...
```

  On timeout the first line is `stopped after 120 s (timeout); partial output below`. A
  non-zero exit is `ok = True` with `data.exit_code` set: a failing test is information, not a
  tool error, and treating it as an error triggers retries. Keeping both ends keeps the pytest
  summary that a first-5,000-characters cut lost (relayed, `gemma-agent.md` 3.2).
- Title `Run: <command, first 80 characters>`.

#### 3.7.8 `todo` and `finish`

`todo` parses lines `^\s*(?:[-*]|\d+[.)])\s*\[( |x|X|~|>)\]\s*(.+)$` (`x` completed, `~` or
`>` in progress, space pending); a line without a checkbox is a pending item. At most 20 items
of at most 200 characters; more gives `invalid_args`. It emits a `plan` event (priority
`medium` for every item) and returns the normalised list. It does not count toward
`max_tool_calls`.

`finish` records the summary as the turn's final text and ends the turn with `finished`. It
does not count toward `max_tool_calls`.

#### 3.7.9 Process hardening in the box

At start in box mode the agent calls `prctl(PR_SET_DUMPABLE, 0)` through `ctypes` (stdlib),
so a process started by `run` under the same uid cannot `ptrace` the agent or open
`/proc/<pid>/fd/*` to inject ACP messages (REQ-AGT-49). Spoofed ACP output could still only
affect things inside the box, because host exits go through the broker and host-side
permission decisions do not trust in-box text (3.14.6).

### 3.8 Assistant toolset

#### 3.8.1 Where the assistant runs

The assistant is the same agent started with `--toolset assistant`. There is no Python API
for it and no `halcore.brain.Brain` shim (RESOLUTIONS A1):

- Each channel (`hal chat`, voice, Telegram, the console) gets a long-lived *assistant box*,
  reached only through `hal acp --channel <name> --toolset assistant` (00-overview Flow 2 to
  4; launch owned by 06-core-cli). The box has a scratch `/work`
  (`$STATE/assistant/<channel>/work`), never the vault. The vault is reachable only through
  hal-memory tools on the broker.
- hal-voice and the Telegram pack are ACP clients of that command (RESOLUTIONS A1, B9, E1).
  They import nothing from Hal. Their approvals arrive as host-originated
  `session/request_permission` requests (06-core-cli "Approvals").
- When no container backend is available, halcore runs the agent in the `hal` process with
  `halagent.acp.serve(..., toolset="assistant")` (3.14.8), the same broker and policy, and a
  visible "unboxed" banner (06-core-cli; residual risk R-new-9 in 01-security). The agent's
  behaviour is identical apart from `sandbox = "none"`.

The assistant toolset (REQ-AGT-102): broker tools only, selected per turn (3.8.3), plus
`hal_find_tools`. No coding tool, no user MCP server in the box (an assistant box has no
project to run one against); `host = true` MCP servers are broker tools like any other.

#### 3.8.2 Assistant turn

`build_assistant(settings, provider, backend) -> Agent` configures an `Agent` for the channel:

- provider: from `agent.json` (3.3.7), api `openai` through the gateway. halcore fills
  `agent.json` from `[agent.assistant]` (3.17.3): `model` (default the role alias `hal-chat`,
  RESOLUTIONS D1), and profile values with thinking off (`think = false`, as today: voice
  latency), temperature 0.4 and a planning window of 8192 (`context`, the measured baseline),
  capped by the gateway's `context_length`;
- budgets: `max_model_requests = max_tool_rounds + 1` (5 + 1: the claims retry),
  `max_tool_calls = max_tool_rounds * 2`, `max_wall_s = 300`;
- context: history trimmed as today to the last `history_turns` (10) user turns and the last
  12 messages of each turn; the window check of 3.9 still runs before each request;
- hooks: `gate = ClaimsGate(...)` and `after_round = claims.after_round` when
  `assistant.claims_check` in `agent.json` is true;
- system prompt: 3.11.5.

Each `session/prompt` runs `run_turn(mode="interactive")`. Visible text is streamed as
`agent_message_chunk` once the gate releases it, so a voice client can start speaking at the
first released sentence. Transcript records go to the host as `_hal/record` (REQ-AGT-66);
the agent no longer appends to `~/.local/state/hal/turns.jsonl` (it cannot write host state
from the box). When `assistant.log_conversation` in `agent.json` is true and the broker offers
`memory_log_conversation`, the agent calls it after the turn through the broker like any
other call, so policy and audit apply.

#### 3.8.3 Per-turn tool selection

```python
class ToolBackend(Protocol):
    """Host tools. The only implementation is BrokerBackend; the protocol exists for fakes."""
    def list_tools(self) -> list[ToolSpec]: ...
    def find_tools(self, query: str, limit: int) -> list[ToolSpec]: ...
    def call(self, name: str, args: dict[str, Any], *, timeout_s: float,
             cancel: threading.Event) -> BackendOutcome: ...


@dataclass(frozen=True, slots=True)
class BackendOutcome:
    status: Literal["ok", "error", "denied", "unavailable"]
    text: str                            # the broker's text content, passed through unchanged
    untrusted: bool = False              # from result._meta.hal.untrusted (02-sandbox)
    guard: str = ""                      # from result._meta.hal.guard, for display only
```

There is no `grant`, `deny` or `approval_required` here: approvals never pass through the
agent (REQ-AGT-98).

Selection for each turn: `backend.find_tools(user_text, tools_per_turn)`, which calls the
broker's `hal_find_tools` (schema and result fields defined in 04-hub "MCP server, Modes"),
plus the agent's own `hal_find_tools` meta-tool (`tools/search.py`, 3.1), which forwards the
model's query to the same broker tool (and, in the coding toolset, also ranks in-box MCP
tools locally with the same lexical scoring). Found tools are offered to the model under their
own names with the `parameters` schema the broker returned, up to 16 offered tools
(REQ-AGT-52).
A call to a found tool goes out as `tools/call` on that name when the broker listed it in
`tools/list`, else as `tools/call` of `hal_call` with `{"tool": name, "arguments": args}`;
either way the broker binds any approval to the inner tool and its arguments (04-hub
"MCP server").

The coding agent uses the same mechanism for broker and MCP tools: when the built-in tools plus
broker and MCP tools exceed the profile's `max_tools_hint`, the extra tools are offered only
through `hal_find_tools` (pinned ones excepted, `[mcp.servers.<name>].pinned`).

#### 3.8.4 Approvals

The model never approves, and neither does the agent (REQ-AGT-98, RESOLUTIONS B5):

1. The model calls a host tool. The agent sends `tools/call` to the broker and waits.
2. If policy says `ask`, the broker sends a host-originated approval request to the
   session's host surfaces (TUI panel, `hal approve`, Telegram buttons, the voice summary;
   01-security "Approval surfaces and protocol", 06-core-cli "Approvals"). The request shows
   every argument the call will run with; the agent and the model are not involved.
3. The broker answers the waiting `tools/call`: the tool's result, or `denied` when the user
   declined or the approval timed out. The agent passes the result through (REQ-AGT-26) or
   renders `denied` with the note of REQ-AGT-54.

The agent's wait is bounded by `broker_call_timeout_s` (3.13.2), a backstop above the
broker's own approval timeout. The model may tell the user that Hal is asking for approval;
that sentence carries no authority. A later "yes" from the user is ordinary prompt text.

In the coding agent the flow is the same; `review` mode (3.5.10) is a separate, in-box
permission that never covers host tools.

#### 3.8.5 Long jobs

There is no agent built-in for long jobs. The hub tool `core_long_task` in halcore's built-in
module (tier `act`, runs a sandboxed `hal -p`; `[agent].cloud_jobs = false` by default;
RESOLUTIONS F2, owner 06-core-cli) reaches the assistant through the broker and is selected
by `hal_find_tools` like any other tool. Its approval and its completion notice are host
concerns.

### 3.9 Context management

#### 3.9.1 Input budget

`window` = the smallest known of: profile `context`, the gateway's `context_length` for the
model (`Provider.context_window`, from `GET /v1/models`; 05-gateway, RESOLUTIONS D5), the
`n_ctx` learned from an overflow error, and `[agent.context].window` when set. The agent never
asks llama-server's `/props`, which the forwarder refuses (01-security REQ-SEC-66).
`input_budget = window - max_output - safety_tokens` (512).

#### 3.9.2 Fit

```python
class ContextManager:
    def fit(self, request: ChatRequest, history: list[Message], counter: TokenCounter,
            provider: Provider) -> FitResult: ...
```

1. Estimate the full request (3.3.5). Fits under `mask_at * input_budget`: send.
2. Mask (3.9.3) down to `mask_to * input_budget`.
3. Still over `summarize_at * input_budget`: summarise (3.9.4).
4. Still over `input_budget`: cut the largest remaining non-protected message to head 2,000 and
   tail 2,000 characters; repeat while it helps.
5. Still over: `context_overflow` (REQ-AGT-62).

Protected (never masked or summarised): the system prompt, the latest user message, the newest
`keep_recent` (3) tool exchanges, and the state note.

Masking is done in one sweep down to `mask_to` rather than a little before every request,
because every rewrite of early context invalidates the server's prefix cache (relayed:
"token reduction is not cost reduction"); one sweep pays that cost once per many requests.

#### 3.9.3 Observation masking

Oldest first, each tool message outside the protected set is replaced by a stub, and its
`meta.masked` set to the stub:

```text
[hal] output of read src/app.py:1-200 hidden to save context (5213 characters). Call it again if you need it.
```

Masking matched LLM summarisation at half the cost in the literature the gemma campaign read
(relayed, "Complexity Trap"), and summaries that keep only text lose the tool history Gemma
relies on (relayed, `gemma-agent.md` 3.4), so masking goes first.

#### 3.9.4 Summary compaction

One model request, no tools, `thinking = "off"`, `max_output = 1500`, over the messages older
than the protected set, with the prompt in 3.11.4. The older messages are replaced by one
user message:

```text
[hal] The earlier part of this session was compacted. Summary:
<model summary>

<state note>
```

On any error or an empty summary, the state note alone is used (`kind = "note"`), so a failing
compaction never loses the work (REQ-AGT-60).

#### 3.9.5 State note

Maintained by the harness from tool calls and results, not written by the model (a model-
written notes slot cost the 31B the time it needed for its first edit: `toolbelt-notes`
killed 2026-10-22, relayed in `dev-notes.md`). At most 3,000 characters:

```text
[hal state note]
Task: <the turn's user message, first 600 characters>
Plan: <todo items with status, or "none">
Files read: src/app.py:1-200, src/util.py:40-90
Files changed: src/app.py (edit x2), tests/test_app.py (write)
Last command: python -m pytest tests/test_app.py -q -> exit 1; last 5 lines:
  ...
Budget: 23 of 50 tool calls, 6 of 15 min used.
Recent errors: edit src/app.py not_found (x2)
```

### 3.10 Sessions

#### 3.10.1 Ids and locations

Session id: 12 lowercase hex characters (`secrets.token_hex(6)`), one id for the runtime
directory, checkpoint refs, the ACP `sessionId` and the transcript (RESOLUTIONS E2,
REQ-AGT-100). halcore chooses it and passes it in `session/new` `_meta.hal.sessionId` (or in
`agent.json` `session_id` for the first session of a box); the agent generates one only when
neither is given, for example in a standalone `hal-agent -p`. Creation time lives in the
`session` record, not in the id.

Layout (RESOLUTIONS E3; 03 owns the record schema, 06-core-cli writes `meta.json` and handles
`_hal/record`):

```text
state_dir()/sessions/<sid>/
  meta.json          halcore's session summary (06-core-cli "Sessions"); not written by halagent
  transcript.jsonl   the records of 3.10.2
```

| Mode | Who writes `transcript.jsonl` |
| --- | --- |
| Client advertises `clientCapabilities._meta.hal.records = true` (halcore: box, backend `none`, unboxed assistant) | the host, from `_hal/record` notifications |
| No such capability, `sandbox = "none"` (standalone `hal-agent -p`, an editor running `hal-agent --acp` directly) | the agent itself |
| No such capability, in the box | nobody; the agent sends a `notice` once |

In the box the agent writes no transcript file: it has no writable path the host reads (`/run/hal`
is a read-only mount, RESOLUTIONS E10), and a file under `/work` would land in the user's
project where any `run` command could alter it. The host validates each record against the
schema, accepts only the record types below (never `checkpoint`, which only the host writes),
and caps a session file at 256 MiB.

Assistant sessions follow the same rules; each ACP session in a long-lived assistant box is
its own transcript.

#### 3.10.2 Record schema (v1)

One JSON object per line, ASCII-escaped (`ensure_ascii=True`), `ts` in UTC with `Z`.

```json
{"type":"session","v":1,"id":"3f9a1c0b7e2d","created":"2026-10-23T18:15:00Z","agent":"halagent 0.1.0","mode":"code","toolset":"coding","channel":"tui","cwd":"/work","host_root":"/home/me/proj","model":"hal-code","api":"openai","family":"qwen35","sandbox":"docker-runsc","title":""}
{"type":"message","ts":"2026-10-23T18:15:02Z","turn":1,"i":0,"msg":{"role":"user","content":"Fix the failing date parser test."}}
{"type":"message","ts":"2026-10-23T18:15:09Z","turn":1,"i":1,"msg":{"role":"assistant","content":"","reasoning":"...","tool_calls":[{"id":"call_1_1","name":"grep","arguments":{"pattern":"parse_date"},"repair":[]}]},"usage":{"prompt_tokens":1834,"completion_tokens":61,"reasoning_tokens":40,"cached_tokens":0,"estimated":false},"model_s":6.8}
{"type":"message","ts":"2026-10-23T18:15:09Z","turn":1,"i":2,"msg":{"role":"tool","tool_call_id":"call_1_1","tool_name":"grep","content":"3 matches in 2 files\n..."},"ok":true,"error_type":"","tool_s":0.04}
{"type":"compaction","ts":"2026-10-23T18:31:40Z","turn":1,"id":"cmp_1","kind":"mask","masked":{"2":"[hal] output of grep ... hidden ..."},"before_tokens":19870,"after_tokens":11020}
{"type":"compaction","ts":"2026-10-23T18:40:12Z","turn":2,"id":"cmp_2","kind":"summary","replaces":[0,57],"summary_i":58,"before_tokens":24010,"after_tokens":6200}
{"type":"notice","ts":"2026-10-23T18:15:09Z","turn":1,"level":"warning","code":"repaired_call","text":"leaked:gemma4"}
{"type":"turn_end","ts":"2026-10-23T18:22:45Z","turn":1,"stop_reason":"finished","final_text":"...","stats":{"model_requests":31,"tool_calls":24}}
{"type":"checkpoint","ts":"2026-10-23T18:15:01Z","turn":1,"n":1,"ref":"refs/hal/checkpoints/3f9a1c0b7e2d/1"}
```

| Record | Fields |
| --- | --- |
| `session` (first line only) | `v` (1), `id` (12 hex), `created`, `agent`, `mode` (`code`, `assistant`, `headless`), `toolset` (`coding`, `assistant`), `channel` (from `agent.json`, empty when standalone), `cwd`, `host_root`, `model`, `api`, `family`, `sandbox` (`<backend>-<runtime>` such as `docker-runsc`, `bwrap`, or `none`), `title` |
| `message` | `turn`, `i` (0-based message index in the session), `msg` (`Message` as JSON: `role`, `content`, `reasoning` capped at 20,000 characters, `tool_calls`, `tool_call_id`, `tool_name`, `meta`), optional `usage`, `model_s`, `ok`, `error_type`, `tool_s` |
| `compaction` | `turn`, `id`, `kind` (`mask`, `summary`, `note`), `masked` (object: message index as string -> stub) for `mask`, `replaces` ([first, last] message index, inclusive) and `summary_i` (index of the summary message, written as a `message` record right after) for `summary` and `note`, `before_tokens`, `after_tokens` |
| `notice` | `turn`, `level`, `code`, `text` |
| `turn_end` | `turn`, `stop_reason`, `final_text`, `stats` (3.5.7) |
| `checkpoint` (host only) | `turn`, `n`, `ref` |

Secret masking: argument names matching
`pass(word)?|token|secret|api_?key|credential` (halcore `SECRET_ARG`) have their values
written as `"***"` in transcripts, events and exports.

#### 3.10.3 Resume and export

```python
class SessionStore:
    """Reads state_dir()/sessions/<sid>/transcript.jsonl; lists by the session record."""
    def __init__(self, root: Path) -> None: ...
    def list(self, *, cwd: str | None = None, limit: int = 50) -> list[SessionInfo]: ...
    def latest(self, *, cwd: str) -> SessionInfo | None: ...
    def load(self, session_id: str) -> list[dict[str, Any]]: ...
    def export(self, session_id: str, fmt: Literal["md", "jsonl"], *,
               reasoning: bool = False) -> str: ...


class SessionWriter:
    """Appends records; one line per record, flushed and fsync'd at turn_end."""
    def __init__(self, path: Path, header: dict[str, Any]) -> None: ...
    def write(self, record: dict[str, Any]) -> None: ...
    def close(self) -> None: ...
```

Resume replays `message` records in order, applies `compaction` records at their position
(masks set content to the stub; summaries drop messages `replaces[0]..replaces[1]`), and
continues numbering turns and message indices. A last line that does not parse is skipped with
a warning (crash mid-write); any other invalid line fails loudly with its line number
(REQ-AGT-69).

Export `md`: a header (id, model, dates), then each turn: the user text, the assistant text,
each tool call as a fenced block with its arguments and the result cut to 2,000 characters;
reasoning only with `reasoning=True`. Export `jsonl`: the transcript records one per line,
with secret masking applied and `reasoning` removed unless `reasoning=True`.
An ATIF export (the open trajectory format the gemma campaign used) is left for later
(Decisions).

### 3.11 Prompts

#### 3.11.1 Structure of the coding system prompt

Sections, in order, joined with blank lines:

1. Role (one sentence) and environment (workspace path, offline sandbox, backend name).
2. Budget schedule (task mode only).
3. Find, Edit, Check, Finish, Working style.
4. `## Project instructions` from AGENTS.md (3.12.1), when any.
5. `## Skills` listing (3.12.2), when any.
6. `## Tools` block, only for `tool_format = "prompt"` (3.11.6).

The source is the 49-line gemma prompt (`gemma4-agent/gemma-agent/prompts/system.md`, identical
in substance to `batch-project/agent/prompts/system.md`), which was tuned for "edit early,
targeted checks, capped output". Changes: Hal's tool names; no SWE-bench grading talk; schedule
numbers computed from the budget; `run` keeps the output tail itself, so the "always pipe to
tail" rule becomes "keep commands focused"; scratch rule kept; protected-path rule is advice
only (the harness enforces `protect`).

#### 3.11.2 Task-mode template (`prompts/code_task.md`)

Placeholders in braces are filled by `render_code_prompt(settings, budget, extras)`.

```text
You are Hal's coding agent. You work in the project at {workdir} inside an offline sandbox ({sandbox}). Do the task in the user's message: make the smallest correct change, check it, and finish.

## Budget

You have {max_tool_calls} tool calls and {minutes} minutes. todo and finish are free. Follow this schedule:

- Calls 1 to {localize_until}: find the code that matters.
- By call {first_edit_by}: your first edit is on disk. If you are still unsure, edit the most likely place now. An imperfect edit beats no edit.
- Calls {check_from} to {check_until}: run one focused check and make at most one repair.
- After call {check_until}: start no new exploration. Finish the change you have.
- From call {finish_from}: do only the Finish steps.

A tool result that ends with budget_warning means {warn_calls} or fewer calls, or 90 seconds, remain: start no new exploration and finish.

## Find

1. Pick the most specific identifier in the task: an error message, a function or class name, a parameter, or a literal string. Copy it exactly.
2. Search for it with the grep tool. Use the glob tool to find files by name.
3. Read only the lines around the hits with the read tool, giving start_line and end_line. Never read a whole large file.
4. Choose the file that owns the behaviour. Prefer source code over examples, docs and tests.

## Edit

- Change the code with the edit tool. Copy old_string exactly from the read output, without the line numbers, and include enough lines to make it unique. If an edit fails, read the lines again and retry once.
- Fix the cause with the smallest change. Keep the existing style, names and public API.
- Change tests only when the task asks for it.
- Put scratch files in /tmp, never in the project.

## Check

- Check with the run tool, using only a targeted test file or a short python -c reproduction.
- Never run the whole test suite unless the task asks for it. Keep commands focused: long output is cut in the middle.
- Ignore failures that existed before your change.
- Make at most one repair. Never repeat a failing command unchanged.

## Finish

1. With the run tool, look at git status --short and git diff --stat. Undo stray changes.
2. Call finish with a two-sentence summary of what you changed and how you checked it.

## Working style

- Keep each thought short and decide quickly. Long reasoning fills the context window.
- Make one tool call per step, and use only the tools you are given.
- There is no network. Do not try to install packages. If dependencies are missing, say so: the user can run hal code --deps.
```

Schedule numbers for `N = max_tool_calls` (fractions of the gemma schedule's 6, 12, 20, 25 of
28):

| Placeholder | Formula | N = 28 | N = 50 |
| --- | --- | --- | --- |
| `localize_until` | `round(0.21 * N)` | 6 | 10 |
| `first_edit_by` | `round(0.43 * N)` | 12 | 22 |
| `check_from` | `first_edit_by + 1` | 13 | 23 |
| `check_until` | `round(0.71 * N)` | 20 | 36 |
| `finish_from` | `max(check_until + 1, N - 3)` | 25 | 47 |
| `warn_calls` | `budget.warn_calls` | 10 | 10 |
| `minutes` | `round(max_wall_s / 60)` | | 15 |

Scaling the schedule beyond N = 28 is untested [unverified: the coding eval (3.18.2) measures
it; the gemma campaign found phase gates must follow the harness's real signals].

#### 3.11.3 Interactive template (`prompts/code_interactive.md`)

The same role line, then Find, Edit and Check unchanged, then:

```text
## Answering

- When the user asks a question, answer it. Read code only as far as you need to.
- When the user asks for a change, make it, check it, and end with two or three sentences on what changed and how you checked it.
- When a task is unclear or risky, ask one short question instead of guessing.

## Working style

- Keep each thought short and decide quickly. Long reasoning fills the context window.
- Make one tool call per step, and use only the tools you are given.
- There is no network. Do not try to install packages. If dependencies are missing, say so: the user can run hal code --deps.
```

#### 3.11.4 Compaction prompt (`prompts/compaction.md`)

```text
Compress the history of this session so the work can continue in a smaller context. Write for yourself, not for the user. Use these headings exactly:

## Task
The user's request. Quote it exactly if it is shorter than 600 characters.

## Done so far
What was changed, file by file. Quote changed lines exactly. Never paraphrase code.

## Findings
Facts learned: file paths with line numbers, function and class names, error messages and test names, quoted exactly.

## Failed attempts
Edits or commands that failed and why, so they are not repeated.

## Next step
The single next action.

Keep it under 600 words. Do not call tools.
```

"Compress around code, never paraphrase it": summaries answered 4 of 45 behavioural questions
where raw source answered 27 (relayed, `gemma-agent.md` 3.4).

#### 3.11.5 Assistant prompts

Ported from `core/hal/halcore/brain.py` lines 32-87 [verified: read 2026-10-23]. `PERSONA`,
`STYLE` (voice, chat, telegram), `NO_TOOLS`, `SUSPECT`, `CLAIM`, `NO_TOOLS_RETRY` and `RETRY`
are copied verbatim. `TOOL_RULES` is copied verbatim except two bullets:

- Old: "A result with needs_confirmation means the action is destructive: ask in one short
  sentence, and only when {user} agrees call the tool again with _confirmed set to true."
  New: "Some actions need {user}'s approval. Hal asks them itself and shows the exact action;
  your tool call waits for the answer. A result that says it was not approved did NOT
  happen: say so, and do not call it again unless {user} asks."
- Old: "... call deep_work with a complete description; it runs in the background and you will
  be told when it is done. Say you have started it." New: "... when a core_long_task tool is
  offered, call it with a complete description; it runs in the background on this computer
  and {user} is told when it is done. Say you have started it."

The system prompt is assembled as today: rules (`TOOL_RULES` or `NO_TOOLS`), persona, the
channel's `STYLE`, `It is <weekday day month year, HH:MM>.` in the user's local time (spoken,
so local; the box has no host timezone, so halcore writes it into `agent.json`
`assistant.timezone`), and up to 2,500 characters of `memory_profile` (named constant
`PROFILE_CHARS = 2500`), fetched through the broker at session start when that tool is
offered. The `_confirmed` property is no longer added to any schema, and REQ-AGT-35 strips it
if a model invents it.

#### 3.11.6 Tool block for `tool_format = "prompt"` (`prompts/tool_prompt.md`)

```text
## Tools

To call a tool, write exactly one block like this and nothing after it:
<tool_call>
{"name": "TOOL_NAME", "arguments": {"ARG": "VALUE"}}
</tool_call>
The result comes back in the next message. Your tools:
{tool_lines}
```

`tool_lines` has one line per tool: `- name(arg: type, ...): description`. Calls are parsed with
the `hermes` parser; tool results are sent as user messages prefixed `[tool result for NAME]`.

#### 3.11.7 Prompt lint

A unit test (REQ-AGT-73) scans every template for text that looks like a tool call written as
a shell command: a line starting with a tool name followed by `(` or `{`, backticked commands
directly after "call", and code fences with tool names. "Never write shell commands in the
prompt as if they were tool names": rewriting them as prose cut malformed tool names from 31%
to 5% in one replay (relayed, `gemma-agent.md` 3.2).

### 3.12 AGENTS.md, CLAUDE.md and Agent Skills

#### 3.12.1 Instruction files

```python
def load_instructions(workspace_root: Path, cwd: Path, user_file: Path | None,
                      max_chars: int = 16000) -> Instructions: ...

@dataclass(frozen=True, slots=True)
class Instructions:
    text: str                       # rendered "## Project instructions" body
    files: tuple[str, ...]          # files used, root first
    truncated: bool
```

- Directories from `workspace_root` down to `cwd`, root first. In each, use `AGENTS.md`; if
  absent, use `CLAUDE.md`. Later (nearer) files come after earlier ones and the rendered text
  says "When these files disagree, the later one wins", which is the AGENTS.md rule that the
  nearest file takes precedence [verified: agents.md, in `understand/research-interop.md` 5].
- The user-global file goes first: `~/.config/hal/AGENTS.md` out of the box, or its copy at
  `/run/hal/user/AGENTS.md` in the box (the host copies it at session start).
- A line that is only `@relative/path.md` (the Claude Code import form) is replaced by that
  file's text when the path resolves inside the workspace, to depth 2; a `CLAUDE.md` whose only
  content is an import of the `AGENTS.md` beside it is skipped as a duplicate.
- Each file is headed `### From <relative path>`. Total cap 16,000 characters: when exceeded,
  files are cut from the root end and a line `[hal] instructions cut: <files>` is added.
- Instruction files are repository content and may be hostile. That is acceptable because no
  policy depends on prompt text (REQ-AGT-23).

#### 3.12.2 Agent Skills

Discovery order (first wins on a name clash): `<root>/.agents/skills/*/SKILL.md`,
`<root>/.claude/skills/*/SKILL.md`, then user skills (`~/.config/hal/skills/*/SKILL.md`, copied
by the host to `/run/hal/skills/` in the box and mounted read-only to the tools through
`Workspace.readonly`).

Frontmatter is the block between the first two `---` lines, parsed by a small stdlib parser
that accepts `key: value` scalars (plain, single- or double-quoted) and one level of nested
`metadata:` mapping. Validation per the Agent Skills specification [verified:
agentskills.io/specification, in `understand/research-interop.md` 5]: `name` required, 1 to 64
characters of lowercase letters, digits and hyphens, equal to the directory name;
`description` required, at most 1,024 characters; optional `license`, `compatibility` (at most
500 characters), `metadata`, `allowed-tools`. Invalid skills are skipped with a `notice`.
`allowed-tools` is ignored: the harness, not a skill file, decides what may run.

Listing in the system prompt (progressive loading: metadata only):

```text
## Skills

Skills are instructions for particular jobs. When one fits the task, read its SKILL.md with the read tool first, then follow it.
- pdf-tools: Extract text and tables from PDF files. (/run/hal/skills/pdf-tools/SKILL.md)
```

At most 12 skills and 3,000 characters (SkillsBench found 3 or fewer focused skills work best,
relayed; listing more costs context on every request). Skill scripts run through `run` like any
other command.

### 3.13 MCP client and broker client

#### 3.13.1 User MCP servers

03 owns the user-facing table `[mcp.servers.<name>]` (RESOLUTIONS E6). Configuration,
`~/.config/hal/config.toml`:

```toml
[mcp]
project_servers = "box"        # "box" | "never": read <root>/.mcp.json
restart_limit = 1              # restarts per server per session

[mcp.servers.docs]
command = "/usr/local/bin/docs-mcp"    # stdio server: absolute path inside the box
args = ["--stdio"]
env = { DOCS_ROOT = "/work/docs" }
cwd = ""                               # empty: the workspace root
timeout_s = 60                         # per tool call
enabled = true
pinned = ["docs_search"]               # always offered, not only through hal_find_tools
tools_allow = []                       # empty: all
tools_deny = []

[mcp.servers.mail]
host = true                            # runs on the host as a broker upstream, under policy
command = "/home/me/.local/bin/mail-mcp"
args = []
```

| Key | Default | Reason |
| --- | --- | --- |
| `project_servers` | `"box"` | A project `.mcp.json` runs project code; inside the box that is no worse than `run`. With `sandbox = "none"` it is never read. |
| `timeout_s` | 60 | hal-hub's subprocess tool default. |
| `restart_limit` | 1 | One restart covers a crash; a crash loop should surface. |
| `host` | false | Stdio servers run in the box unless marked `host`. A `host = true` entry is not started by the agent: the broker reads it as an upstream (02-sandbox "Broker upstreams", argv = `command` + `args`), as pseudo-pack `mcp-<name>` with tools named `<name>__<tool>`, tier `act` unless `policy.toml` says otherwise (RESOLUTIONS E6; 01-security REQ-SEC-88). The agent sees them as broker tools. |
| `url`, `headers` | none | Not accepted in v1: an entry with `url` is a config error naming this reason (REQ-AGT-75). |

Sources merged in order: `[mcp.servers]`, project `.mcp.json` (`{"mcpServers": {name:
{"command", "args", "env"}}}`; `http` entries are skipped with a `notice`), and ACP
`session/new` `mcpServers` (stdio entries only; others are skipped with a `notice`). Same
name: the later source wins and a `notice` says so. A project file or an ACP client can never
set `host = true`: only `config.toml` can, because both of the others are under the model's
or the editor's control.

With `sandbox = "none"` the agent starts none of these servers (REQ-AGT-75, RESOLUTIONS F4,
ATK-40): without a box a stdio server would run on the host as the user with no broker
policy or audit. `host = true` servers still work, because the broker runs them. With
`--toolset assistant` no in-box server is started (REQ-AGT-102).

Implementation (`halagent/mcp/client.py`): one asyncio event loop in a daemon thread; each
server is an `mcp.Client` over `StdioServerParameters(command, args, env, cwd)` [verified:
`mcp.Client` and `StdioServerParameters` fields `command, args, env, cwd, encoding,
encoding_error_handler` in `mcp` 2.3.0, installed in a scratch venv 2026-10-23]; synchronous
wrappers use `asyncio.run_coroutine_threadsafe(...).result(timeout)`. The SDK negotiates
`2026-07-28` or a handshake version up to `2025-11-25` [verified: `mcp_types/version.py`].
The client registers no elicitation or sampling handler, so a server request for either is
answered with an error.

Tools appear as `mcp__<server>__<tool>` (name characters outside `[A-Za-z0-9_-]` become `_`,
total length cut to 64), kind `other`, descriptions cut to 1,024 characters. Results: text
content blocks joined; other content types replaced by `[<type> content omitted]`;
`isError: true` becomes an error result; the text is wrapped in the untrusted-content envelope
defined in 01-security ("Taint, guard and reviewer") with `tool` set to the
`mcp__<server>__<tool>` name, `trust="untrusted"` and `guard="off"` (REQ-AGT-26), and capped
by REQ-AGT-45. `structuredContent` is rendered with `json.dumps(indent=1)` when there is no
text block. Tool annotations are shown to the user but never used to decide anything (MCP
spec: clients must not make tool-use decisions from untrusted annotations; 01-security
REQ-SEC-87).

#### 3.13.2 Broker client

`halagent/mcp/lite.py`, stdlib only (REQ-AGT-77):

```python
class LiteMcpClient:
    def __init__(self, url: str, *, client_name: str, timeout_s: float = 30.0,
                 max_message_bytes: int = MAX_MESSAGE_BYTES) -> None: ...
        # url: "unix:///run/hal/broker.sock" in the box, a host path with backend "none"
    def initialize(self) -> dict[str, Any]: ...
    def list_tools(self) -> list[dict[str, Any]]: ...          # follows nextCursor
    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout_s: float,
                  cancel: threading.Event) -> dict[str, Any]: ...
    def ping(self) -> None: ...
    def close(self) -> None: ...

MAX_MESSAGE_BYTES = 4 * 2**20   # the broker's per-message cap (RESOLUTIONS E9, 02-sandbox)
```

Wire: newline-delimited JSON-RPC 2.0 over the unix socket (MCP stdio framing; 02-sandbox
"Broker socket transport"). Handshake: `initialize` with `protocolVersion: "2025-11-25"`,
`capabilities: {}`, `clientInfo: {name, version}`, then `notifications/initialized`. The empty
capabilities mean no elicitation: the broker can never ask the box to approve anything
(RESOLUTIONS B4). The broker is Hal's own server on the official SDK, which still accepts
handshake versions [verified: `HANDSHAKE_PROTOCOL_VERSIONS` in `mcp_types` 2.3.0 ends at
`2025-11-25`], so a 200-line stdlib client is enough and keeps the broker path free of SDK
version churn.

Example (result text abbreviated; the hub has already applied 01's envelope to it):

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"halagent","version":"0.1.0"}}}
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"hal-broker","version":"0.1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"web_read","arguments":{"url":"https://docs.python.org/3/library/tomllib.html"}}}
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"<tool_result tool=\"web_read\" ...>..."}],"isError":false,"_meta":{"hal":{"status":"ok","untrusted":true,"guard":"off"}}}}
```

Hal-specific outcome data travels in `result._meta.hal`, defined by 02-sandbox with the keys
`status`, `untrusted` and `guard` and no approval id (RESOLUTIONS E7). The agent maps
`status` to `BackendOutcome.status` (`ok`, `denied`, `unavailable`; any other value is
`error`), uses `untrusted` and `guard` for display only, and passes the text content to the
model unchanged (REQ-AGT-26). A missing `_meta.hal` is read as `ok` or `error` from
`isError`.

Each broker call waits up to `broker_call_timeout_s` (default 3,900 s: the longest approval
wait a host surface allows, the 3,600 s headless queue of 06-core-cli, plus 300 s for the tool
itself). The broker, not this timer, decides when an approval has timed out; the timer only
stops a hung connection from holding the turn forever. Cancel sends
`notifications/cancelled` with the request id and stops waiting.

Broker tool kinds for ACP come from the tier: `read` -> `read`, `write` -> `edit`, `act` ->
`other`, `send` -> `fetch`, `system` -> `execute`.

### 3.14 ACP agent server

#### 3.14.1 SDK or hand-rolled

Hand-rolled (`halagent/acp/`, about 600 lines), validated in tests against the vendored ACP v1
JSON schema. Reasons:

- The Python SDK `agent-client-protocol` is 0.12.1 (2026-08-16) and requires `pydantic>=2.7`
  [verified: PyPI JSON API 2026-10-23]; a 0.x API changes under us while ACP's schema moves
  fast (release `schema-v1.26.0` on 2026-10-16 [verified: GitHub releases API]).
- Hal needs about ten methods over newline-delimited JSON-RPC on stdio, which the stdlib does
  well; the brief asks for stdlib where practical.
- Conformance does not depend on the SDK: every message the test suite sends or receives is
  validated against `schema/v1/schema.json` at tag `schema-v1.26.0` (Apache-2.0
  [verified: GitHub API license field]), vendored under `tests/fixtures/acp/` with its source URL
  and sha256 (REQ-AGT-79).

#### 3.14.2 Methods

| Method | Direction | Behaviour |
| --- | --- | --- |
| `initialize` | client -> agent | Reply as below. Requested version other than 1: reply with 1 (the client decides). |
| `session/new` | client -> agent | Params `cwd`, `mcpServers`, optional `_meta.hal.sessionId` (12 hex, from halcore; REQ-AGT-100). `cwd` must be inside the workspace root after host-path mapping (3.14.5), else error -32602. Coding toolset in the box: starts user MCP servers (3.13.1). Returns `sessionId` and `modes`. |
| `session/load` | client -> agent | Params `sessionId`, `cwd`, `mcpServers`, optional `_meta.hal.records` (transcript records from the host). Replays history as `session/update` (`user_message_chunk`, `agent_message_chunk`, `tool_call` with final status), then replies `{}`. Unknown id: error -32602. |
| `session/prompt` | client -> agent | Runs one turn; streams `session/update`; replies `{stopReason}` when the turn ends. A second prompt while one runs: error -32002 "a prompt is already running". A prompt whose whole text is `/compact` runs summary compaction (3.9.4) instead of a model turn and ends with `end_turn` (06-core-cli forwards `/compact` as prompt text). |
| `session/cancel` | client -> agent (notification) | Sets the turn's cancel event (REQ-AGT-82). |
| `session/set_mode` | client -> agent | `auto` or `review` (coding toolset; the assistant toolset offers only `auto`, since it has no in-box tools to review); replies `{}`; sends `current_mode_update`. Any other id, including `plan`, is error -32602 (RESOLUTIONS F3). |
| `session/update` | agent -> client (notification) | 3.14.3. |
| `session/request_permission` | agent -> client | Review mode (3.5.10). No answer within 3,600 s counts as `reject_once`. |
| `_hal/record` | agent -> client (notification) | Transcript record (3.10.2); sent only when the client advertised `clientCapabilities._meta.hal.records = true`. |
| any other | | `-32601 Method not found`. |

`initialize` result:

```json
{"protocolVersion":1,
 "agentCapabilities":{"loadSession":true,
   "promptCapabilities":{"image":false,"audio":false,"embeddedContext":true},
   "mcpCapabilities":{"http":false,"sse":false},
   "_meta":{"hal":{"records":true,"version":"0.1.0"}}},
 "agentInfo":{"name":"hal-agent","title":"Hal","version":"0.1.0"},
 "authMethods":[]}
```

`mcpCapabilities.http` is always `false`: user MCP servers are stdio only (REQ-AGT-75).
`session/new` result (coding toolset):

```json
{"sessionId":"3f9a1c0b7e2d",
 "modes":{"currentModeId":"auto","availableModes":[
   {"id":"auto","name":"Auto","description":"Work freely inside the sandbox"},
   {"id":"review","name":"Review","description":"Ask before each edit or command"}]}}
```

#### 3.14.3 Event mapping

| halagent event | ACP `session/update` |
| --- | --- |
| `text` | `agent_message_chunk` `{content: {type: "text", text}, messageId}` |
| `reasoning` | `agent_thought_chunk` `{content: {type: "text", text}, messageId}` |
| `tool_call` | `tool_call` `{toolCallId, name, title, kind, status: "pending", rawInput, locations}` |
| `tool_start` | `tool_call_update` `{toolCallId, status: "in_progress"}` |
| `tool_result` | `tool_call_update` `{toolCallId, status: "completed" or "failed", content: [{type: "content", content: {type: "text", text}}] or [{type: "diff", path, oldText, newText}], rawOutput: {ok, error_type, data}}` |
| `plan` | `plan` `{entries}` |
| `usage` | `usage_update` `{used, size}` |
| `compaction` | `compaction_update` only when the client advertised `session.compaction`; otherwise not sent |
| `notice` | `notice` `{severity, title, description}` only when the client advertised `session.notices`; otherwise logged |
| `permission` | none (the request itself is the message) |
| `turn_end` | the `session/prompt` response |

Locations and diff paths are absolute host paths (3.14.5).

#### 3.14.4 Stop reasons

| halagent | ACP `stopReason` |
| --- | --- |
| `end_turn`, `finished` | `end_turn` |
| `max_tool_calls`, `max_model_requests`, `max_time`, `loop`, `nudge_limit`, `tool_errors` | `max_turn_requests`, with `_meta.hal.stopReason` set to the halagent value |
| `max_tokens`, `context_overflow` | `max_tokens`, with `_meta.hal.stopReason` |
| `refusal` | `refusal` |
| `cancelled` | `cancelled` |
| `error` | JSON-RPC error -32603 on `session/prompt`, `data: {"hal": {"stopReason": "error", "message": ...}}` |

#### 3.14.5 Paths

The agent knows its workspace root (`/work`) and the host root it stands for (`--host-root`, or
`host_root` in `agent.json`). It rewrites paths in both directions itself: incoming `cwd` and
`resource` URIs from host to box, outgoing `locations`, diff `path` and tool titles from box to
host. The mapping is idempotent: an incoming path already under `/work` is kept, so the
`hal acp` proxy may also translate paths (06-core-cli REQ-CLI-39) without double mapping, and
the TUI, which talks to the agent without the proxy, gets host paths directly. With
`--toolset assistant` the workspace root is the channel's scratch `/work` and nothing outside
it is accepted.

#### 3.14.6 Trust

The ACP client (the host TUI, or `hal acp` for Zed, voice and Telegram) treats everything
from the box as untrusted text: permission requests from the box only gate in-box actions,
which the box already contains, and host exits use the broker's own host-originated approval
request with the exact canonical arguments (06-core-cli REQ-CLI-38: the proxy strips any
`hal/`-prefixed `_meta` key from agent messages, so the agent cannot forge one). halcore calls
the broker's `on_human_message` for every prompt it sends (01-security, RESOLUTIONS F5); the
agent plays no part in taint.
`fs/*` and `terminal/*` client methods are not used (REQ-AGT-84): using them would let the
in-box agent act on the host outside the box.

#### 3.14.7 Example

```json
{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{"session":{"notices":{}},"_meta":{"hal":{"records":true}}},"clientInfo":{"name":"hal","version":"0.1.0"}}}
{"jsonrpc":"2.0","id":0,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,"promptCapabilities":{"image":false,"audio":false,"embeddedContext":true},"mcpCapabilities":{"http":false,"sse":false},"_meta":{"hal":{"records":true,"version":"0.1.0"}}},"agentInfo":{"name":"hal-agent","title":"Hal","version":"0.1.0"},"authMethods":[]}}
{"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":"/home/me/proj","mcpServers":[],"_meta":{"hal":{"sessionId":"3f9a1c0b7e2d"}}}}
{"jsonrpc":"2.0","id":1,"result":{"sessionId":"3f9a1c0b7e2d","modes":{"currentModeId":"review","availableModes":[{"id":"auto","name":"Auto"},{"id":"review","name":"Review"}]}}}
{"jsonrpc":"2.0","id":2,"method":"session/prompt","params":{"sessionId":"3f9a1c0b7e2d","prompt":[{"type":"text","text":"Rename parse_date to parse_iso_date"}]}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"3f9a1c0b7e2d","update":{"sessionUpdate":"tool_call","toolCallId":"call_1_1","name":"grep","title":"Search 'parse_date'","kind":"search","status":"pending","rawInput":{"pattern":"parse_date"}}}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"3f9a1c0b7e2d","update":{"sessionUpdate":"tool_call_update","toolCallId":"call_1_1","status":"completed","content":[{"type":"content","content":{"type":"text","text":"3 matches in 2 files\n..."}}]}}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"3f9a1c0b7e2d","update":{"sessionUpdate":"tool_call","toolCallId":"call_1_2","name":"edit","title":"Edit src/dates.py","kind":"edit","status":"pending","rawInput":{"path":"src/dates.py","old_string":"def parse_date(","new_string":"def parse_iso_date("},"locations":[{"path":"/home/me/proj/src/dates.py"}]}}}
{"jsonrpc":"2.0","id":7,"method":"session/request_permission","params":{"sessionId":"3f9a1c0b7e2d","toolCall":{"toolCallId":"call_1_2"},"options":[{"optionId":"allow-once","name":"Allow once","kind":"allow_once"},{"optionId":"allow-always","name":"Allow edit for this session","kind":"allow_always"},{"optionId":"reject-once","name":"Reject","kind":"reject_once"}]}}
{"jsonrpc":"2.0","id":7,"result":{"outcome":{"outcome":"selected","optionId":"allow-once"}}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"3f9a1c0b7e2d","update":{"sessionUpdate":"usage_update","used":5210,"size":32768}}}
{"jsonrpc":"2.0","id":2,"result":{"stopReason":"end_turn"}}
```

Every message in the 2026-10-23 version of this example and the two results in 3.14.2
validated against the ACP v1 schema at tag `schema-v1.26.0` (sha256
`4bcd1d2051ff7ed379bd9b234fb9b09c5ab9245478698e9fab805bd9b52a33a6`) [verified: `jsonschema`
Draft 2020-12 validator, script `spec-tmp/agent/validate_acp.py`, 2026-10-23]. The 2026-10-24
revision changed the session id strings and added `_meta.hal.sessionId` to `session/new`
[unverified against the schema; `test_acp_server.py` validates every example message, and the
script was lost in the scratch wipe].

#### 3.14.8 In-process entry point

```python
def serve(reader: BinaryIO, writer: BinaryIO, *, toolset: Literal["coding", "assistant"],
          workdir: Path | None, settings: AgentSettings) -> None:
    """Serves ACP v1 on a byte-stream pair until EOF; the same code as hal-agent --acp.

    Used by halcore for the unboxed assistant fallback (RESOLUTIONS A1) over two os.pipe()
    pairs. `settings` carries the endpoint, the broker socket path and the session id exactly
    as agent.json would (sandbox "none"); `workdir` None means the assistant's scratch
    directory from settings. Blocks the calling thread; never prints; logs through logging.
    """
```

`hal-agent --acp` is `serve(sys.stdin.buffer, sys.stdout.buffer, ...)` with the settings
read from `--config`. Because halagent never imports halsandbox, the in-process agent still
reaches the broker through its unix socket (02-sandbox: with backend `none` the socket paths
are host paths under `$R/s/<sid>/run/`), not through a Python object (RESOLUTIONS E5).

### 3.15 Headless mode

06-core-cli owns the `hal -p` flags and exit codes; 03 owns the output schema (RESOLUTIONS
F1). `hal-agent -p` uses 06's flag names and exit codes, so the two commands differ only in
who starts the sandbox.

#### 3.15.1 Synopsis

```text
hal-agent -p TASK|- [--json | --stream-json] [--dir DIR] [--timeout SECONDS]
          [--model ALIAS] [--include-thinking] [--toolset coding|assistant] [--config FILE]
```

| Flag | Same as in `hal -p` (06-core-cli) | Meaning here |
| --- | --- | --- |
| `--json`, `--stream-json` | yes | Output format (3.15.2, 3.15.3); without either, text |
| `--dir DIR` | yes | Workspace; default the current directory. Ignored in box mode, where the workspace is `/work` |
| `--timeout SECONDS` | yes | Cancels the turn at that time; exit 124 |
| `--model ALIAS` | yes | Gateway model or role alias; default `[agent].model` |
| `--include-thinking` | yes | Adds reasoning to the output |
| `--toolset` | no (`hal -p --chat` selects `assistant`) | `coding` (default) or `assistant` |
| `--config FILE` | no | Settings file (`agent.json`); box mode when its `sandbox` is not `none` |

`TASK` of `-` reads the task from stdin. Approval handling (`--approvals`), `--isolated` and
checkpoints are host concerns that only `hal -p` has. `hal -p` starts the sandbox, drives the
in-box agent over ACP, and renders the same formats on the host with
`halagent.headless.Renderer` from the ACP updates and `_hal/record`s, adding the host-only
`system` lines of 3.15.3. Headless runs use task mode; review mode is not available (nobody to
ask).

#### 3.15.2 `--json` result

One object on stdout at the end:

```json
{"type":"result","subtype":"success","is_error":false,"exit_code":0,
 "result":"Renamed parse_date to parse_iso_date in 3 files and ran tests/test_dates.py: 12 passed.",
 "stop_reason":"finished","session_id":"3f9a1c0b7e2d","model":"hal-code",
 "num_turns":1,"num_model_requests":19,"num_tool_calls":14,"duration_ms":214000,
 "usage":{"input_tokens":184220,"output_tokens":6120,"reasoning_tokens":3900},
 "files_changed":["src/dates.py","src/cli.py","tests/test_dates.py"],
 "stats":{"repaired_calls":1,"malformed_calls":0,"unknown_tools":0,"nudges":0,"compactions":0,"peak_prompt_tokens":14210,"first_edit_call":6},
 "ts":"2026-10-23T18:22:45.112Z","hal_version":"0.1.0"}
```

`stop_reason` is the halagent value (3.5.6); `subtype` and `exit_code` follow it:

| halagent stop reason | ACP `stopReason` (3.14.4) | `subtype` | `exit_code` (06-core-cli) |
| --- | --- | --- | --- |
| `finished`, `end_turn` | `end_turn` | `success` | 0 |
| `max_tool_calls`, `max_model_requests`, `max_time` | `max_turn_requests` | `error_max_turns` | 4 |
| `loop`, `nudge_limit`, `tool_errors` | `max_turn_requests` | `error_during_execution` | 4 |
| `max_tokens`, `context_overflow` | `max_tokens` | `error_during_execution` | 4 |
| `refusal` | `refusal` | `error_refusal` | 5 |
| `error` | JSON-RPC error | `error_during_execution` | 1 |
| `error` because the gateway could not be reached after retries | JSON-RPC error with `data.hal.reason = "model_unreachable"` | `error_model_unreachable` | 3 |
| `--timeout` expired (`cancelled` by the timer) | `cancelled` | `error_timeout` | 124 |
| `cancelled` (SIGINT or SIGTERM) | `cancelled` | `error_cancelled` | 130 |

Exit codes follow the ACP stop reason so that `hal -p`, which sees only ACP, and
`hal-agent -p` agree. Usage and configuration errors exit 2 before any result is printed.

`files_changed` comes from `git --no-optional-locks status --porcelain` in the workspace when it
is a git repository (works with a read-only `.git` [verified: `chmod -R a-w .git` then
`git status --porcelain` and `git diff --stat`, exit 0, 2026-10-23]), else from the paths
`edit` and `write` touched.

#### 3.15.3 `--stream-json`

One JSON object per line, flushed per line, in the spirit of
`claude -p --output-format stream-json`. Every line carries `session_id` and `ts` (UTC, `Z`,
milliseconds; 06-core-cli REQ-CLI-34):

```json
{"type":"system","subtype":"init","session_id":"3f9a1c0b7e2d","ts":"...","cwd":"/home/me/proj","model":"hal-code","toolset":"coding","sandbox":"docker-runsc","tools":["read","grep","glob","edit","write","run","todo","finish"],"hal_version":"0.1.0"}
{"type":"assistant","session_id":"3f9a1c0b7e2d","ts":"...","message":{"role":"assistant","content":[{"type":"text","text":"I'll find the parser first."},{"type":"tool_use","id":"call_1_1","name":"grep","where":"box","input":{"pattern":"parse_date"}}]}}
{"type":"user","session_id":"3f9a1c0b7e2d","ts":"...","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1_1","content":"3 matches in 2 files\n...","is_error":false}]}}
{"type":"system","subtype":"notice","session_id":"3f9a1c0b7e2d","ts":"...","level":"warning","code":"repaired_call","text":"leaked:gemma4"}
{"type":"system","subtype":"compaction","session_id":"3f9a1c0b7e2d","ts":"...","kind":"mask","before_tokens":19870,"after_tokens":11020}
{"type":"system","subtype":"usage","session_id":"3f9a1c0b7e2d","ts":"...","used":14210,"size":32768}
{"type":"result", "...": "same object as --json"}
```

`tool_use.where` is `box` for in-box tools and `host` for broker tools.

Host-only lines, written by `hal -p` and never by the agent (the agent sees neither approvals
nor checkpoints):

| `subtype` | Fields |
| --- | --- |
| `approval_requested` | `approval_id` (`apr_` + 16 hex), `tool`, `tier`, `reason`, `key` (01-security "Approval binding") |
| `approval_resolved` | `approval_id`, `verdict`, `scope`, `source` (01-security `Decision`) |
| `checkpoint` | `n`, `ref` (02-sandbox) |

`--include-thinking` adds `{"type": "thinking", "thinking": ...}` content blocks to
`assistant` messages. Tool results in stream-json are cut to 4,000 characters.

#### 3.15.4 Exit codes

The codes and their meanings are 06-core-cli's ("Command reference", exit codes); 3.15.2 maps
each stop reason to one. Without a format flag, only the final text goes to stdout; notices
and errors go to stderr.

### 3.16 CLI synopsis and the in-box settings file

#### 3.16.1 `hal-agent`

```text
hal-agent --acp [--config FILE] [--host-root DIR] [--toolset coding|assistant]
    Serve ACP v1 on stdin/stdout (the in-box agent; halcore spawns it, RESOLUTIONS A7).
hal-agent -p TASK [options of 3.15.1]
    Run one task headless.
hal-agent profiles list
hal-agent profiles show MODEL
    Print the resolved profile with the source of each value.
hal-agent sessions list [--cwd DIR] [--limit N]
hal-agent sessions show ID
hal-agent sessions export ID [--format md|jsonl] [--reasoning] [--out FILE]
hal-agent doctor
    Check: config parses, the gateway answers GET /v1/models, token counting endpoint,
    profile resolution for the configured model, ripgrep and bash present, sandbox
    detection, broker socket, MCP servers start and list tools. Prints one line per check.
```

With `--config`, the settings file's `toolset` wins; a `--toolset` flag that disagrees with it
is a usage error (exit 2). `hal` (06-core-cli) wraps these as `hal code`, `hal chat`, `hal -p`,
`hal acp`, `hal sessions` and `hal eval`. `cli.py` is the only module that prints; libraries
log.

#### 3.16.2 Detecting the box

The agent is in box mode when `--config` points at a settings file whose `sandbox` is not
`none`. It never infers sandboxing from the environment, which the model could influence;
the host writes the file before the container starts and the agent reads it once at start-up.

#### 3.16.3 `agent.json` (schema 1)

03 owns this schema; halcore renders it with `AgentSettings.to_json()` and passes it to the
session as a run file (06-core-cli; RESOLUTIONS E4, E5). It lands at
`$R/s/<sid>/run/agent.json`, which is `/run/hal/agent.json` in the box (read-only mount,
02-sandbox "Session runtime directory"). The agent reads it with `AgentSettings.from_json()`,
strict. With backend `none` and for the unboxed assistant the same object is used with host
socket paths (3.14.8). The agent finds the broker and the model endpoint only here.

```json
{"v":1,
 "session_id":"3f9a1c0b7e2d",
 "mode":"code",
 "toolset":"coding",
 "channel":"tui",
 "sandbox":"docker-runsc",
 "workspace":"/work", "host_root":"/home/me/proj", "scratch":"/tmp",
 "endpoint":{"url":"unix:///run/hal/model.sock","api":"openai"},
 "broker":"unix:///run/hal/broker.sock",
 "broker_call_timeout_s":3900,
 "model":"hal-code",
 "profile":{"family":"qwen35","values":{"context":32768,"max_output":8192,"temperature":0.6,"top_k":20,"top_p":0.95,"thinking":"on","reasoning_replay":"tool_turns","tool_format":"native","leak_parsers":["hermes","qwen_xml","json"],"parallel_tool_calls":false,"first_token_timeout_s":600,"system_role":"system","strict_alternation":false,"max_tools_hint":12}},
 "budget":{"interactive":{"max_tool_calls":100,"max_model_requests":200,"max_wall_s":1800},"task":{"max_tool_calls":50,"max_model_requests":120,"max_wall_s":900},"warn_calls":10,"max_calls_per_response":8,"max_consecutive_nudges":3,"loop_repeat_limit":3,"max_consecutive_tool_errors":6},
 "context":{"window":32768,"mask_at":0.6,"mask_to":0.4,"summarize_at":0.85,"keep_recent":3,"safety_tokens":512},
 "tools":{"read_max_lines":200,"read_max_chars":12000,"grep_max_matches":100,"grep_max_chars":8000,"glob_max":200,"run_timeout_s":120,"run_max_timeout_s":600,"run_head_chars":2000,"run_tail_chars":6000,"write_max_chars":200000,"max_result_chars":12000,"syntax_guard":true,"protect":[".git",".git/**"]},
 "permissions":"auto",
 "instructions":{"agents_md":true,"max_chars":16000,"user_file":"/run/hal/user/AGENTS.md"},
 "skills":{"enabled":true,"dirs":["/work/.agents/skills","/work/.claude/skills","/run/hal/skills"],"max_listed":12,"max_chars":3000},
 "mcp":{"servers":[{"name":"docs","command":"/usr/local/bin/docs-mcp","args":["--stdio"],"env":{},"cwd":"","timeout_s":60,"pinned":[],"tools_allow":[],"tools_deny":[]}],"project_servers":"box","restart_limit":1},
 "assistant":{"history_turns":10,"max_tool_rounds":5,"tools_per_turn":8,"claims_check":true,"log_conversation":true,"persona":"","user_name":"","timezone":"Europe/London"},
 "prompt":{"extra":""}}
```

Every key is required in the file (the host writes resolved values); unknown keys fail loudly.
`mode` is `code`, `assistant` or `headless`; `toolset` is `coding` or `assistant`; `channel`
is the channel name (`tui`, `voice`, `telegram`, `console`, `editor`, `headless`, `eval`).
`mcp.servers` lists only in-box servers (never `host = true` ones, which the broker runs) and
is empty for the assistant toolset and for `sandbox = "none"`. `context.window` is the
smaller of `[agent.context].window` (when not 0) and the gateway's `context_length` for the
model as halcore read it, or 0 when neither is known; the agent still applies 3.9.1 to it. The
`assistant` object is present for both toolsets and read only by the assistant.

### 3.17 Configuration keys

All in `~/.config/hal/config.toml` (path kept: hal-employment depends on it). Loaded with
`halkit`'s strict TOML loader, which fails loudly on a syntax error or an unknown key instead
of silently returning `{}` as `halcore.config.load` does today [verified:
`halcore/config.py:43-48`]. halcore reads these tables and renders `agent.json`; the in-box
agent never reads `config.toml`.

#### 3.17.1 `[agent]`

| Key | Default | Reason |
| --- | --- | --- |
| `model` | `"hal-code"` | The gateway's coding role alias (RESOLUTIONS D1). The default coding model is chosen by the coding eval; until then it is whatever the user's `hal-code` alias resolves to (RESOLUTIONS, settled questions). |
| `api` | `"openai"` | Chat Completions is the lowest common denominator; `responses` and `anthropic` are also allowed (the gateway serves all three, RESOLUTIONS D3). |
| `permissions` | `"auto"` | The box is the permission boundary (brief). |
| `thinking` | `""` | Empty: profile. |
| `broker_call_timeout_s` | 3900 | 3.13.2. |
| `cloud_jobs` | `false` | Read by halcore's `core_long_task`, not by halagent (RESOLUTIONS F2; 01-security REQ-SEC-54 treats a cloud job as `send`). Defined here because 03 owns `[agent]`. |

There is no `endpoint`, `endpoint_token_file` or `allow_host_run` key: the endpoint is always
the gateway (3.3.7, RESOLUTIONS G5) and there is no host shell (RESOLUTIONS G4). Because the
loader is strict, a leftover `allow_host_run` fails loudly (ATK-41). Transcripts always go to
`state_dir()/sessions/` (RESOLUTIONS E3), so there is no `sessions_dir` key either.

`[agent.overrides]`: any profile key (3.4.1), applied for every model.

#### 3.17.2 `[agent.budget]`, `[agent.context]`, `[agent.tools]`, `[agent.prompt]`, `[agent.skills]`

| Key | Default | Reason |
| --- | --- | --- |
| `budget.max_tool_calls` / `task_max_tool_calls` | 100 / 50 | 3.5.4 |
| `budget.max_model_requests` / `task_max_model_requests` | 200 / 120 | 3.5.4 |
| `budget.max_minutes` / `task_max_minutes` | 30 / 15 | 3.5.4 |
| `budget.warn_calls` | 10 | 3.5.4 |
| `budget.max_calls_per_response` | 8 | Bounds one runaway response. |
| `budget.max_consecutive_nudges` | 3 | gemma harness rule. |
| `budget.loop_repeat_limit` | 3 | 3.5.8 |
| `budget.max_consecutive_tool_errors` | 6 | Twice the nudge limit: errors carry information, nudges do not. |
| `context.window` | 0 | 0: profile and gateway decide (3.9.1). |
| `context.mask_at` / `mask_to` / `summarize_at` | 0.60 / 0.40 / 0.85 | Compaction at 14,336 of 32,768 with an 8,192 output cap is 0.59 of the input budget in the gemma harness (relayed); masking starts there, summaries only when masking is not enough. |
| `context.keep_recent` | 3 | Keeps the newest observations the model is acting on. |
| `context.safety_tokens` | 512 | Margin for template tokens the estimate misses. |
| `tools.*` | as in 3.16.3 | Caps from the gemma harness (`read_file` 150 lines / 10k, command output 5k) raised slightly for Hal's 32k default window; tk capped at 3k to stay under 5k (relayed). |
| `tools.protect` | `[".git", ".git/**"]` | Hooks and history are guarded by the read-only mount too; extra globs (for example `tests/**` in an eval) are user choices. |
| `prompt.extra` | `""` | Text appended to the system prompt. |
| `prompt.agents_md` | `true` | Open standard; the user's repos use it. |
| `prompt.agents_md_max_chars` | 16000 | About 4k tokens, an eighth of the default window. |
| `skills.enabled` / `max_listed` / `max_chars` | true / 12 / 3000 | 3.12.2 |
| `skills.user_dir` | `"~/.config/hal/skills"` | Brief. |

#### 3.17.3 `[agent.assistant]` and the `[brain]` mapping

`[agent.assistant]` replaces `[brain]` (00-overview marks `[brain]` deprecated and makes 03
the owner of the mapping):

| Key | Default | Note |
| --- | --- | --- |
| `model` | `"hal-chat"` | The gateway's chat role alias, which resolves `[hal].chat_model` (`gemma4:12b-it-qat`) (RESOLUTIONS D1). |
| `history_turns` | 10 | As today. |
| `max_tool_rounds` | 5 | As today. |
| `tools_per_turn` | 8 | As today. |
| `context` | 8192 | Was `num_ctx`; the measured baseline (74/76) used it. Capped by the gateway's `context_length`. |
| `temperature` | 0.4 | As today; the baseline used it. Overrides the profile for the assistant only. |
| `think` | `false` | Today's hard-coded `think: false` (voice latency), sent as thinking off on the OpenAI route. |
| `persona` | `""` | As today. |
| `claims_check` | `true` | The English-only guard can be turned off for other languages. |
| `log_conversation` | `true` | Calls `memory_log_conversation` through the broker after each turn, as today (3.8.2). |

How a user addresses Hal comes from `[hal].user_name` (00-overview), not from this table.

`map_brain()` (REQ-AGT-101), applied by `hal configure --migrate` (06-core-cli). Until the user
migrates, halcore reads `[brain]` through the same function and logs one warning per process.

| `[brain]` key | Becomes | Reason |
| --- | --- | --- |
| `model`, `history_turns`, `max_tool_rounds`, `tools_per_turn`, `temperature`, `persona`, `think` | same key in `[agent.assistant]` | Same meaning |
| `num_ctx` | `[agent.assistant].context` | The agent plans a window; the gateway owns the server's context |
| `user_name` | `[hal].user_name` when that is empty, else dropped | One source of truth (00-overview) |
| `keep_alive` | dropped | Model residency is the gateway's (05-gateway) |
| `ollama_url` | dropped | The agent reaches models only through the gateway (RESOLUTIONS G5); the gateway reads `[hal].ollama_url` |
| `deep_work = "off"` or `"hal"` or `"local"` | dropped (`[agent].cloud_jobs` stays `false`) | Long jobs are `core_long_task` (RESOLUTIONS F2) |
| `deep_work = "claude"` or `"cloud"` | dropped, `[agent].cloud_jobs` stays `false`, one warning naming the key | Cloud jobs are off by default and an explicit opt-in (RESOLUTIONS, settled questions; brief) |
| `deep_work_dir` | dropped, with a warning that names 06-core-cli's long-task settings | The long-task workspace belongs to `core_long_task` |
| anything else | error naming the key | Strict config |

hal-voice's own `brain:` overrides in its `config.yaml` stop applying, because hal-voice is now
an ACP client (08-packs-and-flagships); the same values go in `[agent.assistant]`.

#### 3.17.4 `[mcp]`

3.13.1.

### 3.18 Evaluation

#### 3.18.1 Assistant scenarios

03 owns the library and the task formats; `hal eval` (06-core-cli "Eval") wraps them and
supplies the runner (RESOLUTIONS F4). The case file is `eval/assistant/scenarios.toml` (00's
layout follows this path). It keeps today's schema [verified: header comment and key counts of
`core/hal/eval/scenarios.toml`: 50 cases, 12 `live`, so 38 run unattended]: `id`, `category`,
`ask` or `turns`, `needs`, `setup`, `tools`, `any_tools`, `forbid`, `no_tools`, `args`,
`confirm_first`, `reply`, `reply_not`, `max_chars`, `after`, `no_extensions`, `look_for`,
`live`, plus one new key, `approve` (bool, default false). Semantics that change:

- `confirm_first`: passes only when the broker requested approval for that tool during the
  turn (06-core-cli REQ-CLI-79); previously the tool returned `needs_confirmation`.
- `approve`: the runner's approver allows the approval requests of this case instead of
  denying them. A multi-turn case whose later turn used to say "yes" now sets `approve = true`;
  the "yes" text, if kept, is ordinary prompt text and approves nothing (REQ-AGT-98).

```python
@dataclass(frozen=True, slots=True)
class TurnOutcome:
    reply: str
    tools: Sequence[ToolUse]                 # name, args, ok, seconds, in call order
    approvals_requested: Sequence[str]       # tool names the broker asked about
    seconds: float
    first_words_s: float | None


class AssistantRunner(Protocol):
    """One fresh assistant session per case; implemented by halcore (06-core-cli)."""
    def run_case(self, case: Case, *, model: str, seed: int | None) -> Sequence[TurnOutcome]: ...


def run_scenarios(cases: Sequence[Case], runner: AssistantRunner, *, model: str,
                  repeat: int = 1, seed: int | None = None, out_dir: Path) -> Report: ...
```

halcore's runner opens the assistant through its normal ACP path with an in-process broker
inside a temporary Hal home, a generated policy that denies tiers `act`, `send` and `system`
and every network tool, and an approver that records requests and answers per `approve`
(06-core-cli REQ-CLI-78, RESOLUTIONS A2). halagent never builds a hub or a broker for eval.
`--model` may be any gateway model or alias. Reports go to the directory 06 names
(`state_dir()/eval/<YYYYmmddTHHMMSSZ>-<model slug>/`, REQ-CLI-80) with the fields of 5.2.

#### 3.18.2 Coding task set

```text
eval/coding/tasks/<id>/
  task.toml        metadata and the task text
  repo/            the starting repository (plain files; committed as a baseline at run time)
  hidden/          test files copied in only for grading
  gold.patch       a known-good fix (unified diff against repo/)
```

`task.toml`:

```toml
id = "csv-quote"
title = "CSV writer does not quote fields that contain commas"
kind = "bugfix"                 # bugfix | feature | refactor
task = """The function write_row in csvlite/writer.py produces a broken row when a field
contains a comma: 'a,b' and 'c' give 'a,b,c'. Fields that contain a comma, a quote or a
newline must be quoted as in RFC 4180."""
check = "python -m pytest -q hidden/test_writer.py"   # run with bash -c in the grading box
protected = ["tests/**", "hidden/**", "pyproject.toml", "conftest.py"]
timeout_s = 120
split = "dev"                   # dev | heldout
license = "MIT"
```

Initial set (12, synthetic, stdlib-only Python, written for Hal, MIT): `csv-quote`,
`leap-century` (leap years for 1900 and 2000), `lru-evict` (evicts the newest entry),
`ini-inline-comment` (`;` inside quotes), `backoff-cap` (delay not capped), `wrap-long-word`
(infinite loop on a word longer than the width), `semver-prerelease` (ordering),
`safe-join` (`..` escapes the base directory), `money-round` (float rounding; use
`Decimal` with `ROUND_HALF_UP`), `cli-limit` (feature: add `--limit N`), `rename-api`
(refactor across three files with a deprecated alias), `dedupe-stable` (order not kept).
Eight are `dev`, four `heldout`; held-out tasks are not used for tuning prompts or profiles.

Every task passes a gold/null check (REQ-AGT-94): the gold patch makes `check` pass, the
unchanged repo makes it fail. The gemma campaign had to filter its local set because only 41
of 129 public tasks passed their gold check (verified there), so the check runs in CI under the
`docker` marker.

#### 3.18.3 Coding runner

```python
class BoxRunner(Protocol):
    """Runs commands in a fresh sandbox; implemented by halcore on halsandbox (06-core-cli)."""
    def run_agent(self, workspace: Path, task: str, settings: AgentSettings,
                  timeout_s: float) -> HeadlessResult: ...
    def run_check(self, workspace: Path, command: str, timeout_s: float) -> CheckResult: ...


def run_coding(model: str, tasks: Sequence[CodingTask], *, repeat: int, seed: int,
               runner: BoxRunner, out_dir: Path) -> Report: ...
```

Per attempt:

1. Copy `repo/` to a temporary directory, `git init`, commit as baseline (tag
   `hal-eval-baseline`).
2. Run the headless agent in a fresh box (task mode, budget from settings, `protect` from the
   task).
3. Take the patch: `git add -N . && git diff hal-eval-baseline` (untracked files included).
4. Grade in a second fresh box: apply the patch to a clean copy of `repo/`, reset `protected`
   paths to the baseline, copy `hidden/`, run `check` with `timeout_s`. Grading outside the
   agent's box is the gemma lesson: the agent's tree is untrusted (`gemma-agent.md` 3.7).

Per-attempt record (`results.jsonl`, one line each):

```json
{"task":"csv-quote","split":"dev","repeat":0,"seed":1234,"model":"...","family":"...","resolved":true,"empty_patch":false,"stop_reason":"finished","wall_s":212.4,"model_requests":19,"tool_calls":14,"first_edit_call":6,"repaired_calls":1,"malformed_calls":0,"unknown_tools":0,"truncated_calls":0,"nudges":0,"compactions":0,"overflows":0,"peak_prompt_tokens":14210,"loop_refusals":0,"protected_edit_attempts":0,"check_exit":0,"patch_lines":7}
```

`summary.json`: model, family, profile values, settings hash, dates (UTC `Z`), counts per split,
resolved rate with Wilson interval, pass^k and pass@k per task, medians and p90 of wall time,
and the sums of every counter above.

#### 3.18.4 Statistics (`halagent.eval.stats`)

```python
def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]: ...
def mcnemar_exact(b: int, c: int) -> float: ...                 # two-sided p value
def paired_bootstrap(a: Mapping[str, Sequence[bool]], b: Mapping[str, Sequence[bool]], *,
                     iters: int = 10000, seed: int = 0) -> tuple[float, float, float]: ...
def pass_hat_k(c: int, n: int, k: int) -> float: ...             # C(c, k) / C(n, k)
def pass_at_k(c: int, n: int, k: int) -> float: ...              # 1 - C(n - c, k) / C(n, k)
def flip_rate(runs: Sequence[Mapping[str, bool]]) -> float: ...
def promote(b: int, c: int) -> bool: ...
```

- Wilson: centre `(p + z^2/2n) / (1 + z^2/n)`, half-width
  `z * sqrt(p(1-p)/n + z^2/4n^2) / (1 + z^2/n)`.
- Exact McNemar on discordant pairs (`b`: A passes, B fails; `c`: the reverse):
  `p = min(1, 2 * sum(C(b+c, i) for i in range(min(b, c) + 1)) / 2**(b+c))`.
- Paired bootstrap resamples tasks (clusters), not attempts, because repeats of one task are
  not independent; returns the mean difference and the 2.5% and 97.5% percentiles.
- `flip_rate`: share of cases whose outcome differs between two runs of the same
  configuration: the noise floor any A/B must beat.
- Promotion rule: `b - c >= ceil(sqrt(b + c))` and `b - c >= 2`, and the pre-registered
  mechanism metric moved as predicted (from `experiment-statistics.md`, relayed in
  `gemma-agent.md` 3.9).

#### 3.18.5 Discipline

- Noise is large: byte-identical payloads scored 0.18, 0.17 and 0.15; one local re-run went
  from 55 to 48 of 129 (relayed). At n = 38 a single run's minimum detectable effect is about
  17.6 points (relayed, `dev-notes.md`). The 2026-10-10 assistant comparisons are single runs,
  so differences of a few cases between models are not evidence.
- Every comparison runs both arms on the same cases, same repeats and same seeds where the
  server honours seeds, and reports `b`, `c`, the McNemar p value, the bootstrap interval and
  the flip rate of a repeated baseline (REQ-AGT-97).
- Default `--repeat 3` for comparisons; the coding set uses `--repeat 5` (12 tasks x 5).
- Each experiment has a card written before the run, `eval/cards/<YYYY-MM-DD>-<slug>.md` with:
  Parent, Lever, Change (one line), Hypothesis, Mechanism metric, Decision rule; Result and
  Decision filled in afterwards, kept when the experiment fails (batchjob practice).
- `hal eval compare A.json B.json` prints the paired table and refuses to name a winner unless
  the promotion rule holds; it says "no detectable difference" otherwise.
- Held-out tasks are run only to confirm a decision already taken on `dev`.

## 4. Failure modes

"Fail closed" here means: no action happens that policy has not allowed, and the user is told.
"Fail open" means: work continues with a weaker guarantee, which is stated.

| Layer | Failure | What happens | Open or closed |
| --- | --- | --- | --- |
| Transport | Endpoint refuses or socket missing | Retries (REQ-AGT-05), then `turn_end(error)` with reason `model_unreachable`; headless exit 3; ACP clients (voice, Telegram) get a JSON-RPC error on `session/prompt` and show their own "no model" text | Closed: no action |
| Transport | Server stalls | Timeouts (REQ-AGT-04); retry only if nothing visible was emitted; else `error`; the partial assistant message is kept with `meta.incomplete` | Closed |
| Transport | Socket path over 107 bytes | Configuration error at start naming the limit | Closed |
| Provider | Overflow error | Record `n_ctx`, compact, retry once; then `context_overflow` | Closed; files on disk and transcript kept |
| Provider | Unknown finish reason or malformed chunk | `notice`, treated as `stop`; malformed JSON chunks skipped up to 3, then `error` | Closed |
| Token counting | Count endpoint missing, refused (403 from the forwarder) or failing | Calibrated estimate with 1.15 factor | Open on accuracy: a request may still overflow, which the overflow path catches |
| Profile | Model not in the catalogue | `[defaults]` plus `notice` | Open on tuning, no security effect |
| Profile | Invalid `model-profiles.toml` | Start fails with file and key | Closed |
| Repair | Unreadable call | Malformed nudge; 3 in a row -> `nudge_limit` | Closed: nothing runs |
| Repair | Leaked call to an unoffered tool | Ignored; text stays visible | Closed |
| Loop | Identical call repeated | Third refused, fourth ends the turn | Closed |
| Loop | Budget exhausted | Turn ends with the budget reason; files kept | Closed |
| Loop | Unexpected exception | Logged with traceback, `turn_end(error)`, history kept (REQ-AGT-21); the broad `except Exception` carries the reason "one failing tool or parser must never end the session" | Closed |
| Tools | Path outside workspace or protected | `outside_workspace` / `protected` error | Closed |
| Tools | `run` timeout | Group killed, partial output returned | Closed |
| Tools | ripgrep missing | Python fallback, same caps | Open on speed only |
| Tools | No sandbox backend | `run` not offered (no opt-out), file tools confined in process, user MCP servers not marked `host` not started, prompt and UI say so | Closed for shell and MCP; file tools are confined by path checks only, which is weaker than a mount namespace (stated in the UI; residual risk R-new-8 in 01-security) |
| Context | Summary call fails | State note replaces old history | Open on detail, no data loss on disk |
| Context | Still over window | `context_overflow` | Closed |
| Broker | Socket missing in the box | Broker tools offered as unavailable; calls return `unavailable`; coding tools work | Closed: no exit |
| Broker | Approval denied or timed out | The broker answers `denied`; the model is told it did NOT happen and not to retry (REQ-AGT-54) | Closed |
| Broker | Broker unreachable (any toolset) | Its tools return `unavailable`; the assistant says the tool service is down; there is no in-process fallback (RESOLUTIONS A2) | Closed |
| Broker | No answer within `broker_call_timeout_s` | `notifications/cancelled`, `unavailable` result | Closed |
| Broker | Message over 4 MiB | Refused by the client; the call fails with `too_large` | Closed |
| MCP | Server fails to start or crashes | Its tools return `unavailable`; one restart; `notice` | Closed |
| MCP | Server needs the network inside the box | It fails; the doctor check and docs explain `host = true` | Closed |
| MCP | Entry with `url`, or a project or ACP entry asking for `host` | Config error (`url` in `config.toml`) or skipped with a `notice` (project, ACP) | Closed |
| MCP | Server sends an elicitation or sampling request | Answered with an error; no handler exists | Closed |
| ACP | Client closes stdin | Cancel the running turn, flush, exit 0 | Closed |
| ACP | Malformed message | JSON-RPC -32700 / -32600; the connection stays up | Closed |
| ACP | Permission request unanswered | `reject_once` after 3,600 s | Closed |
| Sessions | Transcript write fails (disk full) | `notice` with level error each turn; the session continues in memory; headless exits 1 at the end | Open on persistence, stated |
| Sessions | Torn last line | Skipped with a warning | Open on that one record |
| Assistant | Claims check misfires | One retry at most, then the reply is released | Open on wording, no action taken |
| Assistant | User types "yes" while nothing is pending, or the model claims it asked | Ordinary text; no approval exists outside the broker's host surfaces (REQ-AGT-98) | Closed |
| Eval | Gold or null check fails for a task | The task is excluded from the run with a loud warning; CI fails | Closed |

Per layer, summarised: providers, repair, loop, tools, broker client, MCP client and ACP fail
closed; token estimation, profile defaults and the summary step fail open on quality only.
Nothing in halagent fails open on isolation or policy: there is no host shell, no in-process
hub and no approval path through the agent.

## 5. Migration

### 5.1 What replaces what

| Today | Path | Replaced by |
| --- | --- | --- |
| Brain loop, Ollama-only `_post` and `_stream_round` | `core/hal/halcore/brain.py:180-257` | `halagent.providers.*` (through the gateway) and `halagent.loop.Agent` |
| `LEAKED` regex and `_leaked_call` | `brain.py:97-110` | `halagent.repair` (`name_json` parser plus seven more) |
| `_confirmed` stripping keyed by tool name, `self.pending` | `brain.py:136, 164-165, 190-195, 332` | broker approvals bound to the exact canonical arguments, rendered by host surfaces (01-security); the agent holds nothing |
| `_confirmed` property injected into schemas | `brain.py:161-165` | removed; REQ-AGT-35 strips it if a model invents it |
| `deep_work` with `claude -p` by default | `brain.py:203-227`, `config.py:34` | the broker tool `core_long_task` (06-core-cli); cloud only with `[agent].cloud_jobs = true` |
| Claims check and prompts | `brain.py:32-87, 260-331` | `halagent.assistant.claims`, `.prompts` (verbatim except 3.11.5) |
| `Brain` class, imported by hal-voice and hal-telegram through `sys.path` | `brain.py:113`, `halvoice/brain.py:43-51`, `haltelegram/config.py:68-73` | `hal acp --channel <name> --toolset assistant`; hal-voice and the Telegram pack are ACP clients (RESOLUTIONS A1, E1, B9; 08-packs-and-flagships). No shim |
| `evaluate.py` checks, placeholders | `core/hal/halcore/evaluate.py` | `halagent.eval.scenarios` (loader, checks, report); the sandbox and approver move to halcore's `AssistantRunner` (06-core-cli) |
| `eval/scenarios.toml` | `core/hal/eval/` | `eval/assistant/scenarios.toml` (same schema plus `approve`; ASCII fixes on the 3 non-ASCII lines) |
| `[brain]` table | `config.toml` | `[agent.assistant]` through `map_brain()` (3.17.3) |
| `config.load()` returning `{}` on errors | `halcore/config.py:43-48` | `halkit` strict loader |
| Local-time `datetime.now().isoformat()` in `last_turn.at` and reports | `brain.py:336`, `evaluate.py:223` | UTC `Z` timestamps |
| `turns.jsonl` appended by the brain | `brain.py` | transcripts in `state_dir()/sessions/<sid>/` written by the host from `_hal/record` |
| Coding through `exec claude` with env redirection | `hal:123-263` | `hal code` (06-core-cli) driving `hal-agent --acp` in the box; `hal claude` keeps the old path with a warning |
| Claude Code auto-compaction that `hal-route.py` triggers by rewriting overflow errors | `hal-route.py:446-456` | halagent's own window check and compaction (the rewrite stays in the gateway for Claude Code users, and halagent also reads it, 3.3.6) |

### 5.2 How existing users keep working

- hal-voice and the Telegram pack change in the same release: they spawn
  `hal acp --channel voice|telegram --toolset assistant` and speak ACP (08-packs-and-flagships
  owns those changes). The assistant they talk to has the same persona, prompts, claims check
  and tool selection; it runs in a box with a scratch `/work` instead of in their own process.
- `[brain]` keeps working until migrated: halcore maps it with `map_brain()` and warns once;
  `hal configure --migrate` writes `[agent.assistant]`. Dropped keys are listed with reasons
  in 3.17.3. The user's live `deep_work = "claude"` becomes "cloud jobs off" with a warning
  that names `[agent].cloud_jobs`.
- Destructive tools: the user still answers a question before the action runs, but the
  question now comes from Hal itself (TUI panel, Telegram buttons, the voice summary, `hal
  approve`) and shows the exact action; answering "yes" in chat text does nothing. Approvals
  can no longer be given by the model through `_confirmed`.
- Eval reports keep their file names and summary fields (`model`, `at`, `passed`, `total`,
  `median_seconds`, `median_first_words`, `results`) and add `wilson`, `repeat`, `seed`,
  `profile`, `api`.

### 5.3 Removed

- The `_confirmed` flag path for the model, in schemas and in the brain.
- The conversational yes matcher and every way for the agent to hold, replay or grant a call.
- The default of running long jobs on the user's cloud Claude Code login.
- Direct model access from the agent (`ollama_url`, an Ollama-native provider,
  `[agent].endpoint`).
- `halcore.brain`, `_siblings.py` and every `sys.path` import of Hal code from another repo.
- Silent fallbacks: broken config files, `warm()` swallowing errors, turn-log write failures
  ignored without a log line, an in-process hub when the broker is down.

### 5.4 Order of cut-over

1. halagent lands in `Hal.next`; `core/hal` in `hal-ecosystem` stays live, and hal-voice and
   Telegram keep using it until their ACP versions land.
2. The old `hal eval` (in `hal-ecosystem`) and the new `hal eval --suite assistant` run the
   same 38 cases with `--repeat 3`; `halagent.eval.report.compare` pairs the reports, and the
   new assistant must not lose by the promotion rule in reverse (that is, `c - b` must not pass
   the rule).
3. hal-voice and the Telegram pack switch to `hal acp` (08-packs-and-flagships).
4. The final cutover (brief) points the live units at the new Hal.

## 6. Test plan

### 6.1 Hermetic unit tests (default run, target under 60 s)

One test file per module: `test_messages.py`, `test_events.py`, `test_settings.py`,
`test_transport.py`, `test_openai_chat.py`, `test_responses.py`, `test_anthropic.py`,
`test_reasoning.py`, `test_tokens.py`, `test_profiles.py`, `test_repair_leaks.py`,
`test_repair_aliases.py`, `test_repair_coerce.py`, `test_tool_read.py`, `test_tool_grep.py`,
`test_tool_glob.py`, `test_tool_edit.py`, `test_tool_write.py`, `test_tool_run.py`,
`test_tool_todo_finish.py`, `test_tool_search.py`, `test_paths.py`, `test_loop.py`,
`test_budget.py`, `test_context.py`, `test_sessions.py`, `test_instructions.py`,
`test_skills.py`, `test_prompts.py`, `test_mcp_lite.py`, `test_mcp_client.py`,
`test_backends.py`, `test_acp_jsonrpc.py`, `test_acp_server.py`, `test_headless.py`,
`test_assistant.py`, `test_claims.py`, `test_eval_scenarios.py`, `test_eval_coding.py`,
`test_eval_stats.py`, `test_cli.py`.

Names are behaviour sentences, for example `test_third_identical_call_is_refused_with_loop_error`,
`test_gemma4_leaked_call_runs_and_is_removed_from_text`,
`test_unix_socket_path_over_107_bytes_fails_loudly`. Tests that the review and resolution
round added, with the finding each guards against (ATK numbers are those the security review
proposed for 01-security's attack list; 01 owns the final numbering):

| Test | Guards against |
| --- | --- |
| `test_broker_ask_call_waits_for_broker_answer`, `test_user_yes_text_is_not_an_approval` | A text yes approving a held call ("ok, but..."), security review F1, ATK-36 |
| `test_backend_none_refuses_non_host_mcp_servers` | Stdio MCP servers running on the host without policy, security review F4g, ATK-40 |
| `test_allow_host_run_key_is_rejected` | A host shell with no broker, security review F4e, ATK-41 |
| `test_no_halhub_import_anywhere` (root layering test) | The in-process hub fallback, consistency review A2 |
| `test_mcp_result_uses_security_envelope`, `test_broker_text_is_passed_through_unchanged` | Double or divergent untrusted wrappers, consistency review B11 |
| `test_count_endpoint_403_marks_missing` | A forwarder 403 surfacing as a session error, consistency review D3 |
| `test_every_provider_request_is_on_the_forwarder_allowlist` | `/api/*` or admin paths from the agent, REQ-SEC-66 |
| `test_assistant_toolset_has_only_broker_tools` | Coding tools in the assistant box, RESOLUTIONS A1 |
| `test_session_id_is_twelve_hex_from_host_meta` | Two session id formats, consistency review E2 |
| `test_url_mcp_entries_are_rejected` | HTTP MCP servers bypassing the broker, RESOLUTIONS F4 |
| `test_map_brain_drops_keys_with_reasons` | Silent loss of `[brain]` settings at migration |
| `test_lite_client_refuses_message_over_4_mib` | Mismatched message caps, consistency review E9 |

Fakes:

- `FakeModelServer`: `http.server` on a unix socket in `tmp_path` (short path via `chdir`
  because of the 107-byte limit), serving scripted SSE responses per API from
  `tests/fixtures/streams/<api>/<case>.txt`; can stall at connect, first byte or mid-stream,
  return overflow errors in llama.cpp's body shape and in the gateway's rewritten shapes, and
  answer 403 on paths outside the forwarder allowlist.
- `ScriptedProvider`: an in-process `Provider` that yields scripted `StreamItem`s per request
  and records each `ChatRequest`, for loop, context and assistant tests.
- `FakeBroker`: a newline JSON-RPC server on a unix socket implementing `initialize`,
  `tools/list`, `tools/call` (including `hal_find_tools` and `hal_call`) with scripted outcomes
  (`ok`, `denied`, `unavailable`, and a delayed answer that stands in for a pending approval)
  and `result._meta.hal` as 02-sandbox defines it. It has no grant API, because none exists.
- `fake_mcp_server.py`: a tiny stdio MCP server built on `mcp.server.mcpserver.MCPServer`
  (in-process transport where possible); one variant sends an elicitation request.
- `FakeClock` for budgets and retries; `FakeBoxRunner` and `FakeAssistantRunner` for the eval
  runners.
- `ACPTestClient`: drives `halagent.acp.server` (and `serve()` over `os.pipe()` pairs) over
  in-memory pipes and validates every message against
  `tests/fixtures/acp/schema-v1.26.0.json` with `jsonschema`.
- Repository fixtures: small git repos created in `tmp_path` by helpers (no binary fixtures).
- Path vectors: `packages/hal-kit/tests/data/path_cases.json` (owned by halkit, shared with
  02-sandbox).

Fixtures for leaked formats are built from the real templates' rendering rules (Gemma 4
`format_argument`, Mistral `[TOOL_CALLS]name[ARGS]`, harmony `to=functions.NAME
<|constrain|>json<|message|>`), and contain no real personal data.

Fuzz (REQ-AGT-33): `random.Random(20261023)` applies 5,000 mutations (truncation, character
deletion, duplication, bracket insertion, quote swaps) to every leak fixture; the test asserts
no exception and a total time under 5 s.

### 6.2 Marked tests

| Marker | Tests | Needs |
| --- | --- | --- |
| `live` | each provider against a real llama-server and an Ollama model through the gateway with a small model (`qwen3.5:0.8b` or `gemma4:e2b-it-qat`): one plain reply, one tool call, one reasoning reply, one cancel; token count comparison of estimate vs server; Gemma 4 history with tool calls renders | a running gateway and model server; uses the GPU only if the server already does (the test never starts one) |
| `docker` | REQ-AGT-49 non-dumpable check and broker reachability, each on runc and on runsc (runsc registered with `--host-uds=open`); a user stdio MCP server started in the box; coding gold/null checks; coding runner end to end with `ScriptedProvider` behind a fake model socket | docker |
| `slow` (with `live`) | assistant scenarios with a real model (`hal eval`), coding eval smoke on 2 tasks | a gateway and model server |
| `gpu` | none in this package | |

### 6.3 Manual

- `docs/howto/use-hal-with-zed.md`: Zed connects through `hal acp`, review mode prompts appear,
  cancel works.
- Voice (with hal-voice's ACP client): say "cancel book club on Friday"; the approval comes from
  Hal with the exact event; answer yes, hear the outcome; answer no in a second run and check
  the event remains; say "yes" when nothing is pending and check nothing happens.

## 7. Implementation plan

Each step is one commit, tests included and passing, subject in the Hal style (`<Verb>
<object>`, no article after the verb, at most 60 characters, no body).

| # | Commit subject | Content |
| --- | --- | --- |
| 1 | `Add hal-agent package skeleton` | `pyproject.toml`, `halagent/__init__.py`, `messages.py`, `events.py`, tests for (de)serialisation |
| 2 | `Add stdlib HTTP transport with unix sockets` | `providers/transport.py`, `errors.py`; SSE reader; timeouts; cancel; `FakeModelServer` |
| 3 | `Add retry with backoff to model transport` | `RetryPolicy`, `Retry-After`, no retry after visible text; fake clock |
| 4 | `Add Chat Completions provider` | `openai_chat.py`, `reasoning.py`; fixtures; forwarder allowlist check |
| 5 | `Add Anthropic Messages provider` | `anthropic.py`; thinking blocks; tool_result blocks |
| 6 | `Add Responses provider` | `responses.py`; stateless input; reasoning events |
| 7 | `Add server token counting and estimates` | `tokens.py`; 403 and 404 memo; calibration |
| 8 | `Add model profile catalogue and resolver` | `profiles/`, seeded `model-profiles.toml`, `meta.hal_profile`, strict validation |
| 9 | `Add leaked tool-call parsers` | `repair/leaks.py` with fixtures and the fuzz test |
| 10 | `Add tool name aliases and argument repair` | `repair/aliases.py`, `coerce.py`, `repair_completion` |
| 11 | `Add workspace path confinement` | `tools/paths.py`, `Workspace`, protected globs, shared path vectors |
| 12 | `Add read, grep and glob tools` | three tools, ripgrep and fallback paths |
| 13 | `Add edit and write tools` | tiers, syntax guard, atomic writes, diffs |
| 14 | `Add run tool with capped output` | process group, head and tail buffers, timeout, ANSI strip, box environment |
| 15 | `Add todo and finish tools` | plan parsing, plan event, free calls |
| 16 | `Add agent loop with budgets and watchdog` | `loop.py`, `budget.py`, nudges, stop reasons, stats |
| 17 | `Add cancellation across loop and tools` | cancel paths, synthetic results, history validator |
| 18 | `Add context window check and masking` | `context.py` fit, estimate, gateway window, overflow retry |
| 19 | `Add summary compaction and state note` | compaction prompt, state note, fallback |
| 20 | `Add session transcripts and resume` | `sessions.py` writer, store, replay, 12-hex ids |
| 21 | `Add session export to Markdown and JSONL` | export, secret masking |
| 22 | `Add coding system prompts` | templates, schedule maths, prompt lint |
| 23 | `Add AGENTS.md and CLAUDE.md loading` | `instructions.py` |
| 24 | `Add Agent Skills discovery` | `skills.py`, frontmatter parser |
| 25 | `Add stdlib MCP client for the broker` | `mcp/lite.py`, 4 MiB cap, `FakeBroker` |
| 26 | `Add broker tool backend` | `backends.py`: `BrokerBackend`, `_meta.hal` mapping, pass-through text |
| 27 | `Add MCP client for user servers` | `mcp/client.py` on the SDK, config merge, naming, envelope, backend `none` refusal |
| 28 | `Add tool search meta-tool` | `tools/search.py`, broker `hal_find_tools`, `hal_call` routing, `max_tools_hint` |
| 29 | `Add agent settings file and brain mapping` | `settings.py`: `agent.json` schema, strict loader, `map_brain()` |
| 30 | `Add ACP agent server` | `acp/`, vendored schema, conformance tests, path mapping, `--toolset` |
| 31 | `Add in-process ACP entry point` | `acp.serve()`, identical-message test against the CLI |
| 32 | `Add review mode permissions over ACP` | `session/set_mode`, `request_permission`, remembered choices |
| 33 | `Add transcript records over ACP` | `_hal/record`, `session/load` with host records, capability rule |
| 34 | `Add headless output schema and exit codes` | `headless.py`, renderer, 06's flags and exit codes, signals |
| 35 | `Add hal-agent command line` | `cli.py`: `--acp`, `-p`, `profiles`, `sessions`, `doctor` |
| 36 | `Add assistant toolset with claims check` | `assistant/`: prompts, gate, per-turn tool selection; ported brain tests |
| 37 | `Port scenario evaluation to halagent` | `eval/scenarios.py`, `AssistantRunner` protocol, `approve` key, scenarios move |
| 38 | `Add eval statistics and comparison` | `eval/stats.py`, `report.py`, `compare` |
| 39 | `Add coding task set` | 12 tasks with gold patches; docker gold/null test |
| 40 | `Add coding eval runner` | `eval/coding.py`, `BoxRunner` protocol, metrics |
| 41 | `Add hal-agent user and developer docs` | `docs/user/agent.md`, `docs/dev/agent.md`, how-tos with symptom tables, CHANGELOG |

Steps 1 to 35 build the coding agent; 36 and 37 add the assistant toolset and its eval; 38 to 41
add evaluation and docs. Steps 25 to 28 run against `FakeBroker` until 02-sandbox's broker
lands; the `_meta.hal` field names follow 02-sandbox (RESOLUTIONS E7).

## 8. Decisions

Choices made where the brief and `RESOLUTIONS.md` are silent, with reasons.

1. **Hand-rolled ACP, schema-validated.** 3.14.1. The SDK is 0.x with a pydantic dependency;
   the protocol surface Hal needs is small; conformance comes from the vendored schema.
2. **Two MCP clients.** The official SDK for third-party servers in the box (version
   negotiation, the 2026-07-28 stateless protocol), and a stdlib client for the broker, whose
   protocol Hal controls, so the broker path enforces its own framing and 4 MiB cap and does
   not move with SDK releases.
3. **One loop for both toolsets, with hooks.** The assistant's claims gate is a hook on the
   coding loop, so repair, budgets, context and providers are shared and tested once.
4. **Sequential execution of multiple calls.** No concurrency in one working tree; small
   models rarely emit several calls.
5. **Non-zero exit codes from `run` are not tool errors.** A failing test is information;
   marking it an error invites retries and trips the error watchdog.
6. **No-match `grep` is a success.** Same reason.
7. **The harness writes the state note, the model does not.** A model-written notes slot was
   killed in batchjob for delaying the first edit.
8. **Masking before summarising, one sweep at a time.** Cheaper, keeps code verbatim, and
   limits prefix-cache invalidation.
9. **`finish` only in task mode.** In an interactive turn the reply itself ends the turn; an
   extra tool would cost context and invite premature finishing.
10. **`todo`, `finish` and `hal_find_tools` are free.** Planning and ending must not compete
    with work for the budget (the gemma harness made `get_status` and `submit_patch` free).
11. **The host writes transcripts for halcore sessions** through `_hal/record`, because the
    box has no writable path the host reads (`/run/hal` is read-only) and a file under `/work`
    would be model-writable and land in the user's project.
12. **The agent maps host and box paths itself, idempotently**, so the TUI (no proxy) gets host
    paths and the `hal acp` proxy may translate again without harm.
13. **Profiles use vendor sampling defaults**, except the assistant keeps temperature 0.4 and
    thinking off, the settings of its only measured baseline.
14. **`tool_format = "prompt"` fallback** for models without native tool support, parsed with
    the Hermes parser, so "works across many models" includes models whose server has no tool
    parser.
15. **`deep_work = "claude"` maps to cloud jobs off**, with a warning. RESOLUTIONS makes cloud
    jobs off by default and the brief makes cloud an explicit opt-in; the old value was a
    template default, not a choice made with the new risk in view.
16. **The assistant has no host-tool path except the broker** (RESOLUTIONS A2): a broker that
    is down means no tools, stated in the reply, rather than tools without the broker's rate
    limits, taint, guard and audit.
17. **ATIF export deferred.** Markdown and JSONL cover sharing and tooling now; ATIF v1.7 is a
    candidate once the coding eval needs trajectory exchange.
18. **Review mode exists but is not a security boundary.** It is labelled as a way to watch the
    agent; the box and the broker are the boundaries.
19. **`.claude/skills` is read** in addition to the brief's two skill locations, so repositories
    set up for Claude Code work unchanged.
20. **Schedule numbers scale as fractions of the gemma schedule.** The only evidence is at N =
    28; the coding eval is how this gets checked.
21. **Gateway interface used:** the forwarder allowlist routes only (01-security REQ-SEC-66 as
    amended by RESOLUTIONS D3), `context_length` and optional `meta.hal_profile` in `GET
    /v1/models` (05-gateway), and the gateway's overflow rewrites. No Ollama-native path and
    no direct model URL.
22. **No Streamable HTTP user MCP servers in v1.** The box has no network, and with backend
    `none` an HTTP server would be reached without the broker. Broker upstreams take an argv
    (02-sandbox), so HTTP servers would need a stdio adapter there; that is left for later.
23. **Only `config.toml` can mark an MCP server `host`.** A project `.mcp.json` and ACP
    `mcpServers` are under the model's or editor's control; letting them request host
    execution would let a repository move code out of the box.
24. **Headless exit codes follow the ACP stop reason**, so `hal -p` (which sees only ACP) and
    `hal-agent -p` agree; the finer halagent reason stays in `stop_reason` and `subtype`.
25. **The session id comes from the host when it has one** (`_meta.hal.sessionId`), because a
    long-lived assistant box serves many ACP sessions and the transcript directory must match
    halcore's `meta.json`.

## 9. Open questions

1. **Coding task set.** Is a 12-task synthetic set enough for v1, or do you want a subset of a
   public SWE-style benchmark added (licence and size to check; the gemma campaign's tasks are
   competition data and cannot be used)?
