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

Resume a session with `--resume ID` or `--continue`. `--no-session` writes no transcript.

## Limits come from the model

When the gateway lists the model, its `context_length` sets the context window and its
`meta.hal_profile` picks the model profile. Your own `context_window`, `max_output` or
`tool_format` setting wins over both. Profiles set sampling, tool-call repair order, how the
system message is sent (`system_role`) and whether consecutive user messages are merged
(`strict_alternation`). `hal-agent profiles show MODEL` prints the result.

If the server answers `context_length_exceeded`, the agent plans for 25 percent less input,
compacts the history and retries once. A second overflow ends the turn with `context_overflow`.

## Host tools

With a broker socket (`--broker`, `HAL_AGENT_BROKER` or `agent.json`) the agent offers the
broker's tools by name, up to the profile's `max_tools_hint`, plus `hal_find_tools`. The rest
are found with `hal_find_tools`; a found tool can be called by name for the rest of the session
(at most 16 at once). Approvals are decided by the host, never by the agent or the model. A
denied or failed call comes back to the model with a note that it did not happen.

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
