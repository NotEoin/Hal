# Core CLI: halcore and the `hal` command

This spec defines `halcore`, the package behind the `hal` command: the command tree, the
terminal UI, headless mode, the `hal acp` proxy, the launch of assistant channels, the
built-in `core` tool module, power and unit management, pack installs, eval, console,
configure, doctor, the installer, and the cutover of this machine from `hal-ecosystem` to the
new repos. It replaces the bash launcher `hal`, `setup.sh`, `hal-kill`, `halcore/*.py` and
`hal-ecosystem/install.sh`. The agent, sandbox, broker, hub, policy and gateway are specified
elsewhere; this spec fixes what `halcore` needs from them and how the user reaches them.

Status: draft 2026-10-23, revised 2026-10-24 to match
`~/src/.dev-notes/specs/RESOLUTIONS.md` (cited below by finding id, such as A1
or B3). Where this text and RESOLUTIONS.md disagree, RESOLUTIONS.md wins.

## Scope and non-goals

In scope (prefix REQ-CLI, per the index in [00-overview.md](00-overview.md)):

- the `hal` command tree, its synopsis, behaviour, flags and exit codes, including the policy,
  approval, audit, guard and secrets commands whose behaviour 01-security defines (B12);
- the TUI (rich and prompt_toolkit): rendering, input, slash commands, approval prompts,
  status line, cancellation;
- the ACP client inside `hal`, the `hal acp` proxy for editors and channels, and the launch of
  the long-lived assistant box per channel and its unboxed fallback (A1);
- code session assembly: rendering `/run/hal/agent.json` and the run files (E4, E5), and
  handling `_hal/record` transcript notifications (E3);
- headless mode `hal -p`: its flags and exit codes (F1);
- the built-in `core` hub module with the `core_long_task` tool (F2);
- front-ends that delegate to other packages: `hal box`, `hal connect`, `hal claude`,
  `hal gateway`, `hal models`, `hal serve`, `hal stop`, `hal sessions`, `hal eval` (F4);
- ops: `hal on|off|toggle|status|kill`, `hal units` (unit rendering for every package and
  pack, F8 to F10), desktop launchers;
- `hal packs` (replaces `hal ext`), pack environments and `state_dir()/packs.json` (C8);
- `hal console`, `hal configure`, `hal doctor`, `hal migrate`;
- `scripts/install.sh` and `scripts/host-setup.sh`;
- the cutover of this machine, with a checklist and an undo path;
- the config tables `[ui]`, `[console]`, `[units]`, `[claude]` and `[long_task]`.

Non-goals (owned elsewhere; this spec only states the interface it consumes):

- the agent loop, toolsets, session record schema, headless output schema, eval library and
  task formats, `[agent]` and `[mcp]` tables (03-agent);
- backends, mounts, masking, dependency prep, images, broker internals, checkpoints,
  `--isolated`, per-CLI box templates (02-sandbox);
- tiers, `policy.toml`, approval data model, keys, scopes, surfaces protocol, control socket
  protocol, audit format, channel profiles, residual risks (01-security);
- manifest v2, entry-point discovery, runner, install steps, `hal-hub` CLI (04-hub);
- gateway API, tokens, presets, model manifest, managed llama-server, gpu-light claims,
  `hal-gateway` CLI and its unit template (05-gateway);
- `halkit` (00-overview), `hal remote` (07-remote), the pack list and pack contents
  (08-packs-and-flagships);
- any change to hal-employment; Windows or macOS hosts for `hal code`.

## Facts this spec relies on

Checked on this machine with read-only commands. Experiments marked "scratch dir" ran on
2026-10-23 in a scratch directory that has since been wiped; the commands are listed so they
can be repeated.

| Fact | How checked |
| --- | --- |
| Docker 29.2.1; runtimes `io.containerd.runc.v2`, `runc`, `nvidia`, `runsc`, `runsc-gpu` | [verified 2026-10-24: `docker version`, `docker info --format '{{range $k,$v := .Runtimes}}{{$k}} {{end}}'`] |
| `/etc/docker/daemon.json` registers `runsc` with no `runtimeArgs`, so `--host-uds=open` is not yet set and a runsc box cannot reach the bind-mounted sockets; the user is registering it (G1) | [verified 2026-10-24: `cat /etc/docker/daemon.json`; refusal without the flag is the brief's host fact] |
| `--runtime=runsc-gpu --gpus all` fails ("cannot read client sync file: EOF"); GPU containers use `--runtime=nvidia` | brief, "Host facts" |
| openssh-server is active, key-only (`/etc/ssh/sshd_config.d/10-hal-hardening.conf`) | [verified 2026-10-24: `systemctl is-active ssh`]; brief, "Host facts" |
| The user is in group `docker` (root-equivalent on the host) | [verified: `id -nG`] |
| A container with `--network none --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 256m --user 1000:1000` ran as uid 1000, had no network and an EROFS rootfs; first run 5.3 s, warm runs 0.33 s; a runsc container starts in 0.26 s | [verified: `docker run` of `alpine:3.22` (scratch dir); runsc time from the brief's host facts] |
| bubblewrap 0.11.1 runs `--unshare-all` unprivileged | [verified: `bwrap --unshare-all ... /usr/bin/true`] |
| llama-server build 11512 accepts `--host` (IP list or unix socket path), `--port` (default 8080), `--api-key-file`, `--models-preset`, `--models-max`, `--no-models-autoload` | [verified: `llama-server --help`, `--version` prints `build 11512, commit a11f57ba9`] |
| Ollama 0.40.1 answers on 127.0.0.1:11434 | [verified: `GET /api/version`] |
| PyPI: `rich` 15.0.0 (2026-04-12), `prompt-toolkit` 3.0.53 (2026-07-26), `mcp` 2.3.0 (2026-10-02) | [verified: PyPI JSON API] |
| rich 15.0.0 passes ESC sequences in model text through to the terminal: an OSC 52 clipboard write and `ESC[2J` survived both `Markdown` and `Text`; rich markup in the text was not parsed | [verified: render test (scratch dir)] |
| rich has `box.ASCII` and the `line` spinner; prompt_toolkit 3.0.53 `create_pipe_input` drives a `PromptSession` in tests; `termios.tcflush` exists | [verified: same scratch dir] |
| Python 3.13.11, uv 0.11.19, systemd 259 | [verified: `--version`] |
| `gpu-light` is not on PATH, so no gpu-light unit is installed | [verified 2026-10-24: `which gpu-light`; `~/.config/systemd/user` listing] |
| `~/.local/state/hal/off` exists; `units.json` lists 20 units, all under `hal-ecosystem/{core,extensions}` | [verified: `cat units.json`] |
| `~/.local/bin`: `hal`, `hal-hub`, `hal-voice`, `hal-employment` point into `hal-ecosystem`; `hal-private` into `core/hal-private`; `batchjob` into `batch-project`; `hal-old` and `a-bot` dangle | [verified: `ls -la ~/.local/bin`] |
| Desktop: `hal-console.desktop`, `hal-toggle.desktop`, `hal-kill.desktop`, `hal-employment.desktop` | [verified: `ls ~/Desktop`] |
| Vault `.mcp.json` servers: `obsidian` (env key is a `${OBSIDIAN_API_KEY}` placeholder), `hal` (`uv run --with mcp .../Hal/hal-mcp.py`, file exists today), `hal-hub` (`~/.local/bin/hal-hub mcp`); the vault is not a git repo | [verified: JSON keys read with python, values not printed] |
| `~/.config/hal/config.toml` is mode 0664; `[hub].module_paths` names the two `hal-ecosystem` folders; it has no `[jobs]` table | [verified: `ls -la`, `sed -n 24,28p`; `[jobs]` absence from review-consistency H1] |
| hal-voice and hal-telegram today locate core hal by `HAL_CORE`, `<ecosystem>/core/hal` or `[hal].core` and build `halcore.brain.Brain`; the released versions are ACP clients of `hal acp` instead (A1, B9) | [verified: `halvoice/brain.py:43-70`, `haltelegram/config.py:68-71`] |
| hal-memory's install step merges the vault `.mcp.json` (adds `obsidian`, points `hal-hub` at its launcher, removes the old `hal` server, keeps `.mcp.json.bak`) | [verified: `hal-memory/README.md:48`] |

## Requirements

Test kinds: **unit** (hermetic, default run), **integration** (hermetic, several modules,
fakes, default run), **docker**, **bwrap**, **slow**, **gpu**, **network**, **live** (marked,
deselected by default; markers per E12), **manual**.

### Command tree

- **REQ-CLI-01** (MUST) `hal` MUST be the console script `hal = "halcore.cli:main"` of
  `hal-core`, and every command in "Command reference" MUST be reachable from it. Test: unit
  `test_every_documented_command_has_a_parser` (parses the synopsis lines of this spec's
  rendered user doc `docs/user/cli.md` and runs `--help` on each).
- **REQ-CLI-02** (MUST) Usage and config errors MUST exit 2, a missing prerequisite (no config
  file, gateway unreachable, no sandbox image in a non-interactive run, a refused sandbox
  fallback) MUST exit 3, and success MUST exit 0; other codes are listed per command. Test:
  unit `test_exit_codes_follow_the_table`.
- **REQ-CLI-03** (MUST) The old names `here`, `mode`, `model`, `ext`, `install`, `serve`,
  `stop` MUST still work and print one line on stderr naming the new command (REQ-SYS-23).
  Test: unit `test_cli_aliases.py::test_old_name_prints_one_line_and_runs_new_command`.
- **REQ-CLI-04** (MUST) The vault commands `ask`, `index`, `brief`, `summarize`, `import`,
  `obsidian-key`, `register-repos` MUST exec the hal-memory CLI found through the installed
  pack, with argv passed unchanged, and MUST say which pack to add when it is missing. Test:
  unit `test_vault_command_execs_hal_memory_with_same_argv`.
- **REQ-CLI-05** (MUST) No `hal` code path MAY source, `eval` or execute `hal.conf` or any
  other shell file. Test: unit `test_no_module_runs_a_shell_on_config_files` (AST scan for
  `bash`/`sh` argv with `-c` or `.` in halcore) and `test_migrate_parses_hal_conf_without_a_shell`.
- **REQ-CLI-06** (MUST) `hal --version` MUST print the version of every installed `hal-*`
  distribution (REQ-SYS-18). Test: unit `test_version_lists_every_hal_distribution`.
- **REQ-CLI-07** (MUST) Every subprocess, socket and HTTP call made by halcore MUST have a
  timeout, through `halkit.proc.run` or an explicit `timeout=` (REQ-SYS-15). Test: unit
  (root `tests/test_style.py`).

### Launch target

- **REQ-CLI-08** (MUST) `hal` with no subcommand on a terminal MUST open the TUI in `code`
  mode when the working directory resolves to a project root, and in `chat` mode otherwise,
  following "Launch target resolution". Test: unit `test_launch.py` (one case per row of the
  resolution table).
- **REQ-CLI-09** (MUST) A project root MUST NOT be mounted as `/work` when it is `/`,
  `$HOME` or an ancestor of `$HOME`, or when it is, contains or lies inside any directory of
  01's built-in hard-deny list (01-security.md, "Built-in hard-deny paths"), `config_dir()`,
  `state_dir()`, `data_dir()`, `~/.config` or `~/.local` (which covers `~/.local/share` and
  `~/.local/state`); `hal code` on such a path MUST exit 2 with the rule. A project root that
  contains a `[paths].deny` path (01) or `[hal].vault` MUST need a typed per-project
  confirmation on a terminal that names those paths (recorded in
  `state_dir()/workspace-ok.json` with the matched set; a changed set asks again); without a
  terminal and without that record it MUST exit 2. Test: unit
  `test_home_and_config_dirs_are_never_a_workspace` (one case per hard-deny directory,
  `~/.config`, `~/.local/share`, `~/.local/state`, and a root containing `~/.aws`),
  `test_root_containing_vault_or_deny_path_needs_typed_confirmation`.
- **REQ-CLI-111** (MUST) Before mounting a work tree, halcore MUST compare it with the source
  roots of every registered module (`packs.json` `path`, `[hub].module_paths`,
  `HAL_MODULE_PATH`) and of every editable `hal-*` distribution in the Hal venv and the pack
  envs (`direct_url.json` `dir_info.editable`), so the box cannot write Python that the hub
  runner or `hal` then runs on the host (review finding G05). When the work tree contains
  or equals one of these roots, `hal code` MUST refuse with exit 2 naming the roots, unless
  `--isolated` is given or the user types a per-project confirmation on a terminal that
  names them (recorded in `state_dir()/workspace-ok.json` beside REQ-CLI-09's record, with
  the matched set; a changed set asks again). halsandbox mounts what it is given (02), and
  the hub marks a module whose source is inside an active session workspace `unavailable`
  (04, "Discovery and registry", review finding G05), so a confirmed session cannot change
  code a host call runs while it is active. Test: unit
  `test_workspace_containing_editable_pack_needs_consent`,
  `test_isolated_session_skips_editable_pack_consent`.
- **REQ-CLI-10** (MUST) `hal` with no subcommand and stdin not a terminal MUST exit 2 and
  suggest `hal -p`. Test: unit `test_no_tty_without_p_is_a_usage_error`.

### TUI

- **REQ-CLI-11** (MUST) All text that came from the model, a tool result, a file or the agent
  process MUST pass through `sanitize_terminal_text` before it reaches the terminal: C0
  controls except `\n` and `\t`, DEL, C1 controls, and every ESC-introduced sequence are
  removed, and the characters that 01-security REQ-SEC-25 lists (bidirectional controls,
  zero-width characters, U+00AD) are shown as `\uXXXX` escapes through `halkit.termsafe`
  (C10). Test: unit `test_osc52_and_csi_sequences_are_stripped`,
  `test_bidi_overrides_are_escaped`.
- **REQ-CLI-12** (MUST) Model text MUST never be parsed as rich markup. Test: unit
  `test_rich_markup_in_model_text_prints_literally`.
- **REQ-CLI-13** (MUST) Assistant text MUST stream as Markdown with completed blocks printed
  once and only the trailing open block kept live, so redraw cost does not grow with message
  length. Test: unit `test_streaming_commits_closed_blocks_and_keeps_open_fence_live`.
- **REQ-CLI-14** (MUST) Reasoning content MUST NOT be shown unless `[ui].show_thinking` is
  true; a one-line "thinking (Ns)" indicator MUST be shown instead. Test: unit
  `test_thought_chunks_are_hidden_by_default`.
- **REQ-CLI-15** (MUST) Each tool call MUST render as one header line (tool, short argument
  summary, where it ran: `box` or `host`), then status and at most `[ui].tool_output_lines`
  lines of output, with the rest reachable through `/tools`. Test: unit
  `test_tool_call_renders_header_status_and_capped_output`.
- **REQ-CLI-16** (MUST) A file change reported by the agent (ACP `diff` content) MUST render
  as a coloured unified diff capped at `[ui].diff_max_lines`, with a `+N -M` summary. Test:
  unit `test_diff_content_renders_unified_diff_with_counts`.
- **REQ-CLI-17** (MUST) The slash commands `/help /model /undo /diff /compact /clear /resume
  /tools /policy /mode /cost /exit` MUST exist with the behaviour in "Slash commands";
  `/apply` MUST exist in `--isolated` sessions. Test: unit `test_slash_commands.py`, one test
  per command.
- **REQ-CLI-18** (MUST) Commands the agent advertises with ACP `available_commands_update`
  MUST be completed and sent as prompt text; a local command with the same name MUST win.
  Test: unit `test_agent_commands_are_completed_and_local_names_win`.
- **REQ-CLI-19** (MUST) `@path` mentions MUST complete paths under the project root, and on
  send MUST become ACP content blocks: an embedded `resource` for a text file up to
  `[ui].mention_max_bytes`, a `resource_link` otherwise. A path outside the project root MUST
  need an explicit y/N confirmation. Test: unit `test_mentions.py`.
- **REQ-CLI-20** (MUST) Enter MUST submit and Alt-Enter (or Ctrl-J) MUST insert a newline;
  bracketed paste MUST keep pasted newlines without submitting. Test: unit
  `test_alt_enter_inserts_newline_and_paste_does_not_submit`.
- **REQ-CLI-21** (MUST) Ctrl-C during a turn MUST send ACP `session/cancel`, answer any open
  permission request with `cancelled`, deny any open broker approval, and return to the
  prompt when the agent replies `cancelled`. A second Ctrl-C after `CANCEL_GRACE_S` MUST stop
  the box and keep the session resumable. Ctrl-C at the prompt MUST clear the line; Ctrl-D on
  an empty line MUST exit. Test: integration `test_ctrl_c_cancels_turn_and_denies_pending_approval`.
- **REQ-CLI-22** (MUST) A status line MUST show the model alias and backend model, context
  use in percent from the last `usage_update`, the sandbox backend (with `no runsc` or
  `UNSANDBOXED` when that applies), the session mode, the policy profile, the last
  checkpoint number, and the session's remaining host-side model budget from its
  `SessionForwarder` (05-gateway.md REQ-GW-69): requests left in the current minute and
  tokens left in the current hour, or the wait in seconds after the forwarder answered 429
  `budget_exceeded`. Test: unit `test_status_line_fields`,
  `test_status_line_shows_remaining_session_budget`.
- **REQ-CLI-23** (MUST) With backend `none` (a code session, or the unboxed assistant of
  REQ-CLI-96), the TUI MUST show a banner at start and `UNSANDBOXED` in the status line on
  every redraw. Test: unit `test_unsandboxed_banner_and_status`.
- **REQ-CLI-24** (SHOULD) Output SHOULD use ASCII box drawing and spinners unless
  `[ui].unicode` is true. Test: unit `test_default_render_is_ascii`.

### Approvals

The approval data model (key, request id `apr_` + 16 hex, `Approver` protocol, `Decision`
with verdict and scope, scopes `once` and `session`, surfaces and the control socket protocol)
is defined in 01-security.md, sections "Approval binding" and "Approval surfaces and
protocol", in the module `halhub.approvals` (B1 to B5). halcore implements surfaces and
renders prompts; it defines no second data model.

- **REQ-CLI-25** (MUST) A broker approval prompt MUST be rendered from the `ApprovalRequest`
  (01) and show the tool, pack, tier, the policy reason, every canonical argument, the first
  12 hex digits of the approval key after `sha256:`, and the label `leaves the box`; an
  in-box ACP permission request MUST be labelled `inside the box` and MUST NOT use the broker
  layout. Test: unit `test_broker_and_box_prompts_are_distinct`.
- **REQ-CLI-26** (MUST) The approve keys MUST be disabled until every argument has been
  shown; arguments longer than `APPROVAL_PAGE_LINES` require the `v` view first. Test: unit
  `test_long_arguments_cannot_be_approved_unseen`.
- **REQ-CLI-27** (MUST) Before an approval prompt reads a key, pending terminal input MUST be
  discarded (`tcflush(TCIFLUSH)`), and Enter alone MUST mean deny. Test: unit
  `test_typeahead_is_flushed_and_enter_denies`.
- **REQ-CLI-28** (MUST) String arguments in an approval prompt MUST be displayed through
  `halkit.termsafe.escape` (01-security, "Approval surfaces and protocol", display rules), so
  hidden or reordered text cannot change what is read. Test: unit
  `test_approval_args_escape_controls_and_non_ascii`.
- **REQ-CLI-29** (MUST) Every answer a halcore surface returns MUST carry the request id and
  the key of the request it displayed, and halcore MUST refuse to return an answer whose key
  does not match the request. The keys halcore displays and sends MUST equal the ones in
  `tests/data/approval_vectors.json` (B1). Test: unit `test_decision_carries_request_key`,
  `test_approval_vectors.py::test_displayed_key_prefix_matches_vectors`.
- **REQ-CLI-30** (MUST) Broker approval prompts MUST offer allow once, allow these exact
  arguments for this session, deny, and deny with a note, and MUST NOT offer an "always"
  option on any surface (B2); on the `voice` and `telegram` channels only allow once and deny
  are offered (REQ-SEC-30). Test: unit `test_broker_prompt_offers_once_and_session_only`,
  `test_voice_and_telegram_offer_once_only`.
- **REQ-CLI-31** (MUST) `hal approvals [list]` MUST list pending requests from every control
  socket of the same user; `hal approve ID` and `hal deny ID` MUST find the socket that owns
  ID, show the full call in the approval layout, and need a typed `y` (approve); all three
  MUST refuse to run when stdin or stdout is not a terminal or `HAL_HARNESS` is set (B3,
  REQ-SEC-27, ATK-39). Test: integration
  `test_hal_approve_resolves_pending_request_over_control_socket`, unit
  `test_hal_approve_refuses_without_tty_or_inside_harness`.
- **REQ-CLI-32** (MUST) Each process that hosts a broker MUST serve exactly one control socket
  per broker session at `runtime_dir()/control/<sid>.sock` (directory 0700, socket 0600),
  outside every session directory that is mounted into a box, with the `SO_PEERCRED` uid
  check of 01-security (B3). No approval or grant file is written to disk. Test: unit
  `test_control_socket_path_is_not_under_session_dir`, `test_control_socket_rejects_other_uid`
  (simulated credentials), `test_no_grant_file_is_written`.
- **REQ-CLI-93** (MUST) Every halcore surface MUST declare `shows_all_args`; a request whose
  tool has a parameter with `content = true` MUST never be offered to a surface without it
  (the `voice` channel, desktop notifications) and MUST go to the TUI, Telegram, the console
  or `hal approve` instead (RESOLUTIONS "Surfaces"). Test: unit
  `test_content_request_never_routed_to_voice_or_notify`.
- **REQ-CLI-94** (MUST) halcore MUST call `Broker.on_human_message(session, text, surface)`
  (01-security, F5) with the prompt text before it sends every `session/prompt` that a human
  typed or spoke (TUI, `hal chat`, `hal acp` editor and channel prompts, console); headless
  runs MUST NOT call it, because their task may be model-written (`core_long_task`). Test:
  integration `test_every_human_prompt_reaches_broker_before_agent`,
  unit `test_headless_never_resets_taint`.

### Headless

`hal -p` flags and exit codes are owned here; the output objects of `--json` and
`--stream-json` are owned by 03-agent.md, section 3.15 (F1). 03 adopts these flags and exit
codes for `hal-agent -p`.

- **REQ-CLI-33** (MUST) `hal -p TASK` MUST run one agent session to completion with no TUI,
  print the final message to stdout, and exit with the code mapped from the ACP stop reason
  ("Headless mode"). Test: integration `test_headless_end_turn_exits_zero_and_prints_result`.
- **REQ-CLI-34** (MUST) `--json` MUST print exactly one JSON object and `--stream-json` one
  JSON object per line, flushed per line, both in the schema of 03-agent.md section 3.15,
  rendered on the host by `halagent.headless.Renderer` from ACP updates and `_hal/record`s.
  Test: integration `test_stream_json_validates_against_agent_schema`.
- **REQ-CLI-35** (MUST) In headless mode an `ask` decision MUST be a deny with reason
  `no-approver` unless `--approvals queue[:SECONDS]` is given; with it, the request MUST be
  offered only on the control socket (`hal approve`) and MUST deny on timeout. Test:
  integration `test_headless_ask_denies_without_queue_and_waits_with_queue`.
- **REQ-CLI-36** (MUST) `--timeout S` MUST cancel the turn at S seconds and exit 124. Test:
  integration `test_headless_timeout_cancels_and_exits_124`.

### ACP proxy and assistant channels

- **REQ-CLI-37** (MUST) `hal acp` MUST proxy ACP between its stdio and the agent, and MUST
  remove the client's `fs` and `terminal` capabilities from `initialize` before forwarding,
  and reject any `fs/*` or `terminal/*` request from the agent with JSON-RPC error -32601, so
  the agent can never run or read anything through the editor on the host. Test: integration
  `test_acp_proxy_strips_fs_and_terminal_and_rejects_their_calls`.
- **REQ-CLI-38** (MUST) `hal acp` MUST send broker approvals to the client as host-originated
  `session/request_permission` in the shape of 01-security.md "Approval surfaces and
  protocol" (ACP mapping; ids `hal-broker-<n>`, `_meta` `hal/origin = "broker"`), consume the
  responses to its own ids, delete any `hal/` `_meta` key from agent-originated messages and
  prefix their tool call title with `[box] ` (REQ-SEC-29). Test: integration
  `test_agent_cannot_forge_broker_origin`.
- **REQ-CLI-39** (MUST) In project mode, `hal acp` MUST reject a `session/new` or
  `session/load` whose `cwd` is outside the project root before forwarding it; path
  rewriting between host paths and `/work` is done by the agent (03-agent.md section 3.14.5)
  and the proxy forwards paths unchanged. With `--toolset assistant` the proxy MUST replace
  the client's `cwd` with the channel scratch directory (REQ-CLI-40) before forwarding; the
  client's value is ignored, so a channel (08's Telegram bridge, hal-voice) may send any
  `cwd`. Test: unit `test_cwd_outside_project_is_rejected_by_proxy`,
  `test_assistant_toolset_cwd_is_replaced_by_scratch`.
- **REQ-CLI-108** (MUST) The host ACP transport (halcore's client and the `hal acp` proxy)
  MUST refuse a line from the agent longer than `ACP_MAX_MESSAGE_BYTES` (8 MiB) or JSON nested
  deeper than `ACP_MAX_JSON_DEPTH` (64), without buffering past the cap, and end the session
  with `AgentCrashed`; the agent's stderr log MUST rotate at `AGENT_STDERR_MAX_BYTES`
  (10 MiB) keeping 2 files. The box is untrusted, so it must not be able to exhaust host
  memory or disk through the pipe. Test: unit `test_oversize_acp_line_ends_session`,
  `test_deep_json_ends_session`, `test_agent_stderr_log_rotates`.
- **REQ-CLI-40** (MUST) `hal acp --channel NAME --toolset assistant`, `hal chat`, the console
  and `hal eval` MUST run the agent with the `assistant` toolset in an assistant box
  (`halsandbox.Session` of kind `chat`) whose `/work` is the scratch directory
  `state_dir()/assistant/<channel>/work` and never the vault; host tools reach it only
  through the session's broker, which passes `channel` to every decision (A1). Test: unit
  `test_assistant_box_mounts_scratch_never_vault`, integration
  `test_assistant_tool_call_goes_through_broker_policy`.
- **REQ-CLI-41** (MUST) No module outside `halcore` MAY be required to import `halcore` or
  `halagent` to talk to Hal; voice, Telegram and editors use `hal acp` (A1, A3). Test: unit
  (root `tests/test_layering.py`) and manual (hal-voice and the comms pack's Telegram bridge
  use `hal acp`).
- **REQ-CLI-95** (MUST) The assistant box MUST live as long as the `hal` process that started
  it and MUST be reused for every ACP session of that process (`/clear`, a new channel
  conversation), so the per-turn cost is a pipe hop. Test: integration
  `test_second_session_reuses_assistant_box` (`FakeSandboxSession` counts starts).
- **REQ-CLI-96** (MUST) Only when the resolved backend is `none` MAY an assistant channel run
  unboxed: the agent then runs in the `hal` process through `halagent.acp.serve` with the
  same broker, policy and settings, the TUI or channel shows an `UNSANDBOXED` banner, and the
  first unboxed start per channel MUST be confirmed with a typed `y` on a terminal (recorded
  in `state_dir()/unboxed-ok.json`); a channel without a terminal and without that
  record MUST exit 3 with the reason. Test: unit `test_unboxed_assistant_needs_recorded_consent`,
  `test_unboxed_assistant_has_only_broker_tools`.
- **REQ-CLI-97** (MUST) When 02's cached probe last saw docker and docker is now unreachable,
  `hal code`, `hal chat`, `hal acp` and `hal -p` MUST refuse to start (exit 3, naming the
  daemon and `--backend`) rather than fall back silently. Backend `none` for a code session
  uses 02's per-project consent (02-sandbox.md REQ-BOX-68, recorded as `allow_none` in
  `$STATE/sandbox/projects.json` by REQ-BOX-70): halcore passes
  `interactive=<stdin is a TTY>` to `Session.start` and keeps no record of its own, and
  exits 3 with 02's reason when 02 refuses (RESOLUTIONS "Fallback"). `unboxed-ok.json`
  (REQ-CLI-96) is for assistant channels only. Test: unit
  `test_docker_seen_then_down_refuses_to_start`,
  `test_code_session_passes_interactive_to_session_start`.
- **REQ-CLI-98** (MUST) With backend `none`, halcore MUST NOT pass any user MCP server that
  is not marked `host = true` (03-agent `[mcp.servers.<name>]`) to the agent, neither from
  config nor from an ACP client's `mcpServers`, and MUST say which were dropped (F4, ATK-40).
  Test: unit `test_backend_none_drops_non_host_mcp_servers`.

### Code session assembly and transcripts

- **REQ-CLI-99** (MUST) For every session halcore MUST render the agent's settings with
  `AgentSettings.to_json()` (03-agent.md section 3.16.3) and the run files (`agent.json`,
  `user/AGENTS.md`, `skills/...`) and pass them to `halsandbox.Session.start(run_files=...)`;
  the broker location in the settings is `unix:///run/hal/broker.sock` (E4, E5). Test: unit
  `test_agent_settings_rendered_into_run_files`.
- **REQ-CLI-100** (MUST) halcore MUST advertise `clientCapabilities._meta.hal.records = true`,
  validate each `_hal/record` against 03-agent.md section 3.10.2, accept only the record types
  the agent may send (never `checkpoint`), write them to
  `state_dir()/sessions/<sid>/transcript.jsonl`, write `checkpoint` records itself, and stop
  accepting records past 256 MiB per session with a notice (E2, E3). Test: unit
  `test_records_validated_and_checkpoint_records_refused_from_agent`.
- **REQ-CLI-105** (MUST) `/undo` and `/diff` MUST show the files that 02's `DiffReport` marks
  as not restorable (ignored files, files over `[sandbox.checkpoints].max_file_bytes`) instead
  of skipping them silently (R-new-5, ATK-43). Test: unit `test_undo_lists_unrestorable_files`.
- **REQ-CLI-106** (MUST) halcore MUST show 02's masking notice (files bound over by
  `[sandbox].mask`) at session start and pass `--unmask` through to 02 (R-new-6). Test: unit
  `test_mask_notice_and_unmask_flag_passed`.
