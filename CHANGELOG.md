# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions follow semver; every package
carries the same version.

## Unreleased

### Added

- The gateway can run an OpenAI-compatible server for you, such as vLLM: a `managed-openai`
  backend starts at the first request (or `hal-gateway backend start NAME`), is ready when its
  health path answers and it lists a model of `served_models`, restarts after a crash, frees the
  GPU by stopping the llama router and emptying Ollama, and stops with its whole process group.
  A restarted gateway keeps a server whose config did not change; `idle_stop = false` keeps it
  through the idle unload. See `docs/howto/run-a-vllm-server-under-hal.md`.

### Changed

- Stopping the managed llama router now ends its whole process group and waits for every
  process in it, not only the leader.

## 0.1.0 - 2026-11-03

First release of the monorepo (uv workspace on branch `next`).

### Added

- `hal code` and `hal chat` run full screen on a capable terminal: clickable chips for model, mode
  and effort, tool calls as cards with their own timer, a composer with `/` and `@` completion,
  paste chips and history, pickers for `/model`, `/resume`, `/rewind`, `/mode`, `/effort` and
  `/theme`, a side panel (todo, changes, context), a plan card, an accept dialog with the exact
  patch, an exit dialog for unaccepted changes, and an approval dialog with Deny focused, a short
  arm delay, a scroll gate and a two-step allow that typing cannot trigger. Ctrl-C needs a second
  press to quit an idle session. Every control stays on screen down to 60x20, and `[ui].notify =
  "osc9"` sends a desktop notification after a long turn. The line UI stays as the fallback (`[ui].interface = "line"`, `HAL_TUI=line`, no
  terminal, or a failed start).

- `hal tune` opens a full-screen app on a capable terminal: a preset row, sliders for context,
  GPU layers and temperature with a memory bar that moves while you drag, buttons for KV cache
  and effort, a source form, a review tab, Ctrl+Z undo and a Quick start on first run. A click on
  a slider's number (or Enter) types an exact value. GPU layers
  can now be set by hand (`halkit.tuning.fit_layers`). The menu screen stays as the fallback
  (`[ui].interface = "line"` or `HAL_TUI=line`, or when the full screen cannot start).

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
- A click on effort in the full-screen top bar opens a row of the supported levels; the mouse
  wheel over effort steps the level while idle and sends it once the wheel stops.
- Pickers and `@` file completion match fuzzily (letters in order, word starts and file names
  first) and pickers show the matched letters in bold.
- Full-screen completions sit in one bordered box: commands with their description in a second
  column and the typed letters in the accent; on a wide terminal a preview beside the list shows
  the first lines of the highlighted `@` file (read without following links, binary files say so)
  or a command's arguments.
- Ctrl+R in the full screen is a fuzzy history search above the message box; Ctrl+R again moves
  to the next match and Enter loads the message without sending it.
- Ctrl+P in the full screen searches commands, stored sessions, files, models and settings in one
  list at the top of the screen.
- A click on the full-screen context gauge opens the `/context` split with a Compact button.
- A click on a path in the full-screen CHANGES list opens a read-only view of that file's diff in
  the copy.

### Changed

- The full-screen approval dialog marks a medium or high risk with `!`, fills a focused allow
  button in amber, and each answer leaves one row in the turn (`denied`, `allowed once`, ...).
- Full-screen dialogs have a thin grey border over a dimmed screen and flat one-row buttons, the
  focused one filled in red; the approval box and the plan card keep their heavy red border.
- The full-screen welcome is the HAL eye with the model, box and workspace beside it, the recent
  sessions of the project (a click resumes one) and a tip of the day, in place of the boxed header.
- The light theme's accent is a darker HAL red (`#c9373c`) and its greys are darker, so every text
  colour passes the WCAG 4.5:1 contrast minimum (hints 3:1); scrollbars in the full screen are one
  thin grey column instead of a red bar.
- Unicode glyphs are lighter: the prompt is a single angle quote (U+203A), reasoning lines start
  with U+2234 and the context bar uses parallelograms (U+25B0, U+25B1); the line UI shows them
  too. Every glyph has an ASCII fallback (`[ui].unicode = false` or a non-UTF-8 terminal).
