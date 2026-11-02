# Design specs

Specs for the Hal 0.1.0 release. Each states numbered requirements (`REQ-<PREFIX>-<nn>`), the
design, a test plan and an implementation plan. The topic each spec owns, and the requirement
prefix it uses, are listed once in the "Requirements index" of
[00-overview.md](00-overview.md); this page only lists the files.

| Document | What it is |
| --- | --- |
| [00-overview.md](00-overview.md) | The whole system: repos, packages, `halkit`, flows, configuration, ports, release rules |
| [01-security.md](01-security.md) | Threat model, tool tiers, policy, approvals, guard, audit |
| [02-sandbox.md](02-sandbox.md) | Container and bubblewrap backends, broker, sockets, checkpoints, `hal box` |
| [03-agent.md](03-agent.md) | The agent library: providers, loop, tool-call repair, sessions, ACP server |
| [04-hub.md](04-hub.md) | The tool hub: manifest v2, registry, runner, MCP server |
| [05-gateway.md](05-gateway.md) | The local model gateway, presets, routing and gpu-light claims |
| [06-core-cli.md](06-core-cli.md) | The `hal` command, terminal UI, headless mode, units and installs |
| [07-remote.md](07-remote.md) | Using the desktop's Hal from a laptop over SSH |

The pack list and the hal-memory and hal-voice designs (spec 08) belong to the hal-tools repo.
