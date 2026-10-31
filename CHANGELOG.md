# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions follow semver; every package
carries the same version.

## 0.1.0 - unreleased

First release of the monorepo (uv workspace on branch `next`).

### Added

- `hal remote setup` pairs a laptop with the desktop (bootstrap or `--manual`), `hal remote up`,
  `down` and `status` run the SSH tunnel on Linux (systemd or detached) and Windows, and
  `docs/howto/` has pages for a Linux laptop, a Windows laptop and Tailscale.
- `[agent].repo_map_auto` (off by default) puts the repo map in the system prompt once per
  session. `/image` stays inside the workspace unless `--outside` is given; the IDE socket caps
  editor connections.
- `hal connect --write` for claude and qwen (diff, y/N, backup) and a notice when it turns on the
  gateway TCP listener.
- IDE integration: `hal ide setup zed|jetbrains|neovim|emacs|vscode`, ACP file access served from
  the isolated copy, edits sent as ACP diffs, a per-session host socket in `hal code`
  (`/ide`, `@path#L10-20` chips) and a VS Code extension in `ide/vscode/`.
- `hal code` and `hal chat` terminal UI: rich markdown and diff rendering, a prompt_toolkit editor
  (history, completion, `@file`, `!cmd`, `# note`), status line, approval panel, more slash
  commands and `[ui]` settings (`docs/user/hal-code.md`).
- `hal-agent` for local models: context usage and compaction updates, prompt-cache friendly
  prefix, `patch`, `repo_map` and `git` tools, a compact tool set, grammar and JSON-schema
  constrained tool calls, profiles for Qwen2.5-Coder, Devstral, DeepSeek and Llama, named
  subagents, model roles with an architect/editor split, `@image` prompts, prompt-submit hooks,
  session fork and first-prompt titles.
- `hal` command: `hal doctor` (`--only`, `--fix`), `hal migrate` with dry run and undo,
  `hal connect`, `hal packs` with a pinned catalogue, grouped `hal help` and `hal version`, and an
  idempotent `scripts/install.sh`. `scripts/host-setup.sh` (sudo, `--dry-run`)
  registers `runsc` with `--host-uds=open` and writes a key-only sshd drop-in with `--ssh`.
- `hal configure --profile` picks `max`, `24gb`, `12gb` or `cloud` from the GPU and RAM and writes it to `[hal].profile`.
- `hal-kit` (`halkit`): paths, config, files, time, logging, subprocess, secrets, redaction,
  canonical JSON, terminal escaping, symlink-safe file access, egress URL checks, test fakes.
- `hal-hub`: tool registry, runner, MCP server, policy with tool tiers, approvals bound to exact
  arguments, risk classifier with paranoia levels, local prompt-injection guard, audit log.
- `hal-sandbox`: the box (docker with runc or runsc, bubblewrap), broker, isolated coding copy
  with `accept` and `export`, checkpoints, scratchpad, lockdown levels, `hal box`.
- `hal-agent`: boxed coding agent with providers, tool-call repair, sessions, headless mode and
  an ACP server.
- `hal-gateway`: local model gateway with presets, routing, gpu-light claims, remote backends
  and a client-token admin interface.
- `hal-remote`: using the desktop's Hal from a laptop over SSH (forced command, authorize and
  revoke, server-check, tunnel supervisor); pairing and the `up`/`down`/`status` commands are
  still to come.
- Containment test suite (`tests/e2e/test_containment*.py`): a hostile process in the real box
  under runc and runsc; every attack is contained. See [docs/user/security.md](docs/user/security.md).
- No-telemetry test (`tests/test_no_telemetry.py`) and a "No telemetry" README section.

### Security

- The model is treated as hostile: every agent turn runs in a box with no network, a copy of
  the project and a scratchpad. Lockdown and paranoia can be changed only on the host.
- Security hardening: nested tool arguments are scanned without a depth limit, the broker, egress
  proxy and model forwarder have send timeouts and budgets, more credential paths are hard-denied,
  and `hal code export` refuses to overwrite a file without `--force`.
