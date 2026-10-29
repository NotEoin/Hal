# Hal documentation

Documents are split by audience: user docs say how to use Hal, how-tos walk through one task,
developer docs say how Hal works, and design specs hold the numbered requirements the code is
tested against. Folders `user/`, `howto/` and `dev/` appear here as their first pages are
written.

| Document | What it is |
| --- | --- |
| [design/README.md](design/README.md) | Index of the design specs |
| [howto/connect-a-harness-to-hal-tools.md](howto/connect-a-harness-to-hal-tools.md) | How to give Claude Code or another MCP client Hal's tools, on stdio or HTTP, and answer approvals |
| [howto/write-a-tool-pack.md](howto/write-a-tool-pack.md) | How to write, install and check a tool pack with `hal-hub new` |
| [dev/manifest.md](dev/manifest.md) | Tool manifest (`hal-module.toml`) reference: every table and key, placeholders, v1 rules |
| [dev/threat-model.md](dev/threat-model.md) | Threat model: assets, adversaries, trust boundaries, threats with their mitigations, residual risks |
| [dev/worker-protocol.md](dev/worker-protocol.md) | The JSON-lines protocol between the tool hub and a tool worker, and the v1 one-shot protocol |
| [design/00-overview.md](design/00-overview.md) | System overview: repos, packages, `halkit`, flows, configuration, ports, release plan |
| [design/01-security.md](design/01-security.md) | Threat model, tool tiers, policy, approvals, guard, audit |
| [design/02-sandbox.md](design/02-sandbox.md) | Container and bubblewrap backends, broker, sockets, checkpoints, `hal box` |
| [design/03-agent.md](design/03-agent.md) | The agent library: providers, loop, tool-call repair, sessions, ACP server |
| [design/04-hub.md](design/04-hub.md) | The tool hub: manifest v2, registry, runner, MCP server |
| [design/05-gateway.md](design/05-gateway.md) | The local model gateway, presets, routing and gpu-light claims |
| [design/06-core-cli.md](design/06-core-cli.md) | The `hal` command, terminal UI, headless mode, units and installs |
| [design/07-remote.md](design/07-remote.md) | Using the desktop's Hal from a laptop over SSH |
