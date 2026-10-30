# Hal documentation

Documents are split by audience: user docs say how to use Hal, how-tos walk through one task,
developer docs say how Hal works, and design specs hold the numbered requirements the code is
tested against. Folders `user/`, `howto/` and `dev/` appear here as their first pages are
written.

| Document | What it is |
| --- | --- |
| [design/README.md](design/README.md) | Index of the design specs |
| [user/agent.md](user/agent.md) | The `hal-agent` commands, limits from the model, host tools and exit codes |
| [user/security.md](user/security.md) | What Hal protects you from, the lockdown and paranoia tables, and which paths are not private |
| [howto/approve-tool-calls.md](howto/approve-tool-calls.md) | How to see why a call asks, answer it, and make a policy rule last |
| [howto/check-the-agent-setup.md](howto/check-the-agent-setup.md) | How to run `hal-agent doctor`, read its checks and fix each symptom |
| [howto/connect-a-harness-to-hal-tools.md](howto/connect-a-harness-to-hal-tools.md) | How to give Claude Code or another MCP client Hal's tools, on stdio or HTTP, and answer approvals |
| [howto/enable-local-guard.md](howto/enable-local-guard.md) | How to install, time and turn on the local prompt-injection guard |
| [howto/resume-and-export-a-session.md](howto/resume-and-export-a-session.md) | How to list, resume and export `hal-agent` sessions |
| [howto/use-the-agent-from-an-editor-over-acp.md](howto/use-the-agent-from-an-editor-over-acp.md) | How to run `hal-agent --acp` for an editor and use review mode |
| [howto/write-a-tool-pack.md](howto/write-a-tool-pack.md) | How to write, install and check a tool pack with `hal-hub new` |
| [dev/agent.md](dev/agent.md) | Agent developer notes: modules, limit precedence, records over ACP, what is not built |
| [dev/gateway.md](dev/gateway.md) | Gateway developer notes: configuration, listeners, relay, aliases, Ollama backend |
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