- **REQ-CLI-107** (MUST) When 02 reports that a lockfile changed since the last dependency
  prep, the TUI MUST offer `hal code --deps` once per lockfile sha256 and MUST NOT run it
  without the `act` approval 02 defines (architect F3). Test: unit
  `test_deps_prep_offered_once_per_lockfile`.

### Harness front-ends

- **REQ-CLI-42** (MUST) `hal box [OPTIONS] -- CMD [ARGS]` MUST parse its options, then hand
  `CMD ARGS` unchanged to halsandbox; `hal box` with no `--` and no subcommand MUST exit 2.
  Test: unit `test_box_passes_argv_after_double_dash_unchanged`.
- **REQ-CLI-43** (MUST) `hal connect HARNESS` MUST print, first, a line stating the harness
  runs unsandboxed on the host and that its approvals are advisory (B4, R-new-4), then the
  gateway base URL, the token file reference and the hub MCP entry for that harness;
  `--write` MUST show a diff of the harness config and need y/N. Test: unit
  `test_connect_prints_unsandboxed_line_first`, `test_connect_write_needs_confirmation`.
- **REQ-CLI-44** (MUST) `hal claude` MUST print a one-line warning that Claude Code runs
  unsandboxed and that the network block is advisory, MUST point Claude Code at the gateway
  root URL `http://127.0.0.1:8741` with a named client token (F7, D5), and MUST inject
  `--permission-mode default` unless the user passed `--permission-mode` or
  `--dangerously-skip-permissions`. Test: unit `test_claude_env_and_permission_mode`.
- **REQ-CLI-45** (MUST) `hal claude` MUST keep the privacy env block of `hal:247-268` and the
  `HTTP(S)_PROXY=http://127.0.0.1:9` backstop unless `[claude].allow_network` is true. Test:
  unit `test_claude_privacy_env_block_matches_legacy_list`.
- **REQ-CLI-104** (MUST) `hal connect` and `hal claude` MUST turn on the gateway's TCP
  listener (05, D4) only after printing that they do so, and MUST use a named client token
  per harness (`connect-<harness>`, `claude`) issued through 05's token API, never the owner
  token. Test: unit `test_connect_enables_tcp_with_notice_and_named_token`.

### Gateway and models front-ends

- **REQ-CLI-46** (MUST) `hal serve [PRESET]` MUST ensure the gateway runs (unit if installed,
  else a detached `hal-gateway serve`) and call `GatewayClient.set_mode(PRESET,
  wait_s=LOAD_TIMEOUT_S)` (05). Test: integration
  `test_serve_starts_gateway_then_preset_with_fake_gateway`.
- **REQ-CLI-47** (MUST) `hal stop` MUST call `GatewayClient.stop_router()`; `hal stop --all`
  MUST also stop the gateway. Test: integration `test_stop_and_stop_all`.
- **REQ-CLI-48** (MUST) `hal models pull` MUST show the total download size and ask before
  downloading more than `CONFIRM_DOWNLOAD_BYTES` unless `--yes` (the only `--yes` that skips a
  safety prompt in halcore, F4); `hal models verify` MUST exit 1 when any file's sha256
  differs from the manifest. Both delegate to `halgateway.models` (05, D10). Test: unit
  `test_pull_confirms_large_downloads`, integration `test_verify_fails_on_hash_mismatch`.

### Power and kill

- **REQ-CLI-49** (MUST) `hal off` MUST create the off flag at `state_dir()/off` (and at the
  default path when `HAL_STATE_DIR` differs), stop every installed unit recorded as `gpu` in
  `units.json`, unload Ollama models, call `GatewayClient.release_gpu(stop_router=True)`
  (05-gateway.md, "GatewayClient (halcore only)"), and release the
  gpu-light v1 claim `hal` last when one is held. Test: unit
  `test_off_order_flag_units_models_gateway_light`.
- **REQ-CLI-50** (MUST) `hal off` MUST NOT stop a managed llama-server that an open
  interactive session holds a lease on (05, D9), and MUST name that session; `hal off --force`
  MUST call `GatewayClient.release_gpu(force=True, stop_router=True)` instead (05 REQ-GW-55).
  Test: unit
  `test_off_keeps_leased_llama_server_and_force_stops_it`.
- **REQ-CLI-51** (MUST) With gpu-light v2 present, `hal on` MUST hold no persistent claim
  (the gateway and hal-voice claim per use, D8); with only gpu-light v1, `hal on` MUST claim
  `hal` (waiting up to `CLAIM_WAIT_S`) before it removes the off flag, as today. Then it MUST
  unload Ollama models that sit partly in RAM and start `hal.target`. Test: unit
  `test_on_claims_only_with_gpu_light_v1`, `test_on_claims_before_removing_flag`.
- **REQ-CLI-52** (MUST) gpu-light failures (missing, timeout, error) MUST produce a warning and
  never stop `hal on`, `hal off` or `hal serve`. Test: unit `test_gpu_light_failure_is_a_warning`.
- **REQ-CLI-53** (MUST) `hal kill` MUST stop `hal.target` and only the units recorded in
  `units.json` (H4), the gateway and its llama-server, every container carrying 02's
  `hal.sandbox=1` and `hal.uid=<uid>` labels, and every process recorded in a Hal pid file
  whose `/proc/<pid>/cmdline` still matches the record; it MUST NOT match units by a
  `hal-*` glob or processes by a `python -m hal...` pattern. Test: unit
  `test_kill_never_signals_unrecorded_or_reused_pids`, `test_kill_does_not_match_jobhunt`,
  `test_kill_leaves_unrecorded_hal_employment_units`.
- **REQ-CLI-54** (MUST) `hal kill --dry-run` MUST list what it would stop and change nothing.
  Test: unit `test_kill_dry_run_changes_nothing`.
- **REQ-CLI-55** (MUST) `hal status` MUST report on or off, GPU services running, gateway
  state and loaded models, managed llama-server preset, running Hal containers, pending
  approvals and the gpu-light line; `--json` MUST give the same as one object. Test: unit
  `test_status_json_fields`.

### Units and desktop launchers

- **REQ-CLI-56** (MUST) `hal units install` MUST render the unit templates of hal-core, the
  `hal-gateway.service` template shipped by hal-gateway (F8), and the `[[units]]` of every
  enabled module the hub reports, replacing `@ROOT@`, `@HAL@`, `@PYTHON@`,
  `@BIN@`, `@HOME@` and `@STATE@`, and MUST fail without writing anything when a template
  uses an unknown placeholder or a removed one (`@CORE@`, `@ECOSYSTEM@`, `@GPU_LIGHT@`,
  `@HAL_GPU_UNITS@`). Test: unit `test_render_placeholders`,
  `test_removed_placeholder_fails_before_any_write`.
- **REQ-CLI-57** (MUST) For a `.service` unit whose manifest entry has `gpu = true` (04), or
  whose template already contains `X-HalGPU=yes`, the renderer MUST emit `X-HalGPU=yes` and
  insert `ConditionPathExists=!<default state dir>/off` as the first lines after `[Unit]`
  (F9, compat item 4). Test: unit `test_gpu_unit_gets_marker_and_off_condition`.
- **REQ-CLI-58** (MUST) Unit names MUST be kept as the template file names, so
  `hal-employment.service` stays `hal-employment.service`, and the jobs pack's template keeps
  `@ROOT@` while its launcher reads `[jobs].jobhunt_root` at run time (H2). Test: unit
  `test_unit_names_are_template_names`, `test_hal_employment_unit_renders_to_golden`.
- **REQ-CLI-59** (MUST) Units installed earlier whose pack is gone or disabled MUST be
  disabled, stopped and deleted; units Hal did not install MUST never be touched. Test: unit
  `test_stale_units_removed_and_foreign_units_untouched`.
- **REQ-CLI-60** (MUST) `hal units install` MUST NOT start anything while the off flag exists,
  even with `--start`. Test: unit `test_install_start_is_ignored_while_off`.
- **REQ-CLI-61** (MUST) Desktop launchers MUST be rendered to the Desktop folder with mode
  0755 and marked trusted with `gio`, each call with a timeout. Test: unit
  `test_desktop_launcher_render_and_trust`.

### Packs

- **REQ-CLI-62** (MUST) `hal packs add NAME` MUST install only from the catalogue entry's
  pinned 40-hex commit (the tag is shown for people), and `hal packs add URL@REF` MUST refuse
  a REF that is neither a tag nor a full commit id, resolve a tag to a commit, and show both
  with the pack summary before asking for a typed `y`. Test: integration
  `test_add_installs_pinned_commit_from_local_bare_repo`, unit `test_branch_ref_is_refused`.
- **REQ-CLI-63** (MUST) After install, the installed commit MUST equal the pinned commit (PEP
  610 `direct_url.json` `vcs_info.commit_id` for pip-installed packs, `git rev-parse HEAD` for
  cloned own-env packs), else the install MUST be rolled back. Test: integration
  `test_commit_mismatch_rolls_back`.
- **REQ-CLI-64** (MUST) A module's `[[install]]` steps (04) MUST be shown verbatim (argv,
  working directory, summary, network flag) and run only after a typed `y` on a terminal,
  through `halhub.install.run_steps`; no flag skips this confirmation (B4), and without a
  terminal the steps stay `pending` and are listed. A failed step MUST leave the pack
  disabled with setup state `failed`. Test: unit
  `test_install_steps_need_typed_confirmation_and_failure_disables_pack`,
  `test_no_tty_leaves_install_steps_pending`.
- **REQ-CLI-65** (MUST) Adding a pack that is already installed at the pinned commit MUST do
  nothing (no setup rerun); `hal packs setup NAME` reruns setup explicitly. Test: unit
  `test_add_is_idempotent`.
- **REQ-CLI-66** (MUST) `hal packs enable|disable NAME` MUST edit only the `disabled` array of
  `[hub]` in `config.toml`, preserving all other bytes of the file, then re-render units.
  Test: unit `test_disable_edits_only_hub_disabled_line`.
- **REQ-CLI-67** (MUST) `hal packs remove NAME` MUST remove the pack's units, uninstall it and
  keep its data stores; an own-env pack folder MUST move to `data_dir()/removed/<name>-<UTC>`.
  Test: unit `test_remove_keeps_data_and_moves_folder`.
- **REQ-CLI-68** (MUST) `hal packs doctor` MUST report per pack: installed commit versus pin,
  local modifications (own-env packs), manifest validity through the hub, units rendered,
  recorded interpreters present, and setup state; it MUST exit 1 when any pack fails. Test:
  unit `test_packs_doctor_report`.
- **REQ-CLI-69** (MUST) `hal ext ...` MUST run the matching `hal packs ...` command after a
  one-line notice. Test: unit (part of `test_cli_aliases.py`).
- **REQ-CLI-103** (MUST) The installer MUST put `shared` packs in one venv at
  `data_dir()/envs/packs`, each `own` pack in `data_dir()/envs/<pack>`, and a pack that asks
  for system site packages (the `desktop` pack) in a `system` env built with
  `--system-site-packages` from `/usr/bin/python3`, and MUST record every module with its
  interpreter in `state_dir()/packs.json`, which the hub reads (C8). Test: unit
  `test_packs_json_records_interpreter_per_module`, integration
  `test_desktop_pack_gets_system_site_packages_env`.
- **REQ-CLI-110** (MUST) A catalogue pack in a `shared` or `system` env MUST get its
  dependencies and build backend only from the hashed requirements file that ships with the
  catalogue for that env kind, installed with `uv pip install --require-hashes`; the pack
  itself is then installed with `--no-deps --no-build-isolation`. A `URL@REF` pack MUST
  ship a `uv.lock`, whose hashed export is installed the same way (`--frozen`), or else the
  summary MUST list its unpinned dependencies and build requirements before the typed `y`
  of REQ-CLI-62. Test: unit `test_catalogue_install_uses_require_hashes_and_no_deps`,
  `test_url_pack_without_lock_lists_unpinned_dependencies`; integration
  `test_tampered_requirement_hash_aborts_install` (local index fixture).

### Policy, audit and sessions

