# hal-agent

The Hal coding agent. Import name: `halagent`. Design: [docs/design/03-agent.md](../../docs/design/03-agent.md).

Status: essentials only. Implemented: OpenAI-compatible chat (TCP or unix socket, optional
streaming), native tool calls with a text fallback, workspace-confined tools (`read_file`,
`write_file`, `edit_file`, `list_files`, `grep`, `run_command`), turn and tool-call limits, and
an MCP client for the hub socket. Not implemented: sessions, compaction, ACP, profiles, repair.

## Usage

```sh
hal-agent -p "add a test for foo" -C ~/proj --endpoint http://127.0.0.1:8741 --model qwen   # one shot
hal-agent -C ~/proj          # REPL; /exit quits
```

Settings come from flags, then `HAL_AGENT_*` environment variables, then the `[agent]` table:
`endpoint` (URL or unix socket path; default `gateway.sock` in the runtime dir), `model`,
`api_key`, `stream`, `tool_format` (`native` or `prompt`), `max_turns` (30), `max_tool_calls`
(100), `broker` (hub MCP socket; empty means no external tools).
`run_command` runs on the host here: use a throwaway workspace.

Run the tests from the repository root:

```sh
uv sync --all-packages
uv run pytest packages/hal-agent
```
