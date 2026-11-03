# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions follow semver; every package
carries the same version.

## 0.1.0 - unreleased

First release of the monorepo (uv workspace on branch `next`).

### Added

- `hal doctor` adds `platform`, `wsl systemd` and `wsl gpu` lines on WSL2, and `docs/howto/run-on-windows-with-wsl2.md` explains the setup.
- `hal setup` opens the `hal tune` screen on a terminal when no chat model is assigned.
- `hal code`: `/model` lists the models of your sources by source, switches the session and `/model save`
  keeps the choice as the `code` role; `/effort` sets the thinking effort for models that support it, shows it in
  the status line and sends it with every request.
- `hal-agent` can send an effort level with each request: the ACP method `session/set_effort` sets it and the
  agent adds it to the Chat Completions body as `hal_effort`, which the gateway maps to the model's thinking controls.
- `hal tune` on a terminal opens an interactive screen in the style of `hal code`: it shows the hardware,
  sources and roles with a live fit line, lets you pick a model, context, KV type, effort and temperature,
  add or remove a source, tune one role or all, then shows the diff before it writes.
- `hal tune --write` and `hal models assign` write all or nothing: a failed pin rolls back the pins of that run.
- `hal tune` detects the hardware, reads the models of your sources and plans context, KV cache type and
  GPU layers per role; `--write` pins the models, saves the roles and the gateway wiring and generates the
  llama.cpp mode `tuned`. `hal models` lists sources and models, adds or removes a source, and assigns a role.
  `docs/user/models.md` and `docs/howto/point-hal-at-your-models.md` describe them.
- The gateway reads an effort hint (`X-Hal-Effort` header or `hal_effort` field) on chat completions and
  maps it to the model's thinking controls; role aliases apply the role's `effort` and `temperature`.
- `halkit.discovery` lists the models of a source with size, parameters, quantisation, layers, heads,
  context and capabilities (tools, vision, thinking): Ollama `/api/show`, GGUF headers, OpenAI-compatible
  `/v1/models`.
- `[models.sources.NAME]` and `[models.roles.ROLE]` config tables name where models come from (Ollama,
  OpenAI-compatible servers, GGUF folders, Anthropic, OpenAI) and which model plays each role. API keys
  are read from a key file, never from the config.
- `halkit.effort` maps Hal's effort levels (`off|low|medium|high|max`) to the request fields of Ollama,
  OpenAI-compatible servers, llama.cpp and Anthropic, per model family.
- `halkit.tuning` computes context length, KV cache type, GPU layers and tensor split from the
  hardware and a model's architecture; the formula is in `docs/dev/tuning.md`.
- `halkit.gguf` reads the metadata of a GGUF file (architecture, layers, KV heads, context, quantisation)
  from its header without reading the weights.
- `halkit.hardware` detects GPUs (`nvidia-smi`, `amd-smi`, `rocm-smi`), RAM, CPU threads and WSL2 for
  model tuning; it never raises and every subprocess has a timeout.
- `hal doctor` warns when a unit renders outside the registered packs or runs a missing program;
  `docs/howto/move-from-hal-ecosystem.md` documents `hal-hub mcp` for the vault MCP client.
- With `--migrate-from PATH` or `[migrate].source`, `hal setup` applies the config part of `hal migrate ecosystem`
  and, with `--replace`, drops `[hub].module_paths` entries inside hal-ecosystem (journaled);
  `--dry-run` prints each unit's `ExecStart`.
- `hal setup` finds the employment helper through `[jobs].jobhunt_root` and creates or narrows
  the sandbox state folder to 0700, so the doctor no longer fails on a 0775 folder.
- Units render from the registered packs: `@ROOT@` is the pack's folder and `@PYTHON@` the
  interpreter of its environment. `[hub].module_paths` only adds modules no pack provides.
- `hal migrate ecosystem` maps every old hal-ecosystem unit to the pack that now provides it.
- `hal eval` runs as a gpu-light `batch` job when gpu-light is installed, so chat turns
  (class `interactive`) and the warm base model (class `service`) keep priority.
- `hal setup` finishes an install in one idempotent step (`--dry-run`, `--enable`; another
  install's units are only replaced with `--replace`), and
  `hal-core` ships a `hal` entry point so `uv tool install` works.
- A bare `hal` opens Hal's own interface (`hal code` in a project, else `hal chat`) and never
  Claude Code. `hal claude [model|mode NAME]` is the only way to Claude Code and prints a notice;
  `hal here`, `hal mode` and `hal model` are removed.
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
- `hal configure --profile` picks `max`, `24gb`, `12gb` or `cloud` from the GPU and RAM and writes it to `[hal].profile`. `--migrate` imports an old `hal.conf` after a diff and a typed y.
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

### Changed

- `README.md` and `SETUP.md` are rewritten for the current install (`scripts/install.sh`, `hal setup`, `hal models`, `hal tune`).
- `setup.sh` and `hal setup` download no model. `setup.sh --pull-suggested` is the optional old behaviour.
  `hal setup` and `hal doctor` say when no chat model is assigned and print the command that assigns one.
- `hal configure` no longer writes model names. It records the profile, writes `[models]` limits and prints
  the `hal tune --auto` plan. `--profile max|24gb|12gb|cloud` still works as a preset for `context_cap` and
  `kv_type`, with a deprecation note.
- Hal holds no GPU claim while idle, so a gpu-light batch job is not parked for as long as Hal
  is on. `hal on` no longer claims the GPU light or waits for the card, and core Hal no longer
  ships `hal-gpu-light.service`; `hal setup` and `hal units install` remove an installed copy,
  and `hal off` still releases a claim named `hal`. The gateway claims the card per request.
- A unit marked `X-HalOff=yes` follows `hal off` and `hal on` (off condition, stopped by
  `hal off`, listed by `hal status`) without being marked as a GPU user. hal-voice's units need it.

### Fixed

- `hal code` shows the "Run this plan?" question when a plan turn ends; it was never drawn on a
  terminal. `hal chat` records its sessions as chats, and its `/resume` lists only chat sessions.
  The approval panel follows `[ui].theme`. Leaving `hal code` names the accept command only when
  the copy has changes.

### Security

- Units render only from core Hal, the registered packs and `[hub].module_paths`; an empty
  `module_paths` no longer falls back to the old ecosystem and extensions folders.
- The `[hub].module_paths` rewrite of `hal setup` and `hal migrate` is checked by parsing; one
  that would change another key or break the file is named for a manual edit instead.
- `hal setup --dry-run` no longer creates the state folder when it reads the unit record.
- `hal migrate undo` puts back a config that was a symlink, instead of leaving the copy the
  migration wrote in its place.
- The model is treated as hostile: every agent turn runs in a box with no network, a copy of
  the project and a scratchpad. Lockdown and paranoia can be changed only on the host.
- Security hardening: nested tool arguments are scanned without a depth limit, the broker, egress
  proxy and model forwarder have send timeouts and budgets, more credential paths are hard-denied,
  and `hal code export` refuses to overwrite a file without `--force`.