- **REQ-CLI-70** (MUST) `hal policy explain TOOL [--args JSON]` MUST print the decision
  (`allow`, `ask`, `deny`), the rule, the tier and flags, and the approval key, computed with
  `halhub.policy.prepare`, `decide` and `explain` (01) without running the tool;
  `--exit-code` MUST map them to 0, 20, 21. Test: unit `test_policy_explain_never_calls_runner`.
- **REQ-CLI-71** (MUST) `hal policy edit` MUST edit a temporary copy, validate it with
  `halhub.policy.load_policy`, and replace `policy.toml` atomically (mode 0600) only when
  valid, keeping the previous file as `policy.toml.bak`. Test: unit
  `test_invalid_policy_edit_is_not_installed`.
- **REQ-CLI-72** (MUST) `hal sessions list` MUST read `state_dir()/sessions/<sid>/meta.json`
  and show id, last update (UTC), project, mode, model and title; `hal sessions export ID`
  MUST write `md` or `jsonl` from `transcript.jsonl` with secrets redacted by `halkit.redact`
  (E3). Test: unit `test_sessions_list_and_export_redacts`.
- **REQ-CLI-73** (MUST) `hal sessions resume ID`, `hal code --resume ID` and `/resume` MUST
  take the 12-hex session id, reopen the session in its original mode and project through ACP
  `session/load` with the host's records, and refuse when the project root no longer exists
  (E2). Test: integration `test_resume_loads_session_in_original_project`.
- **REQ-CLI-102** (MUST) `hal policy allow` MUST show the rule it will add to `policy.toml`
  and write it only after a typed `y` on a terminal; it MUST refuse when stdin is not a
  terminal or `HAL_HARNESS` is set (B2). Test: unit
  `test_policy_allow_needs_typed_confirmation_and_refuses_in_harness`.

### Console

- **REQ-CLI-74** (MUST) The console MUST bind only `127.0.0.1:[console].port`, MUST reject a
  request whose `Host` is not `127.0.0.1:<port>` or `localhost:<port>` with 403, and MUST
  require either the session cookie or `Authorization: Bearer <console-token>` on every
  `/api/` route. Test: integration `test_console_rejects_foreign_host_and_missing_token`.
- **REQ-CLI-75** (MUST) Every state-changing console request MUST be a POST with an `Origin`
  equal to the console's own origin, else 403, and with `Content-Type: application/json`,
  else 415 (01-security REQ-SEC-71, F10); this blocks the cross-origin `text/plain` POST that
  works today. Test: integration `test_console_rejects_cross_origin_and_text_plain_post`.
- **REQ-CLI-76** (MUST) `hal console --open` MUST obtain a one-time link (valid
  `[console].open_link_ttl_s`, single use) with the token file and open it; the link MUST set
  an `HttpOnly; SameSite=Strict` cookie and redirect to `/` without the token in the URL.
  Test: integration `test_open_link_is_single_use_and_expires`.
- **REQ-CLI-77** (MUST) Console pages MUST send `Content-Security-Policy: default-src 'self';
  form-action 'none'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'` and
  `X-Frame-Options: DENY`. Test: unit `test_console_security_headers`.
- **REQ-CLI-109** (MUST) The console page MUST insert model text, tool output and tool
  arguments with `textContent` only (any Markdown goes through an allowlist sanitizer that
  keeps no links, images, raw HTML or attributes), MUST show the characters 01-security
  REQ-SEC-25 lists (bidirectional controls, zero-width characters, U+00AD) as `\uXXXX`, and
  MUST keep the approve button of an approval disabled until every argument is shown in
  full, so the console's `shows_all_args` claim (01-security.md, "Approval surfaces and
  protocol") holds. Test: unit `test_console_renders_untrusted_text_as_text`
  (a `<img onerror>` argument stays text, U+202E shows as `\u202e`),
  `test_console_approve_disabled_until_all_args_shown`.

### Eval

The eval library and its task formats are owned by 03-agent.md section 3.18 (F4); `hal eval`
wraps it.

- **REQ-CLI-78** (MUST) `hal eval` MUST run every case in a `TempHalHome` with a generated
  policy that denies tiers `act`, `send` and `system` and every `network` tool, through an
  in-process broker owned by halcore (A2), and MUST NOT read or write the real config, state
  or vault. Test: integration `test_eval_never_touches_real_home` (real home replaced by a
  sentinel directory that must stay unchanged).
- **REQ-CLI-79** (MUST) A case's `confirm_first` check MUST pass only when the broker raised an
  approval request for that tool. Test: unit `test_confirm_first_uses_approval_requests`.
- **REQ-CLI-80** (MUST) Eval output MUST be written to
  `state_dir()/eval/<YYYYmmddTHHMMSSZ>-<model slug>/` in 03's report format, with UTC `Z`
  timestamps. Test: unit `test_eval_report_dir_and_timestamps`.
- **REQ-CLI-81** (SHOULD) `hal eval --suite coding` SHOULD supply 03's `BoxRunner` over
  `halsandbox.Session`, so each attempt runs and is graded in fresh boxes. Test: docker
  `test_coding_suite_runs_check_in_box`.

### Configure, doctor, migrate

- **REQ-CLI-82** (MUST) `hal configure` MUST create `config.toml` (0600) only when it is
  missing, and MUST never overwrite an existing key; `--fix-modes` MUST set 0600 on Hal's
  config and secret files. Test: unit `test_configure_never_overwrites_keys`.
- **REQ-CLI-83** (MUST) The profile detector MUST map VRAM and RAM to `max`, `24gb`, `12gb` or
  `cloud` with the thresholds of `setup.sh` (`profile_for_hardware`). Test: unit
  `test_profile_thresholds_match_setup_sh`.
- **REQ-CLI-84** (MUST) `hal configure --migrate` MUST read `hal.conf` as `KEY="value"` lines
  only, map them as in "Configuration migration", write `[jobs].jobhunt_root` with the current
  effective value when `[jobs]` is absent (H1), and print every change before writing. Test:
  unit `test_migrate_maps_hal_conf_keys` (synthetic fixture),
  `test_migrate_writes_explicit_jobhunt_root`.
- **REQ-CLI-85** (MUST) `hal doctor` MUST run every check in "Doctor checks" with its own
  timeout, print `ok`, `warn` or `fail` with a fix line per check, and exit 1 on any `fail`.
  Test: unit `test_doctor_check_timeouts_and_exit_code` (fake probes).
- **REQ-CLI-86** (MUST) `hal doctor --fix` MUST only apply local, reversible fixes (file
  modes, runtime directories, unit re-render) and MUST NOT use sudo or the network. Test: unit
  `test_doctor_fix_never_calls_sudo_or_network`.
- **REQ-CLI-87** (MUST) `hal migrate ecosystem` MUST record every change it makes (old
  symlink target, backed-up file, rendered unit) in a journal before making it, and
  `hal migrate undo JOURNAL` MUST restore the recorded state. Test: integration
  `test_migrate_then_undo_restores_tree_byte_for_byte` (TempHalHome with a synthetic
  ecosystem layout).
- **REQ-CLI-88** (MUST) `hal migrate ecosystem` MUST NOT write inside the `--from` folder, the
  vault, or any hal-employment folder; an existing `hal.conf` is only verified, never
  rewritten (H3). Test: integration `test_migrate_writes_nothing_under_from_vault_or_employment`.

### Installer

- **REQ-CLI-89** (MUST) `scripts/install.sh` MUST be idempotent, MUST support `--dry-run`
  (prints every mutating step, performs none), MUST NOT pipe a download into a shell, and
  MUST NOT call sudo (REQ-SYS-17). Test: slow `tests/e2e/test_install.py` (dry run twice,
  identical plans; a real run in a TempHalHome twice, second run changes nothing) and unit
  `test_install_sh_has_no_curl_pipe_or_sudo` (static scan).
- **REQ-CLI-90** (MUST) `scripts/install.sh` MUST NOT replace an existing `~/.local/bin` link
  that points outside its own venv unless `--replace-links` is given, and MUST name each such
  link. Test: slow `test_install_keeps_foreign_links`.
- **REQ-CLI-91** (MUST) Pack install steps run by the installer MUST follow REQ-CLI-64: the
  installer's `--yes` never answers them, and in a run without a terminal they stay pending
  and are listed at the end. Test: slow `test_install_yes_never_answers_pack_steps`.
- **REQ-CLI-92** (MUST) `scripts/host-setup.sh` MUST support `--dry-run`, MUST restart Docker
  only when no containers run, MUST NOT pipe a download into a shell, and MUST register
  `runsc` with `runtimeArgs ["--host-uds=open"]` when it is missing (G1). Test: unit
  `test_host_setup_static_rules` (static scan) and manual (run on this machine with
  `--dry-run`).

### Built-in `core` module

- **REQ-CLI-101** (MUST) hal-core MUST ship a manifest v2 module `core` (pack `core`) whose
  tool `core_long_task` is tier `act`, starts a sandboxed `hal -p` on the named workspace and
  returns at once with a job id; a request with `where = "cloud"` MUST be refused unless
  `[agent].cloud_jobs = true` (F2). Test: unit `test_long_task_manifest_tier_act`,
  integration `test_long_task_runs_sandboxed_headless_and_refuses_cloud_by_default`.

## Design

### Components

```text
packages/hal-core/
  pyproject.toml                  [project.scripts] hal = "halcore.cli:main"
                                  [project.entry-points."hal.packs"] core = "halcore.core_module"
  src/halcore/
    __init__.py                   version
    __main__.py                   python -m halcore -> cli.main
    cli.py                        argparse tree, aliases, exit codes, prints (entry point)
    launch.py                     launch target resolution, forbidden roots
    acp/
      client.py                   ACP v1 client (asyncio, stdlib JSON-RPC)
      transport.py                SubprocessTransport, PipeTransport (unboxed fallback)
      proxy.py                    hal acp: editor/channel <-> agent proxy
      content.py                  prompt blocks, @mentions, diff extraction
      records.py                  _hal/record validation and transcript writer
    surfaces.py                   approval surfaces (TUI, ACP, console, control, notify)
    sessions.py                   session assembly: code box, assistant box, unboxed fallback
    settings.py                   AgentSettings and run files for each session
    headless.py                   hal -p
    core_module/                  built-in hub module: hal-module.toml, tools.py (core_long_task)
    tui/
      app.py                      REPL loop, turn lifecycle, cancellation
      render.py                   sanitizer, streaming Markdown, tool calls, diffs
      input.py                    PromptSession, key bindings, completers
      commands.py                 slash commands
      status.py                   status line and banners
      approval_view.py            approval panel and key handling
    harness.py                    hal box, hal connect, hal claude
    gateway_cmds.py               hal gateway, hal serve, hal stop, hal models
    power.py                      on, off, toggle, status, kill
    pids.py                       pid files for hal kill
    units.py                      unit and desktop rendering, units.json
    packs.py                      hal packs, pack environments, packs.json
    cli_policy.py                 hal policy, hal approvals, hal approve, hal deny
    cli_audit.py                  hal audit, hal guard, hal secrets
    sessions_cmd.py               hal sessions
    evaluate.py                   hal eval over halagent.eval
    console.py  console.html      hal console
    configure.py                  hal configure, profiles, --migrate
    doctor.py                     hal doctor
    migrate.py                    hal migrate ecosystem / undo
    data/
      catalog.toml                pinned pack catalogue
      systemd/                    hal.target, hal-console.service
      desktop/                    hal-console.desktop, hal-toggle.desktop, hal-kill.desktop
      eval/                       copy of the root eval/ at build time (build include)
  tests/                          one test file per module
scripts/install.sh, scripts/host-setup.sh   (repo root)
```

01-security's file layout names `halcore/cli/{policy,approvals,audit,guard,secrets}.py`;
halcore keeps one flat module per command group (`cli_policy.py`, `cli_audit.py`) because
`cli.py` is the only printing module and a `cli/` package would shadow it. The commands and
their behaviour are 01's.

`cli.py` and `__main__.py` are the only modules that print (REQ-SYS-15); every other module
returns values or raises `HalError` subclasses, and logs through `logging`.

Imports used by halcore (allowed by 00-overview "Allowed imports"): `halkit`, `halhub`
(policy engine, approvals, registry, install steps), `halagent` (settings, headless renderer,
eval library, and the unboxed assistant), `halsandbox` (sessions, backends, checkpoints, box),
`halgateway` (client, forwarder, models, gpu-light adapters), `halremote` (`hal remote`
delegates).

### Interfaces consumed from other packages

These are the calls halcore makes. The owning spec fixes the final names and signatures; this
table points at the owner's section and does not restate it. If an owner's text changes, this
table is updated in the same commit.