- The full screen runs one animation clock, capped at 30 frames a second, only while something
  moves; an idle session runs no timer (it used to wake 4 times a second). Spinners are braille
  dots, and `[ui].animations = false` turns every motion off.
- The full screen shows the HAL eye, a red dot in the header: dim when idle, a slow pulse while a
  turn runs, an amber blink while an approval waits.
- The full-screen header is a slim one-row top bar with no filled chips: the HAL eye, project,
  then model, mode and effort (click to change) and a context gauge (click for `/context`). The
  status line and the top bar now name the model the same way (`default`, not `no model`).
- The full-screen status line has no background: box, lockdown, branch and changes, a tok/s
  sparkline and key hints that change while a turn runs; the separate key footer is gone.
- The full-screen message box is borderless with a prompt mark and grows with multiline input;
  pastes and pending attachments show as chips under it, and `x` on a chip removes it.
- The full-screen transcript groups each turn behind a left rail (red while running, amber while
  an approval waits) instead of flush blocks; a queued message gets its own rail, and the "new
  below" line is a faint row instead of a filled bar. Blocks of a different kind in a turn
  (prose, tool rows, thought, end line) have one blank row between them, none below 90x30.
- Tool calls in the full screen are one-line rows (verb, target, diffstat, time, result) whose
  output stays closed until clicked; a failed call opens by itself. Edits show a preview of up
  to six changed lines that expands to the full diff. Three or more consecutive read-only calls
  fold into one "ran N tools" row.
- Code blocks in the full screen show their language and a `copy` row (a click copies the code
  only) and scroll sideways instead of wrapping. The dark code theme is `github-dark` instead of
  `monokai`, in the line UI too.
- The todo list in the full screen is one `plan` checklist per turn with a progress bar and
  `1 of 3`, updated in place, instead of a new `Todo` panel on every change.
- Subagents in the full screen are one live row each (spinner, task, time) with their last
  three tool calls nested below, instead of a new line per update.
- Hover actions in the full screen: `copy edit again` (and `rewind` with a checkpoint) on your
  messages, `copy` on reply blocks. `again` sends the same text as a new turn; it is not a
  regenerate, and the earlier turn stays in the history. The message dialog gains Edit and Send again.
- `hal chat` shows the local time a message or reply appeared when you hover it.
- The full-screen side panel starts hidden at every width (it no longer opens from 110 columns
  or follows resizes). A handle right of the transcript toggles it on click and resizes it on
  drag. Tabs gave way to PLAN, CHANGES (with the changed paths) and CONTEXT sections.
- Reasoning in the full screen folds to one faint `thought for 4s` row (time from the first to
  the last thought chunk); opening it adds the token estimate.
- `hal chat` offers only the commands that work in a chat: `/help`, Tab and Esc Esc leave out the
  project-copy commands (`/rewind`, `/diff`, `/accept`, `/export`, `/ide`, `/init`), and `!`, `#`
  and `@` are sent as plain text.

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
- `/resume` starts from a clean todo list, turn stats and context bar and switches to the
  session's stored model. The status line names the box backend (`box runsc`).
- An approval panel in `hal code` or `hal chat` closes as soon as its request is answered in
  another terminal or the console; before, it kept reading and took the next line typed for the
  main prompt. Two requests no longer draw over each other.
- `hal tune`: Save and exit with nothing changed exits, Quit without saving asks first when there
  are changes, and an effort can be set back to `default`. `hal setup` says to press Esc to skip
  the tune screen.
- In `hal code` and `hal chat`, the times on tool rows, folded groups and subagents line up in
  one column, whether or not a row has a body or failed.
- The approval panel shows its whole hint line at 60x20; the stacked buttons used to push the
  end of it out of the box.
- The Ctrl+P palette, the Ctrl+R history search and the `/` and `@` completions keep each row on
  one line, clipped with an ellipsis, and the palette and history search lines show their
  placeholder instead of a box drawn over them.
- The Ctrl+P palette lines its descriptions up in one column.
- The side panel's plan uses the same marks and colours as the plan in the transcript.
- Typing in `hal code` and `hal chat` stays fast in a long session: the message box, status line
  and side panel no longer redraw the whole screen for text that did not change. Measured with a
  scripted agent: a turn after 150 others took about 0.5 s before and 0.25 s after.

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
