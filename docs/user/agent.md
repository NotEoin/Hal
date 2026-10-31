# The Hal agent

`hal-agent` is Hal's coding agent: a loop that asks a model for tool calls, runs them in a
workspace and repeats until the task is done. It talks to models only through the Hal gateway
and reaches host tools only through the tool broker. The design is in
[03-agent](../design/03-agent.md).

Status: the library and command line described here are built and tested against fake model
servers and a fake broker. Nothing has been run against a real model, gateway or box. The agent
does not create a box itself: it is boxed only when `hal` (06-core-cli) starts it in one.

## Commands

| Command | What it does |
| --- | --- |
| `hal-agent` | Interactive prompt in the current directory (type `/exit` to leave) |
| `hal-agent -p TASK` | Runs one task and exits; `-p -` reads the task from stdin |
| `hal-agent -p TASK --json` | Prints one result object; `--stream-json` prints one object per line |
| `hal-agent --acp` | Serves the Agent Client Protocol on stdin and stdout |
| `hal-agent --toolset assistant` | Offers host tools from the broker only, no file or shell tools (with `--config`, the file's toolset wins and a different flag is an error) |
| `hal-agent --config FILE` | Takes endpoint, model, broker, budgets, context fractions, tool limits, instructions, skills, MCP servers, permissions and profile from a host-written `agent.json` |
| `hal-agent profiles list` / `profiles show MODEL` | Lists model families, or prints the resolved profile with the source of each value |
| `hal-agent sessions [list]` | Lists stored sessions (`--cwd DIR`, `--all`, `--limit N`) |
| `hal-agent sessions show ID` | Prints a short overview of a session |
| `hal-agent sessions export ID` | Prints Markdown (`--format jsonl` for records, `--reasoning`, `--out FILE`) |
| `hal-agent doctor` | Checks config, gateway, profile, broker, `rg` and `bash`; exit 1 if a check fails |

Resume a session with `--resume ID` or `--continue`. `--no-session` writes no transcript. See
[resume and export a session](../howto/resume-and-export-a-session.md) and
[use the agent from an editor over ACP](../howto/use-the-agent-from-an-editor-over-acp.md).

## Wire API

Set `api` in `[agent]` (or `HAL_AGENT_API`, or `endpoint.api` in `agent.json`) to `openai` (default,
Chat Completions), `responses` (stateless, `store: false`) or `anthropic` (Messages). The last two
run without streaming only; `stream = true` with them is an error.

## Limits come from the model

When the gateway lists the model, its `context_length` sets the context window and its
`meta.hal_profile` picks the model profile. Your own `context_window`, `max_output` or
`tool_format` setting wins over both. Profiles set sampling, tool-call repair order, how the
system message is sent (`system_role`) and whether consecutive user messages are merged
(`strict_alternation`). `hal-agent profiles show MODEL` prints the result. Shipped profiles cover Gemma 4, Qwen 3.x,
Qwen2.5-Coder and Qwen3-Coder, gpt-oss, Devstral and other Mistral models, GLM 4.x, DeepSeek, Llama
3.x, Granite and Phi. Add your own in `~/.config/hal/model-profiles.toml`. The key
`tool_constraint = "grammar"` (or `"json_schema"`) makes llama.cpp enforce well-formed tool calls
for models that use `tool_format = "prompt"`.

If the server answers `context_length_exceeded`, the agent plans for 25 percent less input,
compacts the history and retries once. A second overflow ends the turn with `context_overflow`.

## Host tools

With a broker socket (`--broker`, `HAL_AGENT_BROKER` or `agent.json`) the agent offers the
broker's tools by name, up to the profile's `max_tools_hint`, plus `hal_find_tools`. The rest
are found with `hal_find_tools`; a found tool can be called by name for the rest of the session
(at most 16 at once). Approvals are decided by the host, never by the agent or the model. A
denied or failed call comes back to the model with a note that it did not happen.

## Subagents

The agent has a `subagent` tool. A subagent starts with an empty context, does one task and returns a
short report, so a big search does not fill the main conversation. Tasks separated by a line of three
dashes run in parallel (at most 4). A subagent is read-only unless the call sets `tools = "all"`, which
works only in auto-edit mode. A subagent gets no host tools and cannot start subagents.

## Hooks

Hooks are commands that run inside the box around tool calls. Put them in `~/.config/hal/hooks.toml`
or `.hal/hooks.toml` in the project (the same `[[hook]]` tables also work inside
`.hal/settings.toml`); they are read once when a session starts.

```toml
[[hook]]
event = "pre_tool"          # pre_tool, post_tool, user_prompt_submit, session_start or session_stop
tools = ["edit", "write"]   # optional; no list means every tool
command = "sh scripts/guard.sh"
timeout = 30                # seconds, 1 to 120
```

The command is split into words and run without a shell, so use a script for pipes or redirects.
It gets the call as JSON on stdin. A `pre_tool` hook that exits 2 blocks the call and its
stderr is shown to the model. A `user_prompt_submit` hook that exits 2 refuses the prompt
before the model sees it. A `post_tool` hook that fails appends its stderr to the result. A hook
that times out or cannot start is logged and ignored. A broken file is skipped with a warning.
A hook can reach only what the boxed agent can reach. The project file sits in the working copy, so
the model can edit it, but the change applies only from the next session.

## Exit codes of `-p`

| Code | Meaning |
| --- | --- |
| 0 | Task finished |
| 1 | Error while running |
| 2 | Bad usage or configuration |
| 3 | Model server unreachable |
| 4 | A budget, loop or nudge limit ended the task |
| 124 | `--timeout` reached |
| 130 | Cancelled by a signal |

See also [the setup check how-to](../howto/check-the-agent-setup.md).

## Run files for a box session

`halcore.settings` writes `agent.json` (every value resolved on the host, including the model's
context window so masking and summaries fit a 16k to 64k local model) and copies
`~/.config/hal/AGENTS.md` and `skills/` into the box as read-only run files. Files over 64 KiB, links
and anything past 1 MiB in total are skipped with a log warning. Verified by
`packages/hal-core/tests/test_settings.py`, which loads the output with the agent's strict loader.
