# hal-agent

The Hal coding agent. Import name: `halagent`. Design: [docs/design/03-agent.md](../../docs/design/03-agent.md).

Status: essentials only. Implemented: OpenAI-compatible chat (TCP or unix socket, optional
streaming), native tool calls with a text fallback, workspace-confined tools (`read`, `grep`, `glob`, `edit`, `write`, `run`, `todo`, `finish`),
turn and tool-call limits, and
an MCP client for the hub socket. Tool-call repair (leaked-call parsers, name and argument aliases, coercion) is in
`halagent.repair`. Sessions are JSONL transcripts under `state_dir()/sessions/<id>/` (`--resume ID`, `--continue`,
`--no-session`, `hal-agent sessions`, `hal-agent export ID [--format md|jsonl]`); argument values
named like secrets are written as `***`. `AGENTS.md` (else `CLAUDE.md`) files from the workspace root to the working directory, the
user file `~/.config/hal/AGENTS.md` and Agent Skills (`.agents/skills`, `.claude/skills`,
`~/.config/hal/skills`) go into the system prompt. Context is kept under `context_window - max_output` tokens: old tool output is masked,
then summarised by the model (falling back to a harness-written state note). `hal-agent --acp` serves the Agent Client Protocol on stdio: `initialize`, `session/new`,
`session/prompt` (streams `session/update`), `session/cancel`, `session/set_mode` (only `auto`).
Not implemented: ACP `session/load`, review mode and `_hal/record`; model profiles.

## Usage

```sh
hal-agent -p "add a test for foo" -C ~/proj --endpoint http://127.0.0.1:8741 --model qwen   # one shot
hal-agent -C ~/proj          # REPL; /exit quits
hal-agent -p - --json < task.txt        # task from stdin, one JSON result (or --stream-json)
hal-agent profiles show gemma4:12b      # resolved model profile with the source of each value
```

Settings come from flags, then `HAL_AGENT_*` environment variables, then the `[agent]` table:
`endpoint` (URL or unix socket path; default `gateway.sock` in the runtime dir), `model`,
`api_key`, `stream`, `tool_format` (`native` or `prompt`), `max_turns` (30), `max_tool_calls`
(100), `broker` (hub MCP socket; empty means no external tools).
`run` runs on the host here: use a throwaway workspace.

Run the tests from the repository root:

```sh
uv sync --all-packages
uv run pytest packages/hal-agent
```

## Headless exit codes

`-p` exits 0 on success, 1 on a model or internal error, 2 on bad usage or configuration, 3 when
the model endpoint is unreachable, 4 when a limit, loop or nudge limit ends the turn, 5 on a
refusal, 124 on `--timeout` and 130 on SIGINT or SIGTERM. Verified by `tests/test_headless.py`.