| Need | Interface (owner section) |
| --- | --- |
| Sandboxed session (code, assistant box, unboxed) | `halsandbox.Session.start(project, *, kind, backend, isolated, image, approver, channel, policy_profile, checkpoints, run_files, child_argv, ...)` with `.id`, `.backend`, `.model_socket`, `.agent_argv`, `.checkpoint()`, `.undo()`, `.diff()`, `.report()`, `.apply_isolated()`, `.close()` (02-sandbox.md, "Session") |
| Human-message hook | `Broker.on_human_message(session, text, surface)` (01-security, F5) on the session's broker; 02 exposes the broker of a `Session` to halcore (requested of 02) |
| Run files that need the session id | `Session.start(run_files=...)` accepting a callable `(sid) -> Mapping[str, bytes]` as well as a mapping (requested of 02) |
| Approval model and router | `halhub.approvals`: `ApprovalRequest`, `Approver` protocol, `Decision(verdict, scope)`, keys, control socket server and client (01-security.md, "Approval binding", "Approval surfaces and protocol") |
| Policy without running | `halhub.policy.load_policy`, `prepare`, `decide`, `explain` (01-security.md, "Decision algorithm" and "File layout") |
| Audit reading | `state_dir()/audit/<UTC date>.jsonl`, v1 schema (01-security.md, "Audit log") |
| Agent settings and run files | `halagent` `AgentSettings.to_json()` (03-agent.md, 3.16.3) |
| Agent argv in a box | `hal-agent --acp --toolset coding|assistant --config /run/hal/agent.json` (03-agent, F3) |
| Unboxed assistant | `halagent.acp.serve(...)` with `toolset="assistant"` and the same `AgentSettings` (03-agent, F3) |
| Headless rendering | `halagent.headless.Renderer` (03-agent.md, 3.15) |
| Transcript schema | record types of 03-agent.md, 3.10.2 |
| Eval | `halagent.eval` scenario and coding runners, `BoxRunner` protocol (03-agent.md, 3.18) |
| Pack registry | `halhub.registry.Registry.load()`, `ModuleSpec.units`, `ModuleSpec.desktop` (04-hub.md, "Discovery and registry") |
| Locating manifests in an env | 04-hub.md, "Locating a manifest without importing code" |
| Install steps | `halhub.install.plan(module)`, `run_steps(module, confirm=...)` (04-hub.md, "Install steps and units") |
| Gateway control | `halgateway.client.GatewayClient`: `status()`, `aliases()`, `set_mode()`, `stop_router()`, `release_gpu(force=False, *, stop_router=False, reclaim=False)`, `create_session()`, `revoke_session()`; token issue for named client tokens (05-gateway.md, "Sessions and the forwarder", D5) |
| Model forwarder | `halgateway.forwarder.SessionForwarder`, `ForwarderLimits`, `ForwarderEvent` (05-gateway.md, "Sessions and the forwarder"); the status line budget (REQ-CLI-22) reads the `request` and `rejected` events (requested of 05: each `request` event's `detail` carries the `tokens` counted against REQ-GW-69, and each budget `rejected` event carries `retry_after_s`) |
| Model manifest | `halgateway.models` list, pull, verify (05-gateway, D10) |
| gpu-light | `halgateway.gpu` autodetect and `halgateway.gpu.lightv1` claims (05-gateway.md, "GPU coordination", D11) |
| Gateway unit template | `halgateway/data/hal-gateway.service` with `@BIN@` (`ExecStart=@BIN@/hal-gateway serve`; 05-gateway.md, "systemd unit") |
| Box templates per CLI | `halsandbox.box.run(...)`, `halsandbox.box.harnesses()`, `halsandbox.images.build()/status()` (02-sandbox.md, "hal box", "Commands") |
| Sandbox doctor | `halsandbox.doctor.checks()` (02-sandbox.md, "Commands") |
| Pack list | 08-packs-and-flagships.md, "hal-tools pack mapping" |

### Command reference

Global options, accepted before or after the subcommand where it makes sense:
`--config PATH` (sets `HAL_CONFIG`), `--policy-profile NAME`, `-v/--verbose`, `--no-color`,
`--version`, `-h/--help`.

```text
hal                                  TUI; code mode in a project, else chat (see resolution)
hal code [DIR] [--isolated] [--backend auto|docker|bwrap|none] [--model ALIAS]
         [--mode auto|review] [--resume SID | --continue] [--no-checkpoint]
         [--deps] [--unmask]
hal chat [MESSAGE...] [--model ALIAS] [--resume SID | --continue]
hal -p TASK|- [--json | --stream-json] [--dir DIR] [--chat] [--isolated]
         [--approvals deny|queue[:SECONDS]] [--timeout SECONDS] [--model ALIAS]
         [--include-thinking] [--no-checkpoint] [--resume SID] [--notify]
hal acp [DIR] [--channel NAME] [--toolset coding|assistant] [--isolated] [--backend B]
hal box [--dir DIR] [--backend B] [--isolated] [--image REF] [--model ALIAS] [--ask]
        [--no-mcp] [--no-checkpoint] -- CMD [ARGS...]
hal box image build [NAME] | status
hal box harnesses
hal connect HARNESS [--write] [--shell bash|zsh|fish] [--json]
hal connect --list
hal claude [--preset NAME] [--model NAME] [--vault | --dir DIR] [--cloud] [CLAUDE_ARGS...]
hal gateway ARGS...                  delegates to hal-gateway (05-gateway "CLI")
hal models list [--json]
hal models pull NAME... | --profile P | --preset P [--yes] [--dry-run]
hal models verify [NAME...] [--full] [--json]
hal serve [PRESET] [--no-wait] | --list
hal stop [--all]
hal on [--quiet]
hal off [--quiet] [--force]
hal toggle [--quiet]
hal status [--json]
hal kill [--quiet] [--dry-run]
hal packs list [--available] [--json]
hal packs add NAME... | --set SET | URL@REF | --local PATH [--no-setup]
hal packs remove NAME [--yes]
hal packs enable NAME | disable NAME
hal packs update [NAME...]
hal packs setup NAME
hal packs doctor [NAME...] [--online] [--json]
hal units install [--start] [--no-desktop] [--dry-run]
hal units list [--json]
hal units remove PACK
hal eval [--suite assistant|coding|all] [--model ALIAS]... [--only IDS] [--category C]
         [--repeat N] [--json] [--keep]
hal console [--port N] [--open] [--service]
hal configure [--profile auto|max|24gb|12gb|cloud] [--vault DIR] [--chat-model M] [--yes]
hal configure --show | --path | --fix-modes | --migrate [--from FILE] [--dry-run]
hal policy show [--profile P] [--channel C] [--json]
hal policy check [FILE]
hal policy explain TOOL [--args JSON | --args-file F] [--channel C] [--profile P]
                   [--session-kind K] [--taint T] [--json] [--exit-code]
hal policy init [--force]
hal policy edit
hal policy allow TOOL [--args JSON] [--pack P] [--profile P]
hal approvals [list] [--json]
hal approvals watch
hal approve ID [--scope once|session]
hal deny ID [--note TEXT]
hal audit tail [-n N] [--session SID] [--json]
hal audit show [--date YYYY-MM-DD] [--session SID] [--json]
hal guard status | install | bench [--labelled FILE]
hal secrets set PACK NAME | list [PACK] | rm PACK NAME
hal sessions list [--all | --project DIR] [--limit N] [--json]
hal sessions resume SID
hal sessions export SID [--format md|jsonl] [-o FILE] [--with-tool-output]
hal sessions rm SID [--yes]
hal doctor [--only IDS] [--json] [--fix]
hal migrate ecosystem [--from DIR] [--dry-run] [--yes]
hal migrate undo JOURNAL
hal remote ...                       delegates to hal-remote (07-remote)
hal ask|index|brief|summarize|import|obsidian-key|register-repos ...   hal-memory CLI
hal version | hal help [COMMAND]
```

The policy, approval, audit, guard and secrets commands behave as 01-security defines them
(B12); halcore owns their names, flags and exit codes. `hal guard bench` exists because
REQ-SEC-38 needs it. `hal gateway` execs `hal-gateway` from the Hal venv with argv unchanged;
05 owns its subcommands, including `token issue|revoke|list` (D5) and the `release-gpu`
command that gpu-light calls as a command reclaimer (D7). `--yes` appears only where it
answers a question that is not a safety prompt (removal, migration plan, config creation),
plus `hal models pull` (F4); it never answers an approval or a pack install step.

Exit codes (all commands unless stated):

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | The operation failed (a check failed, a download failed, a pack failed doctor); headless: the agent failed during execution |
| 2 | Usage error or config error (bad flag, malformed TOML, unknown key, forbidden root, unknown session id) |
| 3 | Missing prerequisite (no `config.toml`, gateway or model unreachable and cannot start, no image in a non-interactive run, uv missing, refused sandbox fallback) |
| 4 | Headless: stopped by a budget (`max_tokens`, `max_turn_requests`) |
| 5 | Headless: the model refused (`refusal`) |
| 20, 21 | `hal policy explain --exit-code`: `ask`, `deny` |
| 124 | Headless: `--timeout` expired |
| 130 | Interrupted by SIGINT or cancelled |

Legacy aliases (REQ-CLI-03): `hal here [ARGS]` -> `hal claude [ARGS]`; `hal mode NAME [here]`
-> `hal claude --preset NAME` (with `--vault` unless `here`); `hal model NAME` -> `hal claude
--model NAME --vault`; `hal ext ...` -> `hal packs ...`; `hal install` -> `hal units install
--start`; `hal serve`/`hal stop` keep their names. Each prints, on stderr,
`hal: 'hal here' is now 'hal claude'` and runs. The aliases are removed at 1.0.

### Launch target resolution

`launch.resolve_target(cwd: Path, requested: Literal["auto", "code", "chat"], *, home: Path)
-> LaunchTarget` with `LaunchTarget(mode: Literal["code", "chat"], root: Path | None,
is_git: bool, reason: str)`. `ForbiddenRoot(HalError)` carries the path and the rule.

| Situation | `hal` (auto) | `hal code` | Reason shown |
| --- | --- | --- | --- |
| cwd inside a git work tree whose top level is allowed | code, root = top level | same | `git repository` |
| git top level is forbidden (for example a dotfiles repo at `$HOME`) and cwd has a project marker | code, root = cwd | same | `project marker <name>; git root is your home` |
| git top level is forbidden, no marker | chat | exit 2 | `your home directory is never a workspace` |
| not git, cwd has a project marker | code, root = cwd (non-git rules of 02 apply: `--isolated` or the shadow git repository) | same | `project marker <name>` |
| not git, no marker | chat | code, root = cwd, if allowed | `no project here` |
| cwd itself forbidden | chat | exit 2 | rule name |

Project markers: `AGENTS.md`, `CLAUDE.md`, `pyproject.toml`, `package.json`, `Cargo.toml`,
`go.mod`, `Makefile`, `CMakeLists.txt`, `.agents/`. Git detection runs
`git rev-parse --show-toplevel` with a 5 s timeout. Forbidden: `/`, `$HOME`, any ancestor of
`$HOME`, anything at or under `config_dir()`, `state_dir()`, `data_dir()`, `~/.config`,
`~/.local`, and any path that is, contains or lies inside a directory of 01's built-in
hard-deny list (`~/.ssh`, `~/.aws`, `~/.kube`, `~/.password-store`, `~/.mozilla` and the
rest; file patterns such as `.env` are left to 02's masking). A root that contains a
`[paths].deny` path or `[hal].vault` is allowed only after the typed confirmation of
REQ-CLI-09. `[ui].default_mode = "code"|"chat"` overrides auto.

The vault is not a code workspace by default. `hal claude --vault` keeps the old "Claude Code
in the vault" path; Hal's own assistant reaches the vault only through hal-memory tools on the
broker (00-overview D3).

### Process model

```text
hal code (TUI)                               hal chat / hal acp --channel voice
-----------------------------------------    ---------------------------------------------
halcore asyncio loop                         halcore asyncio loop
  AcpClient --SubprocessTransport--> docker    AcpClient --SubprocessTransport--> docker
     stdio of hal-agent --acp                     stdio of hal-agent --acp --toolset assistant
     --toolset coding (box, /work = project)      (assistant box, /work = scratch dir)
  broker thread (halsandbox) <-MCP- box        broker thread (halsandbox) <-MCP- box
  model forwarder thread    <-HTTP- box        model forwarder thread    <-HTTP- box
  control socket server (approvals)            control socket server (approvals)
  checkpoints before each turn                 no checkpoints (scratch dir only)
```

Both columns follow Flow 1 of 00-overview; the assistant column is 00's Flow 2 (A1). The
unboxed fallback (REQ-CLI-96) replaces the right column's docker child with
`halagent.acp.serve` on a thread over `PipeTransport`; the broker, forwarder and control
socket are the same objects.

### Code session assembly

`sessions.open_code(target, *, isolated, backend, model, mode, approver_surfaces)`:

1. Load config and policy (`halkit.config`, `halhub.policy.load_policy`). A malformed policy
   means the broker does not start and the session has no host tools, with the reason shown
   (00-overview "Failure modes").
2. Resolve the backend with 02's probe. Apply REQ-CLI-97: docker seen before and down now is
   a refusal; backend `none` is left to 02's per-project consent (REQ-BOX-68), which
   `Session.start` asks for in step 4. Apply REQ-CLI-111: a work tree that holds a
   registered module's source or an editable `hal-*` checkout needs `--isolated` or the
   recorded confirmation.
3. Render settings (`settings.py`): an `AgentSettings` for mode `code` with the resolved
   model profile, budgets, `endpoint = unix:///run/hal/model.sock`,
   `broker = unix:///run/hal/broker.sock`, `host_root` = the project, the user's
   `config_dir()/AGENTS.md` and skills as run files, and the user MCP servers from 03's
   `[mcp.servers.<name>]` that may run in the box (REQ-CLI-98 for backend `none`). The
   settings carry the 12-hex session id, which 02 generates inside `start`, so halcore passes
   the run files as a callable `(sid) -> Mapping[str, bytes]` that 02 calls after creating
   the runtime dir and before the box starts (requested of 02: `run_files` accepts a mapping
   or such a callable). 02 rejects absolute paths, `..`, symlinks and the session token value
   in run files.
4. `halsandbox.Session.start(project, kind="code", backend=..., isolated=...,
   interactive=<stdin is a TTY>, approver=<01 router over halcore surfaces>,
   channel="tui" or "editor",
   policy_profile=..., run_files=..., child_argv=("hal-agent", "--acp", "--toolset",
   "coding", "--config", "/run/hal/agent.json"))`.
5. Create a gateway session token (`GatewayClient.create_session`) and start
   `SessionForwarder(session.model_socket, ...)`.
6. Start the control socket for the session (`halhub.approvals`, B3).
7. Spawn `session.agent_argv` with stdin and stdout pipes; `initialize` with
   `clientCapabilities._meta.hal.records = true`; `session/new` with `cwd` = the project and
   `mcpServers = []` (the broker comes from `agent.json`, E5).
8. Show 02's masking notice and any dependency-prep offer (REQ-CLI-106, REQ-CLI-107).

Teardown: `session/cancel` if a turn runs, close the agent pipes, `session.close()`, close
the forwarder, revoke the gateway session, close the control socket, mark `meta.json`
`closed`.

### Assistant channels

`hal chat`, `hal acp --channel NAME --toolset assistant`, the console and `hal eval` call
`sessions.open_assistant`:

```python
def open_assistant(channel: str, surfaces: Sequence[Approver], *,
                   model: str | None = None,
                   policy_profile: str | None = None) -> AssistantSession: ...

class AssistantSession:
    client: AcpClient            # talks to the agent in the assistant box (or unboxed)
    session: halsandbox.Session  # kind "chat"; owns the broker
    boxed: bool                  # False only for the backend-none fallback
    acp_session_id: str          # 12 hex, the same id as the runtime dir (E2)
    async def prompt(self, blocks: list[dict]) -> str: ...     # returns ACP stopReason
    async def new_conversation(self) -> str: ...               # session/new in the same box
    async def cancel(self) -> None: ...
    async def close(self, timeout: float = 5.0) -> None: ...
```

Assembly differs from a code session in four points:

1. `/work` is `state_dir()/assistant/<channel>/work` (created 0700 and kept across runs as
   the channel's scratch space), never the vault (A1, 00-overview D3).
2. Settings mode `assistant`, child argv
   `("hal-agent", "--acp", "--toolset", "assistant", "--config", "/run/hal/agent.json")`,
   checkpoints off, `kind="chat"`.
3. The box lives as long as the `hal` process (REQ-CLI-95). Voice and Telegram keep one
   `hal acp --channel` process for their lifetime, so their turns never pay a container
   start; `hal chat` pays one warm start per invocation (0.33 s runc, 0.26 s runsc on this
   machine).
4. Backend `none`: the unboxed fallback of REQ-CLI-96. `Session.start(kind="chat",
   backend="none")` still creates the runtime dir, broker and forwarder; halcore does not
   spawn `agent_argv` and instead runs `halagent.acp.serve(...)` on a thread with the same
   `AgentSettings` (sockets as host paths). The agent then has only the broker client for
   host actions, and with no `run` tool and no user stdio MCP servers (REQ-CLI-98) it has
   nothing else to act with. 01-security's residual risks list this mode (R-new-8, R-new-9).

Channel approvals: broker `ask` decisions reach the channel client as host-originated
`session/request_permission` (REQ-CLI-38). hal-voice speaks the host-made summary and takes
yes or no from the microphone path only (voice surface, `shows_all_args = false`); the comms
pack's Telegram bridge shows inline buttons whose callback carries the approval id and a nonce
(B9, owned by 01 and 08). Pending channel approvals are also listed by `hal approvals`, so a
desktop user can answer a Telegram- or voice-started request, and calls with `content`
parameters reach only surfaces that show every argument (REQ-CLI-93).

### Built-in `core` module and long jobs

hal-core registers one hub module through the `hal.packs` entry point (C2), so long jobs are
an ordinary tool under policy and audit (F2):

```toml
# halcore/core_module/hal-module.toml
schema = 2

[module]
name = "core"
pack = "core"
summary = "Hal core: long sandboxed jobs"
python = "halcore.core_module.tools"

[[tools]]
name = "core_long_task"
title = "Start a long coding job"
description = "Start a long job: a sandboxed coding agent works on TASK in WORKSPACE and notifies you when done. Returns a job id at once."
tier = "act"
network = false
untrusted_output = false
reviewable = false
reversible = false
timeout = 30

[tools.params.task]
type = "string"
required = true
format = "text"
content = true
max_length = 8000

[tools.params.workspace]
type = "string"
required = true
format = "path"
access = "write"

[tools.params.where]
type = "string"
enum = ["local", "cloud"]
default = "local"
```

Key names follow 04's manifest v2 as amended by C1 (`format`, `access`, `reversible`); 04's
schema is authoritative if a key is spelled differently there.

`core_long_task(task, workspace, where)`:

1. Refuse `where = "cloud"` with `error_type = "cloud_jobs_off"` unless `[agent].cloud_jobs`
   is true (03 owns the key) and `[long_task].cloud_model` names a gateway alias.
2. Check `workspace` with the launch rules (REQ-CLI-09); the broker has already checked the
   path against 01's path rules.
3. Start, detached (`start_new_session=True`, pid file for `hal kill`):
   `hal -p - --json --isolated --dir <workspace> --approvals queue:<[long_task].queue_timeout_s>
   --notify [--model <cloud_model>]` with the task on stdin, output to
   `state_dir()/jobs/<sid>.json`. At most `[long_task].max_concurrent` jobs run at once.
4. Return `{"job": "<sid>", "result_file": "...", "approvals": "hal approvals"}`.

`--notify` makes `hal -p` send the final status line through `halkit.notify` when it exits.
The job's approvals reach the user through its control socket (`hal approvals`); the
channel that started the job is told the job id, not asked for approvals, because the job may
outlive that channel's process. "Cloud" means a cloud model upstream behind the gateway: the
job still runs in the box under the same policy (Decision D15). Because the job runs with
`--isolated`, its changes stay in the isolated worktree until the user reviews them with
`hal sessions resume <sid>` and `/apply`.

### ACP client

```python
class Transport(Protocol):
    async def send(self, message: dict[str, object]) -> None: ...
    async def receive(self) -> dict[str, object] | None: ...   # None at EOF
    async def close(self, timeout: float) -> None: ...

class SubprocessTransport:    # newline-delimited JSON-RPC on a child's stdin/stdout
    def __init__(self, argv: Sequence[str], *, env: Mapping[str, str], stderr_log: Path): ...

class PipeTransport:          # unboxed assistant on a thread, os.pipe pairs
    def __init__(self, serve: Callable[[BinaryIO, BinaryIO], None]): ...

class ClientHandler(Protocol):
    async def on_update(self, session_id: str, update: dict[str, object]) -> None: ...
    async def on_record(self, session_id: str, record: dict[str, object]) -> None: ...
    async def request_permission(self, session_id: str, tool_call: dict[str, object],
                                 options: list[dict[str, object]]) -> dict[str, object]: ...

class AcpClient:
    def __init__(self, transport: Transport, handler: ClientHandler, *,
                 request_timeout_s: float = REQUEST_TIMEOUT_S): ...
    async def initialize(self) -> dict[str, object]: ...
    async def new_session(self, cwd: str, mcp_servers: list[dict[str, object]]) -> str: ...
    async def load_session(self, session_id: str, cwd: str,
                           records: list[dict[str, object]]) -> None: ...
    async def prompt(self, session_id: str, blocks: list[dict[str, object]]) -> str: ...
    async def cancel(self, session_id: str) -> None: ...
    async def set_mode(self, session_id: str, mode_id: str) -> None: ...
```

- Protocol version 1. The client advertises no `fs` and no `terminal` capability (the agent
  does file work inside the box, and a host terminal would bypass the sandbox), and
  advertises `_meta.hal.records = true` (03-agent.md 3.14.2).
- `load_session` passes the host's transcript records in `_meta.hal.records` (03-agent.md
  3.14.2).
- `prompt` has no request timeout (turns can run for an hour); it ends on the stop reason or
  on cancellation. All other requests use `REQUEST_TIMEOUT_S = 30` (seconds; covers a
  `session/load` replay of a long transcript on this machine [unverified: measure with a
  2,000-message transcript]).
- An agent that writes invalid JSON or exits ends the session with `AgentCrashed`; the TUI
  reports it and offers resume (00-overview: box exit keeps transcript and checkpoints).
- Limits (REQ-CLI-108): `SubprocessTransport` reads stdout with
  `asyncio.StreamReader(limit=ACP_MAX_MESSAGE_BYTES)`, so an over-long line raises before more
  is buffered, and parses with a depth check of `ACP_MAX_JSON_DEPTH` (a pre-scan of bracket
  depth outside strings before `json.loads`); either failure is `AgentCrashed` with the reason.
  `ACP_MAX_MESSAGE_BYTES = 8 * 2**20` is well above the largest legitimate message (a
  `session/load` replay is sent by the host, not read from the agent; agent updates are
  chunks) [unverified: measure the largest `tool_call` update in 03's eval runs], and
  `ACP_MAX_JSON_DEPTH = 64` is far deeper than any ACP schema nests. `stderr_log`
  is a `logging.handlers.RotatingFileHandler` with `maxBytes = AGENT_STDERR_MAX_BYTES =
  10 * 2**20` and `backupCount = 2`, fed line by line, each line truncated to the same cap.

### Transcripts and `_hal/record`

`acp/records.py` validates each `_hal/record` against the record schema of 03-agent.md
3.10.2, accepts `session`, `message`, `compaction`, `notice` and `turn_end`, refuses
`checkpoint` (only the host writes it, after each `session.checkpoint()`), and appends to
`state_dir()/sessions/<sid>/transcript.jsonl` through `halkit.files.append_jsonl` with no
rotation and a 256 MiB cap (REQ-CLI-100). `<sid>` is the 12-hex id of the runtime dir, the
checkpoint refs and the ACP `sessionId` (E2). halcore also writes `meta.json` beside it (see
"Sessions"). A transcript write failure is a notice, not a stop (Failure modes).

### TUI

#### Screen

```text
hal 0.1.0 | code | ~/src/parser (git main) | docker-runsc | policy default
AGENTS.md loaded. /help lists commands. Ctrl-C cancels a turn, Ctrl-D exits.

> fix the failing test @tests/test_parser.py

  read   src/parser.py:1-120                                   box   ok
  run    pytest -q tests/test_parser.py                         box   exit 1  3.2s
         F......
         ... 14 more lines (/tools 2)
  edit   src/parser.py                                          box   +3 -1
         @@ -42,7 +42,9 @@
         -    if not text:
         +    if text is None or not text.strip():
  web_read  https://docs.python.org/3/library/re.html           host  ok (approved)

The test failed because empty input reached the tokenizer. I now reject blank input ...

[hal-code qwen3.8-27b | ctx 23% 30k/131k | docker-runsc | auto | policy default | ckpt 3]
```

#### Rendering (`tui/render.py`)

```python
def sanitize_terminal_text(text: str) -> str: ...
class MarkdownStream:
    def __init__(self, console: rich.console.Console, *, refresh_hz: int): ...
    def feed(self, delta: str) -> None: ...      # prints closed blocks, updates the live tail
    def close(self) -> None: ...                 # prints the tail
def render_tool_call(update: dict[str, object], *, max_lines: int) -> rich.console.RenderableType: ...
def render_diff(path: str, old: str | None, new: str, *, max_lines: int) -> rich.console.RenderableType: ...
```

- Sanitizer: removes `\x00-\x08`, `\x0b-\x1f` (keeps `\t` and `\n`), `\x7f`, `\x80-\x9f`,
  and whole escape sequences (CSI `ESC [ ... final`, OSC `ESC ] ... BEL|ESC \`, DCS, SOS,
  PM, APC, and any lone `ESC x`), then escapes the REQ-SEC-25 characters with
  `halkit.termsafe` (one implementation, C10). Needed because rich passes escapes through
  [verified above]: OSC 52 writes the clipboard and CSI sequences can redraw the screen to
  fake an approval.
- `MarkdownStream` keeps a buffer. A block is closed at a blank line outside a code fence
  when the next line is not indented and not a list item. Closed blocks are printed once with
  `rich.markdown.Markdown(text)`; the open tail is shown in a `rich.live.Live` region at
  `[ui].markdown_refresh_hz`. This keeps redraw cost constant for long answers and avoids the
  duplicated lines `Live` produces when its region is taller than the terminal.
- Text is always passed as data (`Markdown(text)`, `Text(text)`), never through
  `console.print(str)` markup parsing (REQ-CLI-12).
- Tool call kinds from ACP (`read`, `edit`, `delete`, `move`, `search`, `execute`, `think`,
  `fetch`, `other`) pick the verb column. Where: `box` for agent tools, `host` for broker
  tools (the broker marks its tool calls in `_meta` `hal/origin = "broker"`, set by the host
  forwarder, not by the agent).
- Diffs: ACP `diff` content (`path`, `oldText`, `newText`) rendered with `difflib.unified_diff`
  (3 lines of context) and `rich.syntax.Syntax(lexer="diff")`.
- Thought chunks: hidden, replaced by `thinking (12s)` updated once a second; shown dimmed when
  `[ui].show_thinking` is true.

#### Input (`tui/input.py`)

- `prompt_toolkit.PromptSession` with a `FileHistory` at `state_dir()/input-history`
  (mode 0600), multiline buffer, Enter submits, Alt-Enter and Ctrl-J insert a newline, a
  trailing `\` followed by Enter continues the line, bracketed paste on.
- Completers: slash commands (local plus agent-advertised), `@` paths. The path list comes
  from `git ls-files -co --exclude-standard` (timeout 5 s) in git projects, else a walk that
  skips `.git`, `node_modules`, `.venv`, `__pycache__` and stops at `MENTION_SCAN_MAX = 5000`
  entries (keeps completion under a second on large trees [unverified: time on a 50k-file
  tree]).
- Mentions on send: `@src/a.py` becomes `{"type": "resource", "resource": {"uri":
  "file:///home/u/src/parser/src/a.py", "mimeType": "text/x-python", "text": "..."}}` when
  the file is text and at most `[ui].mention_max_bytes`; otherwise `{"type": "resource_link",
  "uri": "file:///home/u/src/parser/src/a.py", "name": "a.py"}`. URIs are host paths; the
  agent maps them to `/work` (03-agent.md 3.14.5). Chat mode has no project: mentions embed
  the file text after confirmation, or are refused when too large.

#### Turn lifecycle and cancellation (`tui/app.py`)

1. User submits. Local slash command? Run it and return to the prompt.
2. Code mode: `session.checkpoint()`. On failure the turn does not start; the message names
   the error and suggests `--no-checkpoint` (00-overview "Failure modes").
3. `broker.on_human_message(session, text, surface="tui")` (REQ-CLI-94).
4. `client.prompt(...)` runs as a task; updates render as they arrive.
5. Ctrl-C: `client.cancel()`, pending ACP permission requests answered
   `{"outcome": {"outcome": "cancelled"}}`, pending broker approvals denied with source
   `cancel`. If no stop reason arrives within `CANCEL_GRACE_S = 5` seconds, the footer says
   `agent not responding: Ctrl-C again stops the sandbox`; a second Ctrl-C calls
   `session.close()` and the next prompt restarts the box and runs `session/load`. Five
   seconds is the ACP agent's time to finish in-flight tool updates (ACP says clients should
   keep accepting updates after `session/cancel`) and is short enough not to feel stuck.
6. Stop reason shown when it is not `end_turn`: `stopped: token budget`, `stopped: turn
   budget`, `the model refused`, `cancelled`.

#### Status line (`tui/status.py`)

Shown as the prompt_toolkit bottom toolbar while idle and as the last line of the live region
during a turn:

```text
[<alias> <backend model> | ctx <pct>% <used>/<size> | <backend>[ (no runsc)|UNSANDBOXED] | <mode> | policy <profile> | ckpt <n> | budget <req>/min <tok>/h]
```

Context comes from ACP `usage_update` (`used`, `size`); before the first update it shows
`ctx ?`. The backend comes from the sandbox session (`docker-runsc`, `docker-runc`, `bwrap`,
`none`). `(no runsc)` appears when docker runs on runc because runsc is absent or failed its
probe (for example registered without `--host-uds=open`). Chat mode omits `ckpt`.

The budget field (REQ-CLI-22) is the host-side limit of 05 REQ-GW-69, which binds every
process in the box, not the agent's own budgets (03 REQ-AGT-16). `<req>` is
`max_requests_per_min` minus the requests in the last 60 s and `<tok>` is
`max_tokens_per_hour` minus the tokens counted in the last 3600 s, both from the
`ForwarderLimits` halcore built from `[gateway.sessions]` and the forwarder's `on_event`
`request` events; tokens show as `k` or `M` (`1.8M/h`). After a `rejected` event naming a
budget limit the field reads `budget wait <n>s` (from `Retry-After`) until that time passes.
The field turns to a warning colour below 10 percent of either limit. Chat mode shows it too,
because assistant sessions have a forwarder as well.

#### Slash commands (`tui/commands.py`)

| Command | Behaviour |
| --- | --- |
| `/help [cmd]` | Lists local and agent commands with one line each |
| `/model [ALIAS]` | Without argument lists gateway aliases (`GatewayClient.aliases()`) and their backend models; with one, re-renders `agent.json` with the new model, restarts the agent process in the same box and reloads the session with `session/load` (transcript kept); hal-agent offers no ACP config option for the model (03-agent.md 3.14.2) |
| `/undo [N]` | Code mode: shows the files that would change and those 02's `DiffReport` marks as not restorable, asks y/N, restores the workspace to the checkpoint before the last turn (or N turns back) with `session.undo`, and prefixes the next prompt with a short note listing the reverted files so the agent's picture of the workspace stays true (REQ-CLI-105) |
| `/diff [all]` | Diff of the workspace against the checkpoint before the last turn; `all` against checkpoint 0; flags from `session.report()` |
| `/apply` | `--isolated` only: shows the diff, asks y/N, applies it to the real workspace with `session.apply_isolated()` |
| `/compact` | Forwarded to the agent as prompt text (03-agent advertises it); progress from ACP `compaction_update` |
| `/clear` | New ACP session (`session/new`) in the same box; checkpoints continue |
| `/resume [SID]` | Lists the 10 most recent sessions of this project (or chat), loads the chosen one with `session/load` |
| `/tools [N]` | Without argument: tools available now (box tools, then broker tools with tier); with N: full output of tool call N of the last turn |
| `/policy` | Profile, channel, the rules that matched in this session, counts of allow, ask and deny |
| `/mode [auto|review]` | ACP `session/set_mode`. `auto`: no prompts inside the box; `review`: every write, edit and run asks through ACP permission (modes defined by 03-agent, F3) |
| `/cost` | Tokens in and out, cached prompt tokens when reported, context use, turns, tool calls (box and host), wall time, and `cost` when an upstream reports one |
| `/exit` | Closes the session (box stops, checkpoints and transcript kept) |

#### Approval prompts (`tui/approval_view.py`)

```text
+-- Approval needed: leaves the box -----------------------------------------+
| tool    mail_send   pack comms       tier  send: sends data off this machine |
| reason  profile default: tier send -> ask                                   |
| key     d3a842d997b0  (sha256 of pack, tool and canonical arguments)        |
| args    {                                                                   |
|           "body": "See you at 10.",                                         |
|           "subject": "Notes",                                               |
|           "to": "ann@example.org"                                           |
|         }                                                                   |
| [y] allow once  [s] allow these exact args this session  [n] deny          |
| [d] deny with a note  [v] view all                                          |
+----------------------------------------------------------------------------+
```

- Box requests use the title `Inside the box (no effect outside)` and a plain border; broker
  requests use the layout above. The agent cannot draw the broker layout: broker panels are
  built only from `ApprovalRequest` objects that the 01 router passed to the TUI surface.
- `APPROVAL_PAGE_LINES = 20`: arguments longer than that show the first 20 lines and `y` and
  `s` are inactive until `v` has paged through the rest (REQ-CLI-26).
- String values are shown through `halkit.termsafe.escape` (REQ-CLI-28).
- Before reading a key: `termios.tcflush(fd, termios.TCIFLUSH)`. Accepted keys: `y`, `s`,
  `n`, `d` (then one line of text), `v`; Enter and Escape mean `n`. `s` is shown only when
  the request's `scopes` include `session`.
- While a prompt is open the live region is paused. Queued approvals are listed together in
  arrival order, each with its own key binding and its own approval key, so three drafts to
  send cost three keystrokes, not three panels, and no answer covers another item.

### Approval surfaces

halcore builds, per entry point, the list of surfaces that 01's router (`halhub.approvals`)
offers each request to; the router owns routing, first-answer-wins, timeouts and audit. Each
surface implements 01's `Approver` protocol and returns `Decision(verdict, scope)`.

| Surface | Used by | `shows_all_args` | Behaviour |
| --- | --- | --- | --- |
| `TuiSurface` | `hal code`, `hal chat` | yes | Schedules the panel on the asyncio loop with `run_coroutine_threadsafe`; scopes `once`, `session` |
| `AcpSurface` (editor) | `hal acp` without `--channel`, or `--channel editor` | yes | Host-originated `session/request_permission` (REQ-CLI-38) |
| `AcpSurface` (telegram) | `hal acp --channel telegram` | yes | Same request; the bridge renders buttons; scope `once` only |
| `AcpSurface` (voice) | `hal acp --channel voice` | no | Same request; hal-voice speaks the summary; scope `once` only |
| `ConsoleSurface` | console | yes | Queues the request for the page; answered through `/api/approvals/<id>` |
| `ControlSurface` | every process that hosts a broker | yes | Serves `runtime_dir()/control/<sid>.sock` for `hal approvals`, `hal approve`, `hal deny` (B3) |
| `NotifySurface` | `hal box` | no | Desktop notification (01 `notify` surface), once only, never for `content` calls |

Per entry point: `hal code` and `hal chat` use TUI and control; `hal acp` uses its ACP
surface and control; the console uses console and control; `hal box` uses control and
notify, because the CLI in the box owns the terminal and in-box clients never get MCP
elicitation (B4); `hal -p` uses no surface (deny, `no-approver`) or, with
`--approvals queue:S`, control only with request timeout S.

A host harness using `hal-hub mcp` gets elicitation or `hal approve` as 04 and 01 define (B4);
`hal connect` and `hal claude` print that those approvals are advisory (REQ-CLI-43).

### Control socket and `hal approvals`

- Path: `runtime_dir()/control/<sid>.sock`, directory 0700, socket 0600, one per broker
  session (B3). It is deliberately not in `runtime_dir()/s/<sid>/`, which is mounted into the
  box at `/run/hal`.
- Peer check and protocol: 01-security.md, "Approval surfaces and protocol" (`control`
  surface). halcore uses the server and client from `halhub.approvals`.
- `hal approvals [list]` connects to every socket in `runtime_dir()/control/` (skipping and
  removing stale ones whose owner pid is gone) and prints the pending requests.
  `hal approvals watch` stays connected and shows each new request in the approval panel
  layout. `hal approve ID` and `hal deny ID` ask every socket which one owns ID, print the full
  call in the panel layout, and for approve ask `Allow this exact call? [y/N]`; `--scope
  session` is accepted only when the request offers it.
- All of them refuse when stdin or stdout is not a terminal or `HAL_HARNESS` is set
  (REQ-CLI-31). `hal claude`, `hal connect` snippets and `hal box` set `HAL_HARNESS=1` in the
  harness environment. Both checks are speed bumps for a harness that runs unsandboxed on the
  host, which can do anything the user can; 01-security lists this as residual risk R-new-4.

### Headless mode (`hal -p`)

- `TASK` is the argument, or stdin when it is `-`. Code mode by default with the launch
  target rules (an unresolvable directory is exit 2); `--chat` uses the assistant box of the
  `headless` channel. `--resume SID` continues a session.
- No TUI. Text output: the final assistant message on stdout, notices on stderr. `--json` and
  `--stream-json` print 03-agent.md 3.15's objects through `halagent.headless.Renderer`;
  halcore feeds it the ACP updates, the `_hal/record`s, and its own approval and checkpoint
  events, whose shape 03 defines.
- `--include-thinking` adds reasoning to the stream (03's reasoning blocks).
- Exit codes (owned here, F1):

| Outcome | Exit |
| --- | --- |
| ACP `end_turn` | 0 |
| ACP `max_tokens` or `max_turn_requests` | 4 |
| ACP `refusal` | 5 |
| ACP `cancelled` or SIGINT/SIGTERM | 130 |
| `--timeout` expired | 124 |
| Agent error whose `data.hal.stopReason` reports an unreachable model, or gateway unreachable at start | 3 |
| Any other agent error (JSON-RPC -32603) | 1 |
| Usage or config error, unknown session id | 2 |

- `--notify` (used by `core_long_task`) sends one `halkit.notify` message with the stop reason
  and the first line of the result when the run ends.
- Headless never calls `on_human_message` (REQ-CLI-94).

### `hal acp` proxy

- Default (`--toolset coding`): code session assembly steps 1 to 6 (and the teardown), then a
  bidirectional pump between the editor's stdio and the box's stdio.
- `--channel NAME --toolset assistant`: the assistant box of that channel, with the channel
  process as the client.
- Client to agent: `initialize` loses `clientCapabilities.fs` and
  `clientCapabilities.terminal` and gains `_meta.hal.records = true` (halcore consumes the
  records and does not forward them to the editor); in project mode, `session/new` and
  `session/load` with a `cwd` outside the project root are answered with error -32602 by the
  proxy, and with `--toolset assistant` the proxy replaces `cwd` with the channel scratch
  directory (REQ-CLI-39);
  `mcpServers` from the client pass only under REQ-CLI-98. Every `session/prompt` first goes
  to `on_human_message` with surface `acp` (REQ-CLI-94).
- Agent to client: `fs/*` and `terminal/*` requests are answered by the proxy with error
  -32601 and never reach the editor (REQ-CLI-37); any `_meta` key starting with `hal/` is
  deleted; tool call titles get the prefix `[box] `; `_hal/record` notifications are
  consumed. Paths are already host paths (03-agent.md 3.14.5).
- Broker approvals are injected by the proxy in 01's ACP mapping. A client answer becomes the
  surface's `Decision`; an answer to an agent-originated request never creates a broker grant
  (REQ-SEC-29).
- The proxy logs JSON-RPC method names and ids (never contents) at DEBUG to stderr, which
  editors capture.

Zed configuration printed by `hal acp --help`:

```json
{"agent_servers": {"Hal": {"command": "hal", "args": ["acp"]}}}
```

### Harness front-ends

`hal box` parses only the options before `--` and calls `halsandbox.box.run` with a
`start_model` callback that creates a gateway session and a `SessionForwarder`; the per-CLI
templates, image names and the relay are 02's. `hal box image build|status` and
`hal box harnesses` delegate likewise.

`hal connect HARNESS` prints, for the harnesses 02 lists (claude, qwen, opencode, codex,
crush, goose) plus cline:

```text
# UNSANDBOXED: <harness> and every tool it runs work on your host as you; Hal's approvals are advisory here. Use `hal box -- <cli>` for the sandboxed version.
# hal connect turned on the gateway's TCP listener on 127.0.0.1:8741.
export OPENAI_BASE_URL=http://127.0.0.1:8741/v1
export OPENAI_API_KEY="$(cat ~/.config/hal/tokens/connect-<harness>)"
export HAL_HARNESS=1
# MCP server entry for <harness>:
{"mcpServers": {"hal": {"command": "hal-hub", "args": ["mcp"]}}}
```

The token is a named client token issued through 05 (D5), kept in
`config_dir()/tokens/connect-<harness>` (0600), and shown as a `$(cat ...)` reference, so the
printed snippet can be pasted into notes without leaking it and the token can be revoked per
harness (`hal gateway token revoke`). `--write` writes the harness's own config file with the
real token after a diff and y/N, backing up the old file as `<file>.hal.bak`. The snippet text
comes from `halkit.harness` (C6), shared with 07.

`hal claude` (legacy, from `hal:123-276`):

1. Print `hal: Claude Code runs unsandboxed on your host; the network block below is advisory
   and Hal's approvals are advisory. Use 'hal box -- claude' for the sandboxed version.` on
   stderr.
2. Ensure the gateway runs (as `hal serve` does, without changing the mode unless
   `--preset`), and turn on its TCP listener with a notice (REQ-CLI-104).
3. Environment: `ANTHROPIC_BASE_URL=http://127.0.0.1:8741` (gateway root, F7),
   `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` = the named client token `claude`;
   `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL` = the
   mode's main alias; `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`,
   `CLAUDE_CODE_SUBAGENT_MODEL` = the mode's agent alias (both from
   `GatewayClient.aliases()` `roles`); `CLAUDE_CODE_MAX_CONTEXT_TOKENS` = the mode's
   `context_window` (replacing the awk over `modes/<mode>.ini`); `API_TIMEOUT_MS` = the
   mode's `client_timeout_ms` (7200000 for the swap modes); `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
   = `[claude].max_output_tokens`.
4. The privacy block of `hal:247-268` verbatim (all `DISABLE_*` and `CLAUDE_CODE_DISABLE_*`
   names), then the proxy backstop `HTTP(S)_PROXY`/`ALL_PROXY` = `http://127.0.0.1:9`,
   `NO_PROXY=localhost,127.0.0.1,::1`, unless `[claude].allow_network`.
5. Compat env for vault tools: `HAL_VAULT`, `HAL_EMBED_MODEL`, plus `[claude.env]` entries
   (for example `HAL_UNIVERSITY`, `HAL_ANSWERER`); `OBSIDIAN_API_KEY` from
   `~/.config/hal/obsidian_api_key` only with `--vault` (the vault `.mcp.json` uses the
   placeholder; an unsandboxed process could read the file anyway, so this changes no
   exposure). `HAL_OLLAMA_URL` is no longer set: Hal components reach models only through
   the gateway (G5).
6. `--permission-mode default` unless the user passed `--permission-mode` or
   `--dangerously-skip-permissions`. The old `acceptEdits` default existed because auto
   mode's model approver timed out on slow prefill (`hal:88-92`); `default` (ask) avoids that
   timeout and no longer lets a local model edit files unasked.
7. `cd` to `--dir`, the vault with `--vault`, else the cwd; `exec claude ARGS`.
8. `--cloud`: steps 2 to 6 are skipped; Claude Code runs with the user's own login and only
   `HAL_HARNESS=1` and the MCP entry are added. The warning still prints.

When `claude` is not on PATH: exit 3 with `Claude Code is optional; install it from its
official instructions, or use 'hal code'.` The installer no longer installs Node or Claude
Code.

### Gateway and models front-ends

`hal serve [PRESET]`:

1. If `hal-gateway.service` is installed: `systemctl --user start hal-gateway.service`
   (timeout 30 s). Otherwise spawn `hal-gateway serve` detached (new session, stdout and stderr
   to `state_dir()/logs/gateway.log`), record its pid file, and wait up to 10 s for
   `runtime_dir()/gateway.sock`.
2. With PRESET: `GatewayClient.set_mode(PRESET, wait_s=LOAD_TIMEOUT_S)` and print progress
   from `status()` per model (`Loading hal-gpu ........ ready`). `LOAD_TIMEOUT_S = 900` (from
   `hal-serve`: loading 50 GB from a cold disk takes a few minutes).
3. `--list` prints modes with their one-line description (as `hal-gateway modes`).

`hal serve` works while Hal is off (it is an explicit user action) and prints `note: Hal is
off; only this server was started`. `hal stop` is `GatewayClient.stop_router()`.

`hal models` delegates to `halgateway.models` and formats the output.
`CONFIRM_DOWNLOAD_BYTES = 1 GiB`: below that a pull is routine (embedding models); above it
the user sees the size first (the max profile is about 160 GB).

### Power and kill

Constants: `CLAIM_WAIT_S = 90` (three gpu-light release deadlines of 30 s, from
`halcore/gpulight.py`), `SYSTEMCTL_TIMEOUT_S = 30`, `OLLAMA_TIMEOUT_S = 10` (unload 30),
`KILL_GRACE_S = 5` (then SIGKILL; matches `hal-kill`'s 10 x 0.5 s loop).

gpu-light: halcore asks `halgateway.gpu` which coordinator is present (05). `gpu-light-v2`:
`hal on` and `hal off` make no claim; the gateway claims per use and gpu-light reclaims idle
router models through the `hal gateway release-gpu` command reclaimer (D7, D8). `gpu-light-v1`:
`hal on` claims `hal` through `halgateway.gpu.lightv1` as `halcore/gpulight.py` does today
and `hal off` releases it. `none`: no claim.

`hal off` (order matters):

1. Write the off flag (and the default-path copy, compat item 2).
2. `systemctl --user stop` every unit that `units.json` records with `gpu: true`.
3. Unload every Ollama model (`/api/generate` with `keep_alive: 0`).
4. `GatewayClient.release_gpu(stop_router=True)`: the gateway unloads its GPU models, stops
   the managed router and releases its claims; while an interactive session holds a lease it
   changes nothing and returns the kept leases (05 REQ-GW-55, D9), which `hal off` prints
   (REQ-CLI-50). With `--force` the call is `release_gpu(force=True, stop_router=True)`.
5. Release the gpu-light v1 claim `hal` last, because until now Hal still held VRAM.
6. Desktop notification unless `--quiet` (`notify-send`, timeout 5 s).

`hal on`: with gpu-light v1, claim `hal` (wait up to `CLAIM_WAIT_S`, with `waiting for
<jobs>` on stderr and as a notification); remove the flag; unload partly offloaded Ollama
models; `systemctl --user start hal.target`.

`hal status --json`:

```json
{"on": false, "gpu_services": {"running": [], "installed": ["hal-voice.service", "hal-tts.service"]},
 "gateway": {"up": true, "tcp": false, "backends": {"ollama": "up", "local": "stopped"},
             "loaded": [{"alias": "hal-chat", "model": "gemma4:12b-it-qat"}]},
 "llama_server": {"mode": null, "pid": null, "leases": []},
 "containers": [{"session": "3f9a1c0b7e2d", "kind": "code", "backend": "docker-runsc", "project": "/home/u/src/x"}],
 "approvals_pending": 0, "gpu_light": "gpu-light v1: green", "checked_at": "2026-10-23T08:15:30Z"}
```

`hal kill` (replaces `hal-kill`):

1. `hal off --quiet --force` steps 1 to 5.
2. `systemctl --user stop hal.target` and every unit named in `units.json` (H4). No
   `hal-*` glob: it would match `hal-employment-app.service` and the transient
   `hal-employment-remote` unit, which hal-employment owns.
3. Stop the gateway (unit or pid file); its llama-server child stops with it.
4. `docker ps -q --filter label=hal.sandbox=1 --filter label=hal.uid=<uid>` (02 labels) and
   `docker stop -t 5`.
5. For each pid file in `runtime_dir()/pids/*.json` (`{"pid", "start_time", "argv0", "kind"}`,
   written by every long-running `hal` process and by `core_long_task` jobs at start and
   removed at exit): signal only when `/proc/<pid>/stat` start time and `/proc/<pid>/cmdline`
   argv0 match the record; SIGTERM, wait `KILL_GRACE_S`, then SIGKILL.
6. Report what is left; exit 1 if anything is.

No pattern matching on `python -m hal...`: today's `pkill -f 'python[0-9.]* -m hal[a-z]+'`
would match any `python -m hal<letters>` process, including tools of other projects; pid
files with start-time checks cannot hit a reused pid or a foreign process.

### Units and desktop launchers

halcore owns unit rendering for every Hal package and pack (F8 to F10). Templates: hal-core
ships `hal.target`, `hal-console.service` and three launchers; hal-gateway ships
`hal-gateway.service` (05, rendered here, not copied); modules declare theirs in
`[[units]]` and `[[desktop]]` (04), found through `halhub.registry.Registry.load()` (no second
discovery scan, replacing `units.module_roots`). gpu-light installs its own unit; hal-core no
longer ships `hal-gpu-light.service`.

Placeholders:

| Placeholder | Value | Reason |
| --- | --- | --- |
| `@ROOT@` | the module's root directory (the manifest's directory) | Existing; data files and `bin/` launchers beside the code; kept for the jobs unit (H2) |
| `@HAL@` | absolute path of the `hal` executable in the Hal venv | Units must not depend on `~/.local/bin` or PATH |
| `@PYTHON@` | the interpreter `packs.json` records for the module | Replaces hard-coded `/usr/bin/python3` and `.venv` paths |
| `@BIN@` | `bin/` directory of that interpreter's env (the Hal venv for hal-core and hal-gateway) | Console scripts of the pack; 05's `hal-gateway.service` uses `@BIN@/hal-gateway` |
| `@HOME@` | home directory | Existing |
| `@STATE@` | default state dir | Off flag and logs |
| `@CORE@`, `@ECOSYSTEM@`, `@GPU_LIGHT@`, `@HAL_GPU_UNITS@` | removed: rendering fails naming module and file | There is no core checkout, ecosystem folder or Hal-shipped light unit any more |

GPU marker: the manifest's `[[units]] gpu = true` is the source (04); the renderer emits
`X-HalGPU=yes` and the off condition. A template that already carries `X-HalGPU=yes` (the
hal-employment unit today, hal-core's console) is treated the same, and the line is not
duplicated (F9). `units.json` records `gpu` per unit, which `hal off` reads.

The jobs pack's `hal-employment.service` keeps `@ROOT@` for its `bin/` launcher; the launcher
reads `[jobs].jobhunt_root` at run time, so no config placeholder is needed (H2). A golden
test renders a fixture copy of that template (REQ-CLI-58).

hal-core template:

```ini
# hal-console.service
[Unit]
Description=Hal test console on http://127.0.0.1:8740
PartOf=hal.target
X-HalGPU=yes

[Service]
ExecStart=@HAL@ console --service
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10

[Install]
WantedBy=hal.target
```

`hal-gateway.service` is not `X-HalGPU`: it uses no GPU itself, holds the auth front door for
every harness, and `hal off` frees the GPU through `release_gpu` instead (Decision D5, 05
"systemd unit"). The `PATH=%h/.npm-global/bin:...` line of today's console unit is dropped:
it existed for `deep_work` running `claude`.

Launchers: `hal-console.desktop` `Exec=@HAL@ console --open`; `hal-toggle.desktop`
`Exec=@HAL@ toggle`; `hal-kill.desktop` `Exec=@HAL@ kill`. Names unchanged.

Record `state_dir()/units.json`, version 2 (version 1, a flat `{unit: root}` map, is read and
upgraded):

```json
{"version": 2, "units": {"hal-clock.service": {"pack": "organiser", "module": "clock",
  "root": "/home/u/.local/share/hal/envs/packs/lib/python3.13/site-packages/halclock",
  "rendered_sha256": "9c1e...", "gpu": false, "installed_at": "2026-10-23T08:15:30Z"}}}
```

Install algorithm: render everything into memory first (fail before any write), write each
changed file atomically, delete stale ones (REQ-CLI-59), `daemon-reload`, `enable` units with
`[Install]` that are not templates and whose manifest entry has `enable = true`, start
`hal.target` only with `--start` and not off.

### Packs

The pack list is 08's (C7): `organiser`, `life`, `comms`, `online`, `desktop`, `system`,
`work`, `jobs` from hal-tools, plus the flagships `memory` and `voice`. Distribution
`hal-<pack>`, directory `packs/<pack>` in hal-tools. Set membership is this spec's.

Catalogue, package data `halcore/data/catalog.toml`, updated with each Hal release; installs
come from git tags resolved to pinned commits (no PyPI in this release):

```toml
schema = 1

[sources.hal-tools]
repo = "https://github.com/NotEoin/hal-tools.git"
tag = "v0.1.0"
commit = "0000000000000000000000000000000000000000"   # filled at release

[sources.hal-memory]
repo = "https://github.com/NotEoin/hal-memory.git"
tag = "v0.1.0"
commit = "0000000000000000000000000000000000000000"

[sources.hal-voice]
repo = "https://github.com/NotEoin/hal-voice.git"
tag = "v0.1.0"
commit = "0000000000000000000000000000000000000000"

[packs.organiser]
source = "hal-tools"
subdirectory = "packs/organiser"
distribution = "hal-organiser"
env = "shared"                     # data_dir()/envs/packs
summary = "Calendar, tasks, clock, notify, briefing and routines"

[packs.desktop]
source = "hal-tools"
subdirectory = "packs/desktop"
distribution = "hal-desktop"
env = "system"                     # own env with --system-site-packages (PyGObject)
summary = "Desktop, screen, media and RGB control; desktop sessions only"

# life, comms, online, system, work and jobs: as organiser, env = "shared"

[packs.memory]
source = "hal-memory"
subdirectory = ""
distribution = "hal-memory"
env = "shared"
summary = "Vault memory: index, search, capture and brief"

[packs.voice]
source = "hal-voice"
subdirectory = ""
distribution = "hal-voice"
env = "own"                        # own env from its lockfile; exec manifest, imports nothing from Hal
summary = "Voice: wake word, speech to text, text to speech"

[sets]
assistant = ["organiser", "online", "memory"]
computer = ["desktop", "system", "work"]
personal = ["life", "comms"]
```

`voice` and `jobs` are in no set: voice downloads several GB of models and needs the GPU, and
jobs needs a hal-employment checkout (08). A pack outside the catalogue (`URL@REF`,
`--local`) declares its env kind in its `pyproject.toml` under `[tool.hal-pack]`:
`env = "shared" | "own"`, `system-site-packages = true|false`, `python = "<path>"` (08 sets
the values per pack); a missing table means `shared`.

Environments (C8):

| Kind | Path | Created with | Used by |
| --- | --- | --- | --- |
| `shared` | `data_dir()/envs/packs` | `uv venv --python python3`, then hal-kit and hal-hub from the installed Hal version (packs may import both, A3) | most packs |
| `own` | `data_dir()/envs/<pack>`; source clone in `data_dir()/packs/<source>` | `git clone`, `uv sync --frozen --project <clone>` with `UV_PROJECT_ENVIRONMENT=<env>` | packs with their own lockfile (`voice`) |
| `system` | `data_dir()/envs/<pack>` | `uv venv --system-site-packages --python /usr/bin/python3` | `desktop` |

Install of a pip-installed pack (`shared` or `system`), in two steps (REQ-CLI-110), because
a commit pin alone pins only the pack's own code: without the first step uv would resolve its
dependencies and build backend from PyPI, unpinned and unhashed:

```text
uv pip install --python <env>/bin/python --require-hashes \
  -r <halcore>/data/requirements/<env kind>.txt        # deps and build backends, hashed
uv pip install --python <env>/bin/python --no-deps --no-build-isolation \
  "<distribution> @ git+<repo>@<commit>#subdirectory=<subdirectory>"
```

`halcore/data/requirements/shared.txt` and `system.txt` ship beside `catalog.toml` and are
regenerated at each Hal release with `uv pip compile --generate-hashes` over the dependencies
and `build-system.requires` of every catalogue pack of that env kind (one file per kind keeps
the shared venv consistent). The git requirement itself cannot carry a hash; the commit check
of REQ-CLI-63 covers it. A `URL@REF` pack is cloned at the commit first: with a `uv.lock`,
`uv export --frozen --format requirements-txt --no-emit-project` gives its hashed file, which
replaces the first step (its build backend must appear in that file or in the catalogue's,
else it is listed as unpinned); without one, the summary lists `dependencies` and
`build-system.requires` from its `pyproject.toml` as `unpinned` before the typed `y`, and the
install runs without `--require-hashes`.

with `timeout = PACK_INSTALL_TIMEOUT_S = 1800` (a cold uv cache builds several packs; the
value bounds a hung network [unverified: measure a full `--set assistant` install]),
`GIT_TERMINAL_PROMPT=0`, and then the commit check of REQ-CLI-63 against
`<site-packages>/<dist>.dist-info/direct_url.json`.

Install of an `own` pack: `git clone --filter=blob:none --no-checkout <repo>
<data_dir>/packs/<source>`, `git -C ... checkout --detach <commit>`, check `HEAD`, then
`uv sync --frozen` into its env (same timeout).

After either install, halcore lists the pack's modules with 04's manifest locator run against
the env (no pack code is imported in the `hal` process) and records each module with its
interpreter in `packs.json`. The hub reads that file to run each module with the recorded
interpreter (C8). An `exec` module (hal-voice) records its `exec` argv's interpreter only for
display; the hub uses the manifest's `exec`.

Catalogue packs install without a prompt for the code itself, because their commits ship with
Hal; `URL@REF` packs need a typed `y` after the summary (REQ-CLI-62). Install steps always need
a typed `y` (REQ-CLI-64).

`URL@REF`: REF must match `^v?[0-9][0-9A-Za-z.+-]*$` (a tag) or `^[0-9a-f]{40}$`. A tag is
resolved with `git ls-remote <url> refs/tags/<ref> refs/tags/<ref>^{}` (timeout 60 s) to a
commit, and both are shown with the pack's manifest summary (modules, tool names with tiers,
network flags, units, install steps) before the typed `y`. Branch names are refused
(REQ-CLI-62).

`--local PATH` (development and the cutover): `uv pip install --python <env>/bin/python -e
PATH` for a `shared` or `system` pack; for an `own` pack it records the path and runs
`uv sync --frozen --project PATH` into its env without cloning. Local packs show as `dev
checkout, unpinned` in `list` and `doctor`.

`HAL_PACKS_MIRROR=<url prefix>` replaces everything up to the last `/` of `repo` (today's
`HAL_GITHUB` behaviour, `ext.py:100-101`). The commit pin is still checked, so a mirror cannot
serve different code.

Install steps (REQ-CLI-64): each module's `[[install]]` steps (04) are shown through
`halhub.install.plan` as

```text
hal-memory wants to run an install step (merge-vault-mcp):
  summary: Adds obsidian to the vault's .mcp.json and removes the old hal server
  argv:    /home/u/.local/share/hal/envs/packs/bin/hal-memory setup
  in:      /home/u/.local/share/hal/envs/packs/lib/python3.13/site-packages/halmem
  network: no
It may change files outside Hal. Type y to run it, anything else to skip: 
```

and run by `halhub.install.run_steps(module, confirm=<typed y on a terminal>)`, which applies
the step's own timeout (04, at most 7200 s) and records the result in
`state_dir()/hub/install.json`. Without a terminal `confirm` returns false, the pack stays
disabled with setup state `pending`, and `hal packs setup NAME` runs the steps later.

State `state_dir()/packs.json` (read by the hub, C8):

```json
{"version": 2, "packs": {"organiser": {"source": "hal-tools", "tag": "v0.1.0",
  "commit": "0123...", "env": "shared", "env_path": "/home/u/.local/share/hal/envs/packs",
  "distribution": "hal-organiser", "path": null, "installed_at": "2026-10-23T08:15:30Z",
  "setup": "none",
  "modules": {"calendar": {"interpreter": ["/home/u/.local/share/hal/envs/packs/bin/python"]},
              "tasks": {"interpreter": ["/home/u/.local/share/hal/envs/packs/bin/python"]}}}}}
```

`setup` is one of `none`, `done`, `pending`, `failed`. Version 1 files (no `modules`) are
upgraded by re-running the manifest locator.

Enable and disable edit `[hub].disabled` with `halkit.config` line editing: the one
`disabled = [...]` line is replaced (or inserted after `[hub]`, or a `[hub]` table appended);
every other byte is kept, as `ext.set_disabled` does today (REQ-CLI-66).

### Policy, audit, guard and secrets commands

- `hal policy show|check|explain|init` follow 01-security.md "Commands"; `explain` prepares
  and decides with `halhub.policy` against a synthetic context (`--session-kind`, `--taint`,
  `--channel`) and prints `explain(decision)` plus the approval key.
- `hal policy edit`: `$VISUAL`/`$EDITOR` (timeout none: the user is typing; the editor runs
  in the foreground on the user's terminal) on a 0600 temporary copy, then `load_policy`,
  then atomic replace with `policy.toml.bak` (REQ-CLI-71).
- `hal policy allow TOOL [--args JSON] [--pack P]`: renders the rule 01's loader accepts
  (a per-tool allow, optionally bound to exact arguments), shows it with the tool's tier and
  flags, and writes it after a typed `y` (REQ-CLI-102). 01's invariants still apply (a
  `system` tool can never be `allow`, and the loader rejects such a rule).
- `hal audit tail|show` read `state_dir()/audit/<UTC date>.jsonl` and print records through
  `halkit.termsafe`.
- `hal guard status|install|bench`: status of 01's guard; `install` builds the guard image
  and downloads its pinned weights after a typed `y` (an install step); `bench` per
  REQ-SEC-38.
- `hal secrets set PACK NAME` reads the value with `getpass` (never from argv) and stores it
  where 01-security "Secrets" says; `list` prints names only; `rm` deletes one.

### Sessions

Session records: `state_dir()/sessions/<sid>/meta.json` written by halcore, and
`transcript.jsonl` beside it (03's record schema, written by halcore from `_hal/record`, E3).

```json
{"version": 1, "id": "3f9a1c0b7e2d", "created_at": "2026-10-23T08:15:30Z",
 "updated_at": "2026-10-23T09:02:11Z", "mode": "code", "channel": "tui",
 "project": "/home/u/src/parser", "isolated": false, "backend": "docker-runsc",
 "model": "hal-code", "policy_profile": "default", "title": "fix the failing parser test",
 "checkpoints": 4, "status": "closed"}
```

`status` is `open`, `closed` or `crashed`. Export `md` writes the user and assistant
messages, tool calls as one line each (full output with `--with-tool-output`), approvals with
their decision (from the audit log, by session), and a header with the meta fields; `jsonl`
writes the records unchanged. Both pass `halkit.redact`. Assistant channel sessions get the
same files with `project` set to the scratch directory.

### Console

- `hal console --service` runs the HTTP server (unit); `hal console` runs it in the foreground;
  `hal console --open` ensures it runs (starts the unit if installed, else a detached server),
  asks it for a one-time link with the token, and opens it with `xdg-open` (timeout 10 s).
- Token: `config_dir()/console-token` (0600), created with `halkit.secrets.generate_token`.
- Routes:

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /open?t=<one-time>` | one-time link | Sets cookie `hal_console=<random session id>; HttpOnly; SameSite=Strict; Path=/`, then 303 to `/` |
| `GET /` | cookie | Page |
| `GET /api/cases` | cookie or bearer | Cases, grades, info |
| `POST /api/open-link` | bearer only | Returns `{"url": "http://127.0.0.1:8740/open?t=..."}` |
| `POST /api/ask`, `/api/voice`, `/api/grade`, `/api/undo`, `/api/reset`, `/api/speak` | cookie or bearer, plus Origin and JSON checks | As today |
| `GET /api/approvals`, `POST /api/approvals/<id>` | cookie or bearer, plus Origin and JSON checks | Pending approvals of the console's assistant, and decisions |

- Check order per request: Host (403), route, auth (401), Origin for POST (403), content type
  for POST (415, 01-security REQ-SEC-71), then the handler. Errors:
  `{"error": {"code": "forbidden", "message": "..."}}`.
- The console's assistant is `open_assistant("console", [ConsoleSurface, ControlSurface])`,
  so it runs in the console channel's assistant box. Undo calls (`calendar_delete`,
  `tasks_delete`, `clock_cancel` from `_remember_undo`) no longer send `_confirmed: True`;
  the page shows them as approval requests and the click is the approval. Each `/api/ask`
  calls `on_human_message` with surface `console`.
- Rendering (REQ-CLI-109): the page builds the DOM with `textContent` and `createElement`
  only; no `innerHTML`, no inline handlers, no inline script (the CSP of REQ-CLI-77 forbids
  them anyway). An approval card shows each argument escaped like the TUI panel; an argument
  longer than the card is collapsed, and approve stays disabled until the user has expanded
  every collapsed argument.
- `grade` timestamps use `halkit.timeutil.iso_z()` (today `datetime.now().isoformat()`).
- `open_link_ttl_s = 60`: long enough for `xdg-open` to start a browser, short enough that a
  link left in shell history is dead.

### Eval

`hal eval` wraps 03's eval library (03-agent.md 3.18, F4); the scenario and task formats, the
coding runner, statistics and report contents are 03's. halcore adds:

- the environment: `halkit.testing.TempHalHome` with a generated policy that denies tiers
  `act`, `send`, `system` and every tool with `network = true`, replacing today's hand-kept
  `UNSAFE` name list (`evaluate.py:36-37`), which drifts as packs are added (REQ-CLI-78);
- the assistant: `open_assistant("eval", [EvalSurface])`, an assistant box with an
  in-process broker owned by halcore (A2). `EvalSurface` records every request and denies
  unless the case says to approve, so `confirm_first` becomes "an approval was requested for
  this tool" (REQ-CLI-79);
- the coding runner: a `BoxRunner` (03's protocol) implemented in halcore over
  `halsandbox.Session` and the headless path with `--approvals deny`, because halsandbox may
  not import halagent;
- output: `state_dir()/eval/<YYYYmmddTHHMMSSZ>-<model slug>/` (REQ-CLI-80); `--keep` keeps
  the temp home for inspection.

### Configure, profiles and migration

`hal configure` on a missing file: detect the profile (`nvidia-smi --query-gpu=memory.total
--format=csv,noheader,nounits`, timeout 10 s; `/proc/meminfo`), propose it, ask (unless
`--yes`), and write the template with `[hal]` keys from 00-overview, the profile's role models,
`[hub]`, `[notify]` with a new private ntfy topic, and `[gateway]` defaults for the profile
(05). Thresholds from `setup.sh` `profile_for_hardware`: VRAM >= 22000 MiB and RAM >= 60 GiB
-> `max`; VRAM >= 22000 -> `24gb`; VRAM >= 11000 -> `12gb`; else `cloud` (the margins exist
because a nominal 24 GB card reports 24564 MiB and the desktop holds about 1 GB).

Profile to settings:

| Profile | `[hal].chat_model` | Code role | Gateway default (05) |
| --- | --- | --- | --- |
| `max` | `gemma4:12b-it-qat` | mode `gpu` on the managed llama-server | llama-server modes, Ollama for chat and embeddings |
| `24gb` | `gemma4:12b-it-qat` | `qwen3.8:27b` through Ollama | Ollama |
| `12gb` | `qwen3.5:4b` | `gemma4:12b-it-qat` through Ollama | Ollama |
| `cloud` | `qwen3.5:2b` | none local; `hal claude --cloud` or a cloud upstream opt-in | Ollama for chat only |

These are today's `pick_profile` values (`setup.sh`). The default coding model is chosen by
the coding eval; until then it is the user's `hal-code` alias (RESOLUTIONS, settled
questions).

Configuration migration (`--migrate`), read-only parse of `KEY="value"` lines:

| `hal.conf` / old key | New key |
| --- | --- |
| `HAL_PROFILE` | `[hal].profile` (informational) |
| `HAL_MODEL`, `HAL_WRITER_MODEL`, `HAL_FAST_MODEL` | `[compat].hal_model`, `writer_model`, `fast_model` (00-overview) |
| `HAL_MODE`, `HAL_LLAMA_SERVER`, `HAL_MODELS_DIR`, `HAL_LLAMA_URL` | `[gateway]` keys (05) |
| `HAL_MAX_OUTPUT_TOKENS`, `HAL_PERMISSION_MODE`, `HAL_ALLOW_NETWORK` | `[claude].max_output_tokens`, `permission_mode`, `allow_network` |
| `HAL_UNIVERSITY`, `HAL_ANSWERER` | `[claude.env]` |
| `HAL_NUM_CTX`, `HAL_BACKEND`, `HAL_OLLAMA_URL` | dropped (gateway modes; `[hal].ollama_url`) |
| `[brain]` | `[agent]` (mapping owned by 03) |
| `[hub].module_paths` naming `hal-ecosystem` folders | removed after the cutover registers the packs |
| no `[jobs]` table | `[jobs].jobhunt_root = "<current effective value>"`, today `~/src/employment-helper` (H1) |
| the path of the migrated `hal.conf` | appended to `[compat].hal_conf` |

The migrator prints a unified diff of `config.toml` and asks y/N, then writes atomically with
mode 0600, keeping `config.toml.bak.<UTC stamp>`. Writing `[jobs].jobhunt_root` is the only
pack-table edit halcore makes; it writes the value the jobs pack would use anyway, so the jobs
pack can drop the old default later without breaking hal-employment.

The `[compat]` writer (00-overview) never rewrites a file it did not create (header check). For
the existing `hal-ecosystem/core/hal/hal.conf`, listed in `[compat].hal_conf`, it only verifies
that the three model lines match `[compat]` and warns otherwise (H3); `hal doctor` `compat`
runs the same check.

### Doctor checks

Each check: id, probe, timeout, fix line. `--only` takes ids.

| Id | Probe | Timeout | Fail or warn |
| --- | --- | --- | --- |
| `python` | interpreter version >= 3.11 | - | fail |
| `versions` | all `hal-*` distributions share one version | - | fail |
| `config` | `halkit.config.load` of every registered table; unknown tables listed | - | fail; unknown tables warn |
| `modes` | config dir 0700, `config.toml`, `policy.toml`, tokens and secret files 0600 | - | warn, `--fix` |
| `policy` | `halhub.policy.load_policy` | 5 s | fail |
| `runtime` | `runtime_dir()` per 00's rule (F6), private, sockets under 107 bytes | - | fail, `--fix` recreates |
| `sandbox` | `halsandbox.doctor.checks()`: docker reachable (group membership noted as root-equivalent), runsc probe (fails with the fix "register runsc with `--host-uds=open`, `scripts/host-setup.sh`" while `daemon.json` lacks it), bwrap probe, image present and current; `[sandbox].require` honoured | 30 s | fail when require and no backend; warn otherwise |
| `gpu-runtime` | `nvidia` listed in docker runtimes (GPU containers, D11 to D14) | 10 s | warn |
| `gateway` | socket present, `GatewayClient.status()`, role aliases resolve (D1) | 5 s | fail |
| `llama` | managed llama-server binary present, `--version` reports the pinned build | 10 s | warn |
| `ollama` | `GET /api/version` on `[hal].ollama_url` | 5 s | warn |
| `models` | `halgateway.models.verify(full=False)`: manifest entries present with matching size (`hal models verify --full` hashes) | 30 s | warn |
| `gpu` | `nvidia-smi` present and answering | 10 s | warn |
| `gpu-light` | coordinator kind from `halgateway.gpu`, its status | 10 s | warn |
| `units` | every unit in `units.json` exists, its `ExecStart` and `WorkingDirectory` paths exist, rendered hash matches | 30 s | fail (catches a broken cutover), `--fix` re-renders |
| `links` | `~/.local/bin/hal`, `hal-hub`, `hal-gateway`, `hal-remote` resolve into the venv | - | warn |
| `compat` | each `[compat].hal_conf` file exists with the three lines matching config; off flag at the default path when `HAL_STATE_DIR` differs | - | warn |
| `jobs` | jobs pack installed and `[jobs]` absent (old default in use, H1) | - | warn: `hal configure --migrate` |
| `vault-mcp` | `<vault>/.mcp.json` servers whose command or script path does not exist (read only, values never printed) | - | warn |
| `packs` | `hal packs doctor` summary | 60 s | fail when any pack fails |
| `console` | token file present and 0600 when the console unit is installed | - | warn |
| `legacy` | `[hal].core` set, or `HAL_HUB`/`HAL_CONF`/`HAL_ROOT` in the environment | - | warn: no longer used |

### Configuration keys owned by this spec

`[ui]`:

| Key | Type | Default | Reason |
| --- | --- | --- | --- |
| `default_mode` | str (`auto`, `code`, `chat`) | `"auto"` | Matches what common AI CLIs do in a project, and falls back to chat where mounting the directory would be wrong |
| `show_thinking` | bool | `false` | The brief keeps reasoning out of visible text |
| `tool_output_lines` | int | `8` | Enough to see a failing test's summary; the rest is one `/tools N` away |
| `diff_max_lines` | int | `80` | About one screen; longer diffs go to `/diff` |
| `markdown_refresh_hz` | int | `8` | Smooth to the eye while keeping rich re-render cost low on long tails |
| `mention_max_bytes` | int | `32768` | About 8k tokens at 4 characters per token, a quarter of a 32k window |
| `unicode` | bool | `false` | ASCII output is predictable in every terminal and copies cleanly into notes |

`[console]`: `port` int `8740` (kept from today; desktop launcher and habits use it),
`open_link_ttl_s` int `60` (see Console).

`[units]`: `desktop` bool `true` (render launchers; false on headless machines),
`desktop_dir` str `""` (empty = `xdg-user-dir DESKTOP`, else `~/Desktop`, as today).

`[claude]`: `max_output_tokens` int `8192` (from `hal:82-87`: the output budget must stay well
below the context window), `permission_mode` str `"default"` (see `hal claude` step 6),
`allow_network` bool `false` (keeps the proxy backstop), `preset` str `""` (empty = gateway
default), `[claude.env]` table of str (extra environment for vault tools; open table).

`[long_task]`:

| Key | Type | Default | Reason |
| --- | --- | --- | --- |
| `queue_timeout_s` | int | `3600` | Matches the old `deep_work` one-hour timeout (`brain.py:203-227`) |
| `max_concurrent` | int | `1` | One local job at a time keeps the GPU for the interactive session |
| `cloud_model` | str | `""` | Gateway alias of a cloud upstream for `where = "cloud"`; empty means no cloud jobs even when `[agent].cloud_jobs` is true |

Constants (not configurable, named in code with their source): `CANCEL_GRACE_S = 5`,
`REQUEST_TIMEOUT_S = 30`, `APPROVAL_PAGE_LINES = 20`, `MENTION_SCAN_MAX = 5000`,
`LOAD_TIMEOUT_S = 900`, `CLAIM_WAIT_S = 90`, `CONFIRM_DOWNLOAD_BYTES = 2**30`,
`PACK_INSTALL_TIMEOUT_S = 1800`, `KILL_GRACE_S = 5`, `ACP_MAX_MESSAGE_BYTES = 8 * 2**20`,
`ACP_MAX_JSON_DEPTH = 64`, `AGENT_STDERR_MAX_BYTES = 10 * 2**20`, `TRANSCRIPT_MAX_BYTES = 256 * 2**20`
(03-agent 3.10.1).

Environment variables read by halcore: `HAL_HARNESS` (set by halcore in harness
environments), `HAL_PACKS_MIRROR`, `HAL_GPU_LIGHT` (as today), `HAL_UNIT_DIR` (tests),
`NO_COLOR`, plus the `halkit` path variables. Legacy `HAL_*` launcher variables
(`HAL_MODEL`, `HAL_MODE` and so on) are ignored with one warning naming the config key.

### File layout on the host

```text
~/.config/hal/          config.toml (0600), policy.toml, console-token, gateway-token (05),
                        tokens/ (named client tokens, 0600), models.toml (05 manifest),
                        model-profiles.toml (03)
~/.local/state/hal/     off, units.json, packs.json, sessions/<sid>/{meta.json, transcript.jsonl},
                        assistant/<channel>/work/, jobs/<sid>.json, audit/ (01), hub/ (04),
                        unboxed-ok.json, workspace-ok.json, eval/, console-results.jsonl, input-history,
                        logs/, migrate/<UTC>/
~/.local/share/hal/     venv/ (Hal packages), envs/packs/, envs/<pack>/, packs/<source>/
                        (own-env clones), removed/, llama.cpp/ (05)
$XDG_RUNTIME_DIR/hal/   gateway.sock (05), s/<sid>/ (02; s/<sid>/run/ is /run/hal in the box),
                        control/<sid>.sock, pids/<pid>.json
~/.local/bin/           hal, hal-hub, hal-gateway, hal-remote -> ~/.local/share/hal/venv/bin/...
```

### Installer: `scripts/install.sh`

```text
scripts/install.sh [--profile auto|max|24gb|12gb|cloud] [--set SET | --packs a,b | --all | --core-only]
                   [--editable] [--models | --no-models] [--no-units] [--start]
                   [--replace-links] [--yes] [--dry-run]
```

Steps; each checks state first and is skipped when already done:

1. Checks: bash, git, `python3` >= 3.11, `uv` >= 0.5 [unverified: lowest uv with every flag
   used; this machine has 0.11.19]. Missing uv: print the distro and pipx options and the
   official documentation link, exit 3. No download is ever piped to a shell.
2. Venv: `uv venv --python python3 <data>/venv` if missing.
3. Hal packages from the checkout the script lives in: `uv pip install --python
   <venv>/bin/python ./packages/hal-kit ./packages/hal-hub ...` (with `-e` for `--editable`,
   used for development and this machine's cutover).
4. Links in `~/.local/bin` for `hal`, `hal-hub`, `hal-gateway`, `hal-remote` (07 names the last
   two by absolute path in forced commands); an existing link elsewhere is reported and kept
   unless `--replace-links` (REQ-CLI-90).
5. `hal configure --profile P` (writes only a missing file; `--yes` forwarded only if given).
6. `hal box image build` when docker or bwrap is usable (asks unless `--yes`).
7. Models: `hal models pull --profile P` when `--models`, asked when neither flag is given.
8. Packs: `hal packs add --set assistant` by default (`--packs`, `--all`, `--core-only`).
   Install steps always ask for a typed `y`; `--yes` never answers them, and without a
   terminal they stay pending and are listed at the end (REQ-CLI-91).
9. Units: `hal units install` unless `--no-units`; `--start` adds `--start`.
10. `hal doctor`.

`--dry-run` wraps every mutating command in `run()`, which prints `would run: <argv>`; the
probes in step 1 still run. A full log goes to `<state>/logs/install-<UTC>.log`.

`scripts/host-setup.sh` (sudo, from `~/src/hal-host-setup.sh` and
`system/ollama-store.sh`):

- `--dry-run` prints every step.
- Default: checks that the NVIDIA Container Toolkit and gVisor `runsc`/`runsc-gpu` are
  installed (they are on this machine) and installs them from their signed apt repositories
  only when missing; ensures `/etc/docker/daemon.json` registers `runsc` with
  `"runtimeArgs": ["--host-uds=open"]` (merging the JSON, keeping a backup), because runsc
  refuses the bind-mounted broker and model sockets without it (G1); restarts Docker only
  when no containers run, else prints the restart command.
- `--ssh`: content owned by 07-remote "Host setup"; on this machine openssh-server is already
  active and key-only, so the step verifies and prints 07's recommendations.
- `--ollama-store DIR`: the fstab, store move and service override of `ollama-store.sh`,
  without its `curl https://ollama.com/install.sh | sh` upgrade, which becomes a printed
  instruction.

### Cutover of this machine

Run by the user, after all repos are released and tests are green. Everything is journaled by
`hal migrate ecosystem`; `hal-ecosystem/` itself is never modified, so the old runtime can be
restored by undoing links, units and config.

`hal migrate ecosystem --from ~/src/hal-ecosystem` plan (shown in full with
`--dry-run`; each step asks unless `--yes`):

1. Journal and backups: `state_dir()/migrate/<UTC>/journal.json` and `backup/` with copies of
   `config.toml`, `units.json`, every `~/.config/systemd/user/hal*` and
   `hal.target.wants/` link, the three Desktop launchers, and the old targets of the
   `~/.local/bin` links it will change.
2. `hal configure --migrate --from <from>/core/hal/hal.conf`: adds that path to
   `[compat].hal_conf` (the file is only verified, never rewritten, H3) and writes
   `[jobs].jobhunt_root` with today's effective value (H1).
3. Register packs as local checkouts: `hal packs add --local
   ~/src/hal-tools/packs/<pack>` for each pack whose modules the
   old `units.json` or `[hub]` listed, `--local .../hal-memory`, `--local .../hal-voice`.
   Install steps are not run (`--no-setup`): the existing stores and venvs are already in
   place.
4. Remove the two `hal-ecosystem` entries from `[hub].module_paths`; set nothing else in
   `[hub]`.
5. `hal units install` (no start; Hal is off today): re-renders the 20 units with the new
   roots, writes `hal-gateway.service`, removes nothing that Hal did not install. The journal
   holds the old files.
6. Desktop launchers re-rendered: `hal-console`, `hal-toggle`, `hal-kill`. `hal-employment.desktop`
   comes from the jobs pack and keeps its name.
7. Links: `~/.local/bin/hal`, `hal-hub`, `hal-gateway`, `hal-remote` -> the venv; `hal-voice` ->
   the voice pack's env launcher; `hal-employment` -> the jobs pack's launcher.
   `hal-private`, `batchjob`, `hal-old` and `a-bot` are not touched (the last two dangle; see
   open question 3).
8. Print the vault step, never performed by the migrator: the vault `.mcp.json` `hal` server
   points at `Hal/hal-mcp.py`, which disappears when that checkout moves to the
   monorepo. The fix is hal-memory's install step (it removes the `hal` entry and keeps
   `.mcp.json.bak`), run by the user with `hal packs setup memory`, which shows the step and
   needs a typed `y`. **This edit needs the user's explicit approval**; the migrator only
   reports it.

Checklist (in `docs/howto/move-from-hal-ecosystem.md`):

| # | Step | Check |
| --- | --- | --- |
| 1 | Confirm the hal-employment session is idle and no `hal` from the old tree is running | `hal-ecosystem/core/hal/hal status`; `pgrep -af halcore` shows nothing |
| 2 | Install the new Hal into the venv, editable, no units, no links | `scripts/install.sh --editable --no-units --core-only` then `~/.local/share/hal/venv/bin/hal doctor --only python,versions,config` |
| 3 | Register runsc for sockets (sudo, once) | `scripts/host-setup.sh --dry-run`, then without it; `hal doctor --only sandbox` shows runsc ok |
| 4 | Dry run | `~/.local/share/hal/venv/bin/hal migrate ecosystem --dry-run` lists every change |
| 5 | Migrate | `hal migrate ecosystem` (prints the journal path) |
| 6 | Verify units | `systemctl --user cat hal-voice.service hal-employment.service` show new paths, `X-HalGPU=yes` and the off condition |
| 7 | Verify the jobs contract | `grep -A2 '^\[jobs\]' ~/.config/hal/config.toml` shows `jobhunt_root`; `hal doctor --only compat,jobs` has no warning |
| 8 | Doctor | `hal doctor` has no `fail` |
| 9 | Turn on | `hal on`; `hal status` shows services running |
| 10 | hal-employment | `curl -fsS http://127.0.0.1:8737/healthz`; `hal-hub call jobs_queue` returns the queue |
| 11 | Voice and Telegram | say the wake word; send a Telegram message; both answer, and an `ask` tool raises a host-originated approval (spoken summary or buttons) |
| 12 | Vault MCP (needs approval) | `hal packs setup memory`, type `y`; `.mcp.json.bak` exists |
| 13 | Undo if anything fails | `hal off`; `hal migrate undo <journal>`; `systemctl --user daemon-reload`; old `hal status` works |

Undo restores links, unit files, `units.json`, Desktop launchers and `config.toml` from the
journal, runs `daemon-reload`, and re-enables the units that were enabled before. It does not
uninstall the venv or the pack envs (harmless when nothing links to them).

## Failure modes

| Layer | Failure | What happens | Stance |
| --- | --- | --- | --- |
| Config | Malformed `config.toml`, unknown key in a halcore table | Exit 2 naming file, line, table, key | Closed |
| Config | No `config.toml` | Exit 3: run `scripts/install.sh` or `hal configure` | Closed |
| Policy | `policy.toml` malformed | Session starts with no host tools; banner and status line say why; `hal policy edit` suggested | Closed for host tools |
| Launch | Forbidden root | `hal code` exits 2; plain `hal` falls back to chat with the reason | Closed |
| Launch | Root contains `[hal].vault` or a `[paths].deny` path, no recorded confirmation | Typed confirmation on a terminal; without one, exit 2 (REQ-CLI-09) | Closed |
| Launch | Root contains a registered module's source or an editable `hal-*` checkout, not `--isolated`, no recorded confirmation | Typed confirmation on a terminal; without one, exit 2 naming the roots (REQ-CLI-111) | Closed |
| Gateway | Session over its host-side model budget | Forwarder answers 429 `budget_exceeded`; status line shows `budget wait <n>s` (REQ-CLI-22, 05 REQ-GW-69) | Closed |
| Gateway | Not running | `hal code`, `hal chat`, `hal -p` start it (unit or detached); if that fails, exit 3 | Closed |
| Gateway | Down mid-turn | Agent gets errors (03); TUI shows a notice; the session stays open | Closed |
| Sandbox | runsc absent or failing its probe (for example registered without `--host-uds=open`) | runc with `(no runsc)` in the status line; `hal doctor` names the fix | Degraded, documented |
| Sandbox | Docker seen by the cached probe, now down | Refuse to start, exit 3 (REQ-CLI-97) | Closed |
| Sandbox | Backend `none` (no docker, no bwrap) | Code: ask once per project on a terminal, then banner and `UNSANDBOXED`, no `run` (02); assistant: unboxed fallback after a recorded consent; no terminal and no consent: exit 3; `[sandbox].require = true`: exit 3 | Closed without consent |
| Sandbox | Backend `none` with user MCP servers not marked `host` | Not started; notice names them (REQ-CLI-98) | Closed |
| Sandbox | Image missing | Interactive: offer `hal box image build`; headless: exit 3 | Closed |
| Sandbox | Box exits mid-turn | Session marked `crashed`; next prompt restarts the box and loads the session | Closed |
| Checkpoint | Fails before a turn | Turn not started; message suggests `--no-checkpoint` | Closed |
| Checkpoint | Ignored or oversize files changed | `/undo` lists them as not restorable (REQ-CLI-105) | Open on recoverability, shown |
| Approvals | TUI approval open when the turn is cancelled | Denied, source `cancel` | Closed |
| Approvals | Headless without `--approvals queue` | Deny, `no-approver` | Closed |
| Approvals | Request times out (01 `approval.timeout_s`, or the queue timeout) | Deny | Closed |
| Approvals | Answer key differs from the request | halcore refuses to send it; the router refuses too and audits `approval_mismatch` (01) | Closed |
| Approvals | `content` call with only a voice or notify surface | Not offered there; waits for TUI, Telegram, console or `hal approve`, else times out to deny | Closed |
| Approvals | Control socket from another uid | Connection closed | Closed |
| Approvals | `hal approve` without a terminal or with `HAL_HARNESS` | Refused, exit 2 | Closed (speed bump, R-new-4) |
| ACP | Agent sends invalid JSON or exits | `AgentCrashed`; session resumable | Closed |
| ACP | Agent line over 8 MiB, JSON deeper than 64, or stderr flood | `AgentCrashed` with the reason; stderr log rotates at 10 MiB, 2 files (REQ-CLI-108) | Closed |
| ACP | Agent asks for `fs/*` or `terminal/*` | -32601, logged | Closed |
| ACP | Agent sends a `checkpoint` record or an invalid record | Dropped, notice | Closed |
| TUI | Escape sequences in model or tool text | Stripped | Closed |
| Console | Missing token, bad Host, bad Origin | 401 or 403 | Closed |
| Console | Non-JSON POST | 415 | Closed |
| Power | gpu-light missing, timing out, failing | Warning; on/off continue | Open (scheduling is not a security boundary) |
| Power | `systemctl` missing or failing | Flag still written; exit 1 naming the failing units | Open for the flag |
| Power | Ollama unreachable during off | Warning; other steps continue | Open |
| Kill | Pid file stale or pid reused | Skipped (start time differs) | Closed |
| Units | Unknown or removed placeholder | Nothing written, exit 1 naming module and file | Closed |
| Packs | Commit mismatch | Install rolled back | Closed |
| Packs | Dependency hash mismatch | `uv` aborts; install rolled back, exit 1 (REQ-CLI-110) | Closed |
| Packs | Install step declined or no terminal | Pack installed, disabled, `setup = "pending"` | Closed |
| Packs | Install step failed or timed out | Pack disabled, `setup = "failed"`, retry with `hal packs setup` | Closed |
| Packs | Network down during add | Exit 1, nothing changed | Closed |
| Long jobs | `where = "cloud"` with cloud jobs off | Tool error `cloud_jobs_off` | Closed |
| Compat | `hal.conf` lines differ from `[compat]` | Warning from configure and doctor; the file is not rewritten | Open for Hal |
| Sessions | Transcript or meta write fails | Notice in the UI; session continues | Open (no security role; the audit log is the broker's and fails closed) |
| Migrate | A step fails | Stop; print `hal migrate undo <journal>`; earlier steps stay journaled | Closed |
| Installer | uv missing, Python too old | Exit 3 with instructions | Closed |

## Migration

What replaces what (paths in `Hal.next` at `6ba8a94` unless noted):

| Today | Becomes |
| --- | --- |
| `hal` (bash, 276 lines) | `halcore.cli`; `hal here` -> `hal claude`; `hal mode` -> `hal claude --preset`; `hal model` -> `hal claude --model --vault`; plain `hal` -> TUI (was Claude Code in the vault) |
| `hal:60-64` sourcing `hal.conf` | removed; `hal configure --migrate` parses it once |
| `hal:179-192`, `hal:123-178` env wiring | `hal claude` against the gateway root URL; `GatewayClient.aliases()` replaces the awk over `modes/*.ini` |
| `hal:88-92, 270-275` `acceptEdits` default | `--permission-mode default` |
| `hal:247-268` privacy block | kept verbatim in `hal claude`, documented as privacy, not security |
| `hal:207-210` `OBSIDIAN_API_KEY` export | `hal claude --vault` only |
| `hal:225-238` gpu-light session claim | the gateway's lease and claim (05) |
| `hal-kill` (56 lines, untracked until `6ba8a94`) | `hal kill` with pid files and `units.json` |
| `setup.sh` (417 lines) | `scripts/install.sh`, `scripts/host-setup.sh`, `hal configure` profiles, `hal models pull`; Node, npm and Claude Code installs removed; `~/.bashrc` editing removed (install.sh prints the PATH line if `~/.local/bin` is missing from PATH) |
| `halcore/__main__.py` | `halcore/cli.py` |
| `halcore/chat.py` | `hal chat` TUI and `hal chat MESSAGE` (one-shot), on the chat channel's assistant box |
| `halcore/brain.py` | `halagent` (03); no compatibility shim: hal-voice and the Telegram bridge are ACP clients of `hal acp` in the same release (A1) |
| `halcore/brain.py` `deep_work` | `core_long_task` in hal-core's built-in `core` module (F2) |
| `halcore/power.py` | `halcore/power.py` (on, off, toggle, status, kill) using `GatewayClient` and `halgateway.gpu` |
| `halcore/units.py` | `halcore/units.py` with hub discovery, manifest `gpu` flags, new placeholders, `units.json` v2 |
| `halcore/ext.py`, `extensions.toml` | `halcore/packs.py`, `halcore/data/catalog.toml`, `state_dir()/packs.json` |
| `halcore/hub.py` | removed (00-overview) |
| `halcore/configure.py` | `halcore/configure.py` (profiles, `--migrate`, `--fix-modes`) |
| `halcore/console.py`, `console.html` | same files with auth, Origin, content type, approvals |
| `halcore/evaluate.py` | `halcore/evaluate.py` over `halagent.eval` (03) |
| `halcore/gpulight.py` | `halgateway.gpu.lightv1` (05); halcore calls it only with gpu-light v1 |
| `systemd/hal.target`, `hal-console.service` | `halcore/data/systemd/`; `hal-gateway.service` comes from hal-gateway (05) |
| `systemd/hal-gpu-light.service` | not shipped; gpu-light installs its own unit |
| `desktop/*.desktop` | `halcore/data/desktop/` with `@HAL@` |
| `hal-ecosystem/install.sh` | `scripts/install.sh` (the old file stays in the old repo, unused) |
| `hal-ecosystem/publish.sh` | not ported; publishing is the reviewed release in 00-overview (it would push `hal-private` to `NotEoin/Hal`) |
| `models/build-roles.sh` | not ported; the Ollama models `hal-coder`, `hal-writer`, `hal-fast` it created stay in Ollama and keep serving hal-employment |

How existing users and config keep working:

- `~/.config/hal/config.toml` keeps its path, `[hal]` keys and pack tables; `[brain]` is read
  with a warning until `--migrate` (03 mapping). halcore edits no pack table except writing
  `[jobs].jobhunt_root` once during `--migrate` when `[jobs]` is absent (H1).
- The off flag, `X-HalGPU`, the off condition in GPU units, unit names and Desktop launcher
  names are unchanged (compatibility contract 2, 4, 5).
- `hal.conf` lines for hal-employment: the existing file at
  `hal-ecosystem/core/hal/hal.conf` is left in place, listed in `[compat].hal_conf` and only
  verified (H3), so hal-employment reads the same values before and after the cutover
  whatever `addon.hal_root` points at today (open question 1).
- `hal kill` touches only units Hal recorded, so hal-employment's own units survive (H4).
- Old command names work until 1.0 (REQ-CLI-03).
- hal-voice and the Telegram bridge: the released versions use `hal acp --channel voice` and
  `--channel telegram` with `--toolset assistant` (E1, B9); `[hal].core` is no longer
  needed and `hal doctor` warns if it is set.

What is removed: `hal.conf` sourcing; `HAL_ROOT`, `HAL_HUB`, `HAL_CONF` lookups; the
`hal-ecosystem` sibling layout assumptions (`config.py:63-64`, `hal:38`); `@CORE@`,
`@ECOSYSTEM@`, `@GPU_LIGHT@`, `@HAL_GPU_UNITS@`; `pkill -f` matching and `hal-*` unit globs;
unpinned clones and unconfirmed install steps; `--yes` on approval and install-step paths;
the console's unauthenticated API; `deep_work` cloud jobs on every turn; the in-process
`Brain` used by voice and Telegram.

## Test plan

Hermetic (default run), in `packages/hal-core/tests/`, one file per module:

- `test_cli.py`, `test_cli_aliases.py`: parser for every command, exit codes, aliases
  (`test_old_name_prints_one_line_and_runs_new_command`), vault passthrough, `hal gateway`
  passthrough.
- `test_launch.py`: one test per resolution row, forbidden roots (every 01 hard-deny
  directory, `~/.config`, `~/.local/share`, `~/.local/state`, a root containing `~/.aws`),
  typed confirmation for a root containing `[hal].vault` or a `[paths].deny` path, symlinked
  cwd into `$HOME`.
- `test_render.py`: sanitizer (OSC 52, CSI clear, DCS, lone ESC, C1, bidi, zero-width),
  markup literal, `MarkdownStream` block closing (fences, lists, indented code), tool call
  and diff rendering with `Console(record=True, width=100)` snapshots.
- `test_input.py`: prompt_toolkit `create_pipe_input` + `DummyOutput`: Enter, Alt-Enter,
  bracketed paste, completion of `/` and `@`.
- `test_slash_commands.py`: each command against a `FakeSandboxSession` and a scripted ACP
  agent; `/model` restarts and reloads; `/undo` lists unrestorable files.
- `test_surfaces.py`, `test_approval_view.py`: key carried, page gating, tcflush called
  (patched `termios`), escaping, scopes `once` and `session` only, voice and telegram once
  only, `content` requests never offered to voice or notify, queued list.
- `test_approval_vectors.py`: reads the shared `tests/data/approval_vectors.json` (B1) and
  checks the displayed key prefix and the key sent by `hal approve`.
- `test_control.py`: socket path, peer uid check (patched `getsockopt`), list, approve, deny,
  key mismatch, expired, stale socket removal, no files written.
- `test_acp_client.py`: request/response matching, notifications, records, cancel, invalid
  JSON, EOF, request timeout, oversize line and deep nesting end the session, stderr log
  rotation (REQ-CLI-108).
- `test_acp_proxy.py`: capability stripping, `fs/*` rejection, `_meta` scrubbing, title
  prefix, cwd rejection in project mode and cwd replacement with `--toolset assistant`, injected broker request ids `hal-broker-<n>`, `on_human_message`
  before each prompt, MCP server filtering with backend `none`.
- `test_records.py`: schema validation against 03's vectors, `checkpoint` refused, cap.
- `test_settings.py`: `AgentSettings` rendered with every key, run files, no token value.
- `test_sessions.py`: code assembly order (fakes), assistant box kind `chat` with scratch
  `/work`, box reuse, unboxed fallback consent and broker-only tools, docker-seen-then-down
  refusal, `interactive` passed to `Session.start` for backend `none` (02 owns the consent).
- `test_headless.py`: stop reason to exit code table, `--json` and `--stream-json` validated
  against 03's schema fixtures, timeout 124, approvals deny and queue, no
  `on_human_message`.
- `test_core_module.py`: manifest validates with 04's loader, tier `act`, cloud refused by
  default, job spawn argv, pid file written.
- `test_harness.py`: `hal claude` env (golden list of the privacy names from `hal:247-268`,
  root base URL, named token), permission mode injection, `--cloud`; `hal connect` first
  line, TCP notice, token reference and `--write` diff.
- `test_gateway_cmds.py`: serve, stop, models confirm, against a `FakeGatewayClient`.
- `test_power.py`: on and off ordering (fake systemctl runner, fake Ollama HTTP server,
  coordinator kinds v1, v2 and none), lease kept, `--force`, status JSON.
- `test_kill.py`: pid files with matching and mismatching start times, `jobhunt` process
  never signalled, only `units.json` units stopped, dry run.
- `test_units.py`: placeholders, removed placeholders, manifest `gpu` and template marker,
  names, stale removal, foreign units untouched, `units.json` v1 upgrade, desktop render,
  golden render of a fixture `hal-employment.service`, 05's gateway template rendered.
- `test_packs.py`: catalogue parsing, ref rules, idempotent add, env kinds and paths,
  `packs.json` modules and interpreters, `--require-hashes` then `--no-deps
  --no-build-isolation` argv, `URL@REF` without `uv.lock` lists unpinned dependencies, install steps need typed `y` and stay pending
  without a terminal, disable line edit preserves bytes, remove, doctor.
- `test_cli_policy.py` (explain never runs a tool, edit validation, allow confirmation and
  harness refusal), `test_cli_audit.py` (tail and show escape control characters, secrets
  read with getpass), `test_sessions_cmd.py`, `test_console.py` (Host, Origin, 415, cookie,
  one-time link, CSP directives, untrusted text rendered as text, approve disabled until all
  arguments shown; real `ThreadingHTTPServer` on port 0), `test_evaluate.py`
  (generated policy, approval-based `confirm_first`, report dir), `test_configure.py`
  (thresholds, never overwrite, migrate fixture, explicit `jobhunt_root`), `test_doctor.py`
  (fake probes, timeouts), `test_migrate.py` (synthetic ecosystem tree in `TempHalHome`,
  journal and byte-for-byte undo, `hal.conf` untouched).

Integration (hermetic, default run): `test_flow_headless_fake.py` runs `hal -p` with
`FakeAgent`, `FakeModelServer` and `FakeBroker`; `test_packs_git.py` creates local bare repos
with tags (`git init --bare`, no network) and installs from `file://` URLs with a fake `uv`
runner, plus a local package index fixture whose tampered file fails `--require-hashes`; `test_assistant_policy.py` sends a tool call through the session broker with a policy
fixture; `test_human_message_hook.py` checks that a TUI, ACP and console prompt each reach
`on_human_message` before the agent sees them.

Marked:

- `docker`: `test_tui_code_session_docker.py` (scripted model edits a file, `/undo` restores,
  `/diff` shows it), `test_acp_proxy_docker.py`, `test_assistant_box_docker.py` (scratch
  `/work`, no vault mount, box reused across two sessions), `test_coding_suite_runs_check_in_box`,
  and the shared e2e flows in 00-overview; each docker test also runs with
  `--runtime runsc` when `runsc` is registered with `--host-uds=open` (architect section 2).
- `bwrap`: `test_assistant_box_bwrap.py`.
- `slow`: `tests/e2e/test_install.py`, `test_install_keeps_foreign_links`,
  `test_install_yes_never_answers_pack_steps`.
- `network`: `test_packs_add_from_github.py` (real tag resolution of the released hal-tools).
- `live`: `hal eval` smoke on one assistant case with the real chat model.
- manual: cutover checklist; hal-employment add-on check; voice and Telegram approvals;
  `scripts/host-setup.sh --dry-run`.

Fakes: from `halkit.testing` (`FakeModelServer`, `FakeBroker`, `TempHalHome`, `FakeClock`),
plus halcore-local `FakeAgent` (scripted ACP agent over `PipeTransport`, also emits
`_hal/record`s), `FakeSandboxSession` (records starts, checkpoints, undo, close),
`FakeGatewayClient`, `FakeSystemctl` (records argv, returns scripted states), `FakeOllama`
(HTTP on port 0: `/api/ps`, `/api/generate`), a fake gpu-light coordinator, and a fake `uv`
runner that installs from a local directory.

## Implementation plan

Prerequisites: 00-overview phase 0 (including "Move halcore into packages/hal-core"), and the
parts of 01 to 05 that each step names. Each commit below is one sitting, keeps every test
green, updates `docs/user/cli.md` (and a how-to where named) for the behaviour it adds, and
uses the `<Verb> <object>` subject style (60 characters or fewer).

1. `Port power commands to halkit paths and proc` - on, off, toggle, status on `halkit`; off
   flag at the default path too; tests.
2. `Add pid files for long-running hal processes` - `pids.py`; tests.
3. `Add hal kill with pid files and recorded units` - replaces `hal-kill`; desktop file
   updated; tests.
4. `Remove hal-kill script` - after the launcher calls `hal kill`.
5. `Port unit rendering to hub discovery` - placeholders, manifest `gpu` flags,
   removed-placeholder failure, `units.json` v2, jobs unit golden test; tests.
6. `Render gateway unit and @HAL@ launchers` - 05's template, launchers; tests.
7. `Add argparse command tree for hal` - `cli.py` with every command stubbed to its module,
   exit code table, aliases, `hal gateway` passthrough; tests.
8. `Add launch target resolution` - `launch.py`; tests.
9. `Add ACP client with subprocess and pipe transports` - with the line, depth and stderr
   caps of REQ-CLI-108; tests with `FakeAgent`.
10. `Add transcript records from the agent` - `acp/records.py`, `meta.json`; tests.
11. `Add approval surfaces over halhub approvals` - needs 01 `halhub.approvals`; tests and
    the shared vectors file.
12. `Add control socket and approval commands` - `hal approvals`, `hal approve`,
    `hal deny`; tests.
13. `Add agent settings and run files` - `settings.py`; needs 03 `AgentSettings`; tests.
14. `Add code session assembly over halsandbox` - needs 02; forwarder, control socket,
    fallback rules; tests with `FakeSandboxSession`.
15. `Add assistant box sessions per channel` - needs 02 kind `chat` and 03 `--toolset`;
    tests.
16. `Add unboxed assistant fallback with consent` - needs 03 `halagent.acp.serve`; tests.
17. `Add human message hook to every prompt` - needs 01 `on_human_message`; tests.
18. `Add headless mode with agent output schema` - `hal -p`, exit codes, `--notify`; needs
    03 `Renderer`; tests.
19. `Add built-in core module with long tasks` - `core_module/`, `[long_task]`; tests.
20. `Add TUI renderer with terminal escape sanitizer`; tests.
21. `Add TUI input with mentions and multiline keys`; tests.
22. `Add TUI approval panel bound to approval keys`; tests.
23. `Add TUI turn loop with cancel and status line`; tests.
24. `Add slash commands for the TUI` - all but undo, diff, apply; tests.
25. `Add undo, diff and apply slash commands` - with unrestorable-file report; tests.
26. `Add mask notice and dependency prep offer` - needs 02; tests.
27. `Add hal sessions list, resume and export`; tests.
28. `Add hal acp proxy for editors and channels`; tests; how-to `use-hal-in-zed.md`.
29. `Add hal box and hal connect front-ends` - needs 02 and 05 named tokens; tests.
30. `Port Claude Code launcher to hal claude`; tests.
31. `Add hal serve and hal stop over the gateway` - needs 05; tests.
32. `Add hal models list, pull and verify` - needs 05; tests.
33. `Add hal policy commands` - show, check, explain, init, edit, allow; needs 01; tests.
34. `Add hal audit, guard and secrets commands` - needs 01; tests.
35. `Add pinned pack catalogue` - `catalog.toml` with 08's packs and placeholder commits,
    hashed `requirements/<env kind>.txt` and the release script that compiles them; tests.
36. `Add pack environments and packs.json` - shared, own and system envs; tests.
37. `Add hal packs list, add and remove` - pinned and local installs, mirror; tests.
38. `Add confirmed install steps for packs` - over `halhub.install`; tests.
39. `Add hal packs enable, disable, update and doctor`; tests.
40. `Remove hal ext and extensions.toml` - alias kept; tests.
41. `Port console to token, Origin and JSON checks` - with the CSP and text-only rendering
    of REQ-CLI-77 and REQ-CLI-109; tests.
42. `Add console approvals and one-time links`; tests.
43. `Port eval to the agent eval library` - needs 03 `halagent.eval`; tests.
44. `Add coding suite box runner to hal eval`; docker test.
45. `Port setup profiles to hal configure`; tests.
46. `Add hal configure migrate for hal.conf` - including `[jobs].jobhunt_root`; tests (the
    `[compat]` writer is 00-overview's).
47. `Add hal doctor checks`; tests.
48. `Add hal migrate with journal and undo`; tests; how-to `move-from-hal-ecosystem.md`.
49. `Add whole-ecosystem install script` - `scripts/install.sh`; tests.
50. `Port host setup script into scripts` - `scripts/host-setup.sh` with the runsc
    registration; static test.
51. `Remove bash launcher and setup.sh` - `hal`, `setup.sh`, `SETUP.md` content moved to docs;
    `hal-serve` and `hal-route.py` are removed by 05.
52. `Add user guide for the hal command` - `docs/user/cli.md` complete, `docs/user/tui.md`,
    `docs/howto/approve-a-request-from-another-terminal.md`; docs index updated.

The cutover (checklist above) is not a commit; the user runs it.

## Decisions

- **D1 Assistant channels run in a long-lived assistant box; in-process is only the
  fallback.** Settled by RESOLUTIONS A1. The box contains bugs in the agent's own parsing of
  model output, gives the assistant the same process model as `hal code`, and keeps the vault
  out of reach (scratch `/work`). The in-process design of the first draft survives as the
  backend-`none` fallback (REQ-CLI-96), with a banner and a recorded consent.
- **D2 Plain `hal` opens the TUI: code in a project, chat elsewhere.** Mounting `$HOME` or a
  random directory read-write into the box would break the sandbox's "nothing else from the
  host" rule, so auto mode only picks code where a project is evident. The old behaviour
  (Claude Code in the vault) stays one flag away as `hal claude --vault`.
- **D3 Approval scopes are `once` and `session`; never "always".** Settled by RESOLUTIONS B2.
  `session` binds exact arguments for one session id, so repeated identical calls in a
  session cost one prompt; anything durable is a rule the user writes with `hal policy allow`
  after a typed confirmation, where it is visible and reviewable.
- **D4 The control socket lives outside the session directory.** `runtime_dir()/s/<sid>/` is
  mounted at `/run/hal` in the box; anything there is reachable by the model. One socket per
  broker session at `runtime_dir()/control/<sid>.sock` (B3).
- **D5 The gateway unit is not `X-HalGPU`; `hal off` releases the GPU through the gateway.**
  The gateway holds no VRAM itself and must stay up for harness auth; leased sessions are
  exempt (D9 of RESOLUTIONS), which keeps today's rule that `hal off` does not kill a running
  coding session (`power.py` header) while still freeing the GPU from the assistant.
- **D6 `hal claude` injects `--permission-mode default`, not `acceptEdits`.** The old reason
  (auto mode's approver timing out) is satisfied by `default`, which also stops unasked edits
  on the host.
- **D7 `hal connect` prints the token as a file reference and uses a named token.** Printed
  snippets end up in notes and screenshots; `--write` puts the value only into the harness's
  own config file; a per-harness token can be revoked alone.
- **D8 Packs install from the commit, not the tag.** A tag can be moved; the commit cannot.
  The tag is shown so people can read it. The commit pins only the pack's own code, so its
  dependencies and build backends come from a hashed requirements file per env kind shipped
  with the release (REQ-CLI-110).
- **D9 `hal migrate ecosystem` is a command, not a private script.** It serves any user of the
  old `hal-ecosystem/install.sh` layout, it can be tested with a synthetic tree, and its
  journal makes the cutover reversible.
- **D10 No `Brain` compatibility shim.** hal-voice and the Telegram bridge are ACP clients of
  `hal acp` in the same release (A1, B9), and nothing outside Hal imports `halcore`.
- **D11 Tables `[claude]`, `[units]` and `[long_task]` added to halcore's ownership.**
  00-overview lists `[console]`, `[ui]`, `[units]`; `[claude]` holds the legacy harness
  settings that used to live in `hal.conf`, and `[long_task]` the job limits. The overview's
  table should list both.
- **D12 ASCII rendering by default.** Matches the repo's ASCII rule for strings in code and
  keeps transcripts and screenshots clean; `[ui].unicode` turns rich's defaults back on.
- **D13 Headless long jobs queue approvals for up to an hour.** `[long_task].queue_timeout_s
  = 3600` matches the old `deep_work` one-hour timeout (`brain.py:203-227`).
- **D14 The assistant box lives as long as its `hal` process.** A box that outlives the
  process would need a daemon to own it, and voice and Telegram already keep one `hal acp`
  process alive all day; `hal chat` pays one warm start (about 0.3 s) per run, which is below
  what a user notices at the start of a chat.
- **D15 "Cloud" long jobs change only the model.** `where = "cloud"` routes the same
  sandboxed `hal -p` to a cloud upstream behind the gateway (`[long_task].cloud_model`), so
  the box, broker and policy are identical and no unsandboxed `claude -p` runs on the host.
  It is off unless `[agent].cloud_jobs` is true (RESOLUTIONS F2).
- **D16 The manifest's `gpu` flag carries `X-HalGPU`.** One source (04's `[[units]] gpu`)
  that the renderer turns into the marker and the off condition; templates that already have
  the line (hal-employment's unit) keep working without edits (F9).
- **D17 Catalogue pack code installs without a prompt; install steps always ask.** The
  catalogue's commits ship inside a reviewed Hal release, so asking again adds a click, not a
  check; `[[install]]` steps run arbitrary argv that can change files outside Hal, so they
  need a typed `y` every time and no flag skips them (B4).
- **D18 Unboxed channels need a recorded consent.** Voice and Telegram have no terminal to
  ask on, and a banner nobody sees is not consent (architect section 3), so the first unboxed
  start of each channel must be confirmed once from a terminal (`hal chat` asks for its own
  channel; `hal doctor` prints the command for the others: `hal acp --channel voice
  --toolset assistant` run once on a terminal).

## Open questions

1. **hal-employment's `addon.hal_root`.** This spec keeps `hal-ecosystem/core/hal/hal.conf` in
   place, lists it in `[compat].hal_conf` and only verifies it (H3), so nothing changes
   whatever the setting is. Do you want the cutover to point `addon.hal_root` at a new
   location later? That setting is in hal-employment, which this work does not touch.
2. **Vault `.mcp.json`.** The cutover leaves the stale `hal` server entry until you run
   `hal packs setup memory` and approve it (it removes the entry and keeps a backup). Approve
   that edit at cutover time?
3. **Dangling links.** `~/.local/bin/hal-old` and `a-bot` point at folders that no longer
   exist. Remove them during the cutover, or leave them?
4. **Plain `hal` default.** Today `hal` opens Claude Code in your vault. The new default opens
   Hal's own TUI (code in a project, chat elsewhere). Keep the new default, or set
   `[ui].default_mode` so `hal` behaves as close to the old one as possible?
