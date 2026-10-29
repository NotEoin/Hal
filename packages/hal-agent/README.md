# hal-agent

The Hal agent: providers, loop, tool-call repair, toolsets, sessions and the ACP server. Import name: `halagent`.

Status: skeleton. The package installs and carries its version; behaviour lands in later
phases of the implementation plan in
[docs/design/00-overview.md](../../docs/design/00-overview.md).

Run the tests from the repository root:

```sh
uv sync --all-packages
uv run pytest packages/hal-agent
```
