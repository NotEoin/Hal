# Hal documentation

Documents are split by audience: user docs say how to use Hal, how-tos walk through one task,
developer docs say how Hal works, and design specs hold the numbered requirements the code is
tested against. Folders `user/`, `howto/` and `dev/` appear here as their first pages are
written.

| Document | What it is |
| --- | --- |
| [design/README.md](design/README.md) | Index of the design specs |
| [user/agent.md](user/agent.md) | The `hal-agent` commands, limits from the model, host tools and exit codes |
| [user/gateway.md](user/gateway.md) | The model gateway: listeners, commands, configuration, pinning, remote backends |
| [user/modes.md](user/modes.md) | The llama.cpp modes: what each runs, how to switch, measurements |
| [user/security.md](user/security.md) | What Hal protects you from, the lockdown and paranoia tables, and which paths are not private |
| [user/hal-code.md](user/hal-code.md) | A tour of the `hal code` and `hal chat` terminal UI: keys, streaming, approvals, slash commands, `[ui]` settings |
| [user/tui.md](user/tui.md) | Headless `hal -p` and `hal sessions` |
| [user/sandbox.md](user/sandbox.md) | The box: lockdown levels, what it sees, getting changes out, checkpoints |
| [user/cli.md](user/cli.md) | The `hal` command: exit codes, `hal doctor` and `hal migrate` |
| [howto/approve-tool-calls.md](howto/approve-tool-calls.md) | How to see why a call asks, answer it, and make a policy rule last |
| [howto/check-the-agent-setup.md](howto/check-the-agent-setup.md) | How to run `hal-agent doctor`, read its checks and fix each symptom |
| [howto/move-from-hal-ecosystem.md](howto/move-from-hal-ecosystem.md) | How to move from the old hal-ecosystem layout with `hal migrate`, and undo it |
| [howto/connect-a-harness-to-hal-tools.md](howto/connect-a-harness-to-hal-tools.md) | How to give Claude Code or another MCP client Hal's tools, on stdio or HTTP, and answer approvals |
| [howto/enable-local-guard.md](howto/enable-local-guard.md) | How to install, time and turn on the local prompt-injection guard |
| [howto/resume-and-export-a-session.md](howto/resume-and-export-a-session.md) | How to list, resume and export `hal-agent` sessions |
| [howto/use-the-agent-from-an-editor-over-acp.md](howto/use-the-agent-from-an-editor-over-acp.md) | How to run `hal-agent --acp` for an editor and use review mode |
| [howto/run-an-agent-cli-in-the-box.md](howto/run-an-agent-cli-in-the-box.md) | How to run claude, qwen, opencode, codex, crush or goose in the box with `hal box` |
| [howto/undo-agent-changes.md](howto/undo-agent-changes.md) | How to review, reject or roll back what an agent changed |
| [howto/connect-a-harness-to-the-gateway.md](howto/connect-a-harness-to-the-gateway.md) | How to point a harness at the local gateway with a client token |
| [howto/run-a-second-hal-install.md](howto/run-a-second-hal-install.md) | How to run two gateways side by side with separate state, ports and claim names |
| [howto/add-a-cloud-backend.md](howto/add-a-cloud-backend.md) | How to opt in to an https cloud backend and who may reach it |
| [howto/pin-a-model.md](howto/pin-a-model.md) | How to pin an Ollama model or a weight file in `models.toml` |
| [howto/install-gvisor.md](howto/install-gvisor.md) | How to register runsc with `--host-uds=open` so the box can use gVisor |
| [howto/prepare-project-dependencies.md](howto/prepare-project-dependencies.md) | How to install project dependencies with `hal-sandbox deps` |
| [howto/use-hal-code-in-zed.md](howto/use-hal-code-in-zed.md) | How to run the boxed agent from Zed or another ACP editor with `hal acp` |
| [howto/use-hal-code-in-jetbrains.md](howto/use-hal-code-in-jetbrains.md) | How to add Hal as a custom ACP agent in JetBrains AI Assistant |
| [howto/use-hal-code-in-neovim.md](howto/use-hal-code-in-neovim.md) | How to connect CodeCompanion, avante.nvim or Emacs agent-shell to `hal acp` |
| [howto/use-hal-code-in-vscode.md](howto/use-hal-code-in-vscode.md) | How to build the Hal extension for VS Code, VSCodium or Cursor and share selections and diffs with `hal code` |
| [howto/use-hal-from-a-laptop.md](howto/use-hal-from-a-laptop.md) | How to pair a Linux laptop with the desktop's Hal and keep the SSH tunnel up |
| [howto/use-hal-from-a-windows-laptop.md](howto/use-hal-from-a-windows-laptop.md) | How to pair a Windows laptop, run the tunnel without a console and start it at logon |
| [howto/use-hal-away-from-home.md](howto/use-hal-away-from-home.md) | How to reach the desktop's Hal from outside the home network with SSH over Tailscale |
| [howto/write-a-tool-pack.md](howto/write-a-tool-pack.md) | How to write, install and check a tool pack with `hal-hub new` |
| [dev/agent.md](dev/agent.md) | Agent developer notes: modules, limit precedence, records over ACP, what is not built |
| [dev/gateway.md](dev/gateway.md) | Gateway developer notes: configuration, listeners, relay, aliases, Ollama backend |
| [dev/manifest.md](dev/manifest.md) | Tool manifest (`hal-module.toml`) reference: every table and key, placeholders, v1 rules |
| [dev/remote.md](dev/remote.md) | Remote developer notes: running the sshd tests (the protocol and threat model follow) |
| [dev/sandbox.md](dev/sandbox.md) | Sandbox developer notes: modules, adding a backend, tests, what is not built |
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
