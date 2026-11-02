# Hal sandbox: backends, images, sockets, checkpoints and hal box

This spec defines how Hal confines the agent and every tool it runs: the container and
bubblewrap backends with their exact flags, the images, the per-session socket directory, the
MCP transport to the broker, the model socket and the in-box relay, workspace checkpoints and
`--isolated` mode, dependency preparation, secret masking, and `hal box`, which runs
third-party agent CLIs in the same box. Broker policy, approvals and audit belong to
01-security; the model forwarder's HTTP handling belongs to 05-gateway; this spec owns
everything between the box and those two components.

Status: draft 2026-10-23, revised 2026-10-24 to match
`~/src/.dev-notes/specs/RESOLUTIONS.md` (binding; ids such as E6 or F3 below
refer to it), and revised 2026-10-29 for the security gaps review
(`~/src/.dev-notes/specs/review-security-gaps.md`; ids G01 to G21 below refer to
it).

Evidence labels: `[verified: how]` means checked on this machine on 2026-10-23 (or the date
given) with the command or experiment named; `[unverified]` means not checked, with the way to
check it. Throwaway experiments ran in a scratch directory that has since been removed
(working files now live in `~/src/.dev-notes/`); their images and containers
were removed afterwards.

## Scope and non-goals

This spec owns:

- backend selection (`docker` with `runc` or `runsc`, `bwrap`, `none`) and probing;
- the exact `docker run` and `bwrap` argv, every flag with its reason;
- uid and gid mapping, the generated `/etc/passwd`, the environment allowlist, resource limits;
- the images: base, contents, size budget, digest pinning, `hal box image build`, and the
  per-CLI box images for `hal box`;
- the session runtime directory `$R/s/<sid>/` and its sockets, the broker socket transport
  (MCP stdio framing over a unix socket), and the lifeline socket;
- the box-side programs `hal-box-entry`, `hal-relay` and `hal-mcp-bridge` (stdlib Python);
- container names, labels and orphan cleanup;
- checkpoints (git and non-git), `/undo`, `/diff`, `--isolated` with accept and reject;
- symlink and path-escape handling for host code that touches the workspace;
- `hal box`: per-CLI config writers and offline settings for claude, qwen, opencode, codex,
  crush and goose;
- the behaviour behind `hal box`, `hal box image build|status`, `hal box harnesses` (command
  tree in 06) and the standalone `hal-sandbox` script (`doctor`, `gc`, `ls`, `shell`, `undo`,
  `diff`, `deps`, `forget`);
- the transport and session wiring of the broker, how the broker turns 03-agent's
  `host = true` MCP server entries into upstreams, and the broker result metadata
  `result._meta.hal` (policy and approvals inside the broker are 01-security's), and the
  mapping of upstream elicitations to host approval requests;
- the terminal filter between a box's TTY and the user's terminal, and terminal-safe output
  of the sandbox commands;
- dependency preparation (`hal code --deps`): the prep box and the host egress proxy it uses;
- secret masking in the work tree (`[sandbox].mask`), in checkpoints and isolated snapshots,
  the sanitized git config, and per-project sandbox choices
  (unmask, the one-time consent to backend `none`).

Non-goals:

- Broker policy, tiers, approvals, guard models and audit format (01-security).
- The HTTP handling inside the model forwarder: routes, token injection, size caps
  (`SessionForwarder`, REQ-GW-60 to REQ-GW-64 in 05-gateway). This spec only places its
  socket, wires it into the session and tests the path end to end.
- GPU containers for inference (05-gateway).
- The agent's tools and ACP server (03-agent); this spec only builds `agent_argv`, and
  halcore spawns it (RESOLUTIONS A7, Decision D20).
- Windows or macOS hosts, Podman, Firecracker or VM backends. Podman through its docker
  shim may work but is not tested [unverified].
- Per-project sandbox config files inside the workspace. The workspace is writable by the
  model (00-overview D4), so per-project choices live on the host (REQ-BOX-70).
- Protection against kernel, runc, gVisor or Docker daemon bugs. gVisor narrows the kernel
  surface; nothing here closes it.

## Requirements

Test kinds: **unit** (hermetic, in-process, fakes), **integration** (real git, real unix
sockets and subprocesses, no docker; runs by default), **docker** (marker `docker`), **bwrap**
(marker `bwrap`, needs unprivileged user namespaces), **systemd** (marker `systemd`, needs a
user manager), **network** (marker `network`, reaches the real package registries), **slow**,
**manual**. The marker set `gpu docker network slow bwrap systemd live` is registered by
00-overview (RESOLUTIONS E12). Docker tests marked `runtime` run twice, once per runtime
(`runc`, `runsc`); the `runsc` run is skipped with the probe's reason when the runsc probe
fails. Each requirement names the test that proves it. Test files are under
`packages/hal-sandbox/tests/` unless a path says otherwise.

### Backends and detection

- **REQ-BOX-01** The sandbox MUST support the backends `docker`, `bwrap` and `none`, chosen
  by `[sandbox].backend` (default `auto`). Test: unit `test_detect.py::test_each_backend_name_is_accepted`.
- **REQ-BOX-02** With `auto`, Hal MUST pick `docker` when the CLI is found and
  `docker version` answers within 5 s, else `bwrap` when its probe passes within 2 s, else
  `none`. When the last successful detection chose `docker` (recorded in
  `$STATE/sandbox/last-backend`, which does not expire with the probe cache) and docker does
  not answer now, `auto` MUST refuse to start with an error naming the cause and the fixes
  (start the daemon, or pass `--backend bwrap` or `--backend none` for this run) instead of
  degrading silently (RESOLUTIONS "Fallback"). Test: unit
  `test_detect.py::test_auto_prefers_docker_then_bwrap_then_none`,
  `test_detect.py::test_docker_down_after_docker_was_used_refuses` (fake `docker` and `bwrap`
  binaries through `HAL_DOCKER` and `HAL_BWRAP`).
- **REQ-BOX-03** With `docker`, Hal MUST use runtime `runsc` when `docker info` lists it and
  the runsc probe (a box connects to a host unix socket through a read-only bind) passes, else
  `runc`; the session status MUST name the runtime. A failed probe MUST name the fix
  (register runsc with `"runtimeArgs": ["--host-uds=open"]`). Test: unit
  `test_detect.py::test_runsc_used_only_after_probe_passes`; docker
  `test_docker_live.py::test_runtime_is_reported` (runc on this machine until runsc is
  registered with `--host-uds=open`).
- **REQ-BOX-04** A backend or runtime set explicitly in config or by flag that is not usable
  MUST be an error that names the cause and the fix; it MUST NOT be replaced by another
  backend. Test: unit `test_detect.py::test_explicit_backend_failure_is_an_error`.
- **REQ-BOX-05** `[sandbox].require = true` MUST turn the `none` result of `auto` into an
  error (fail closed). Test: unit `test_detect.py::test_require_refuses_none`.
- **REQ-BOX-68** Before the first session with backend `none` in a project, Hal MUST ask once
  on a terminal and record a yes per project (REQ-BOX-70); without a terminal and without
  a recorded yes, the session MUST refuse to start. A banner alone is not consent
  (RESOLUTIONS "Fallback"). Test: unit `test_projects.py::test_none_asks_once_per_project`,
  `test_projects.py::test_none_without_terminal_refuses`.
- **REQ-BOX-06** Probe results MUST be cached in `$STATE/sandbox/probe.json` for at most
  24 h, keyed by docker server version and runtime list; `hal-sandbox doctor` and
  `--refresh-probe` MUST bypass the cache. Test: unit `test_detect.py::test_probe_cache_expires`.
- **REQ-BOX-07** With backend `none`, the session capabilities MUST say
  `isolation = "none"`, `run = false`, `paths = "confined"`, and the agent MUST NOT be offered
  the `run` tool; the agent then refuses user MCP servers not marked `host` (03-agent,
  RESOLUTIONS F4). Test: unit `test_backend_none.py::test_none_disables_run_and_confines_paths`;
  the banner itself is tested in 06.

### Docker hardening

- **REQ-BOX-08** Every docker box MUST be started with exactly the flags in "Docker argv"
  (`--network none`, `--read-only`, `--cap-drop ALL`,
  `--security-opt no-new-privileges=true`, `--init`, `--ipc private`, `--pull never`,
  `--log-driver none`, `--cidfile`, pids, memory equal to memory-swap, cpus,
  `--ulimit core=0`), and the
  builder MUST refuse to emit `--privileged`, `--device`, `--gpus`, `--cap-add`,
  `seccomp=unconfined`, `apparmor=unconfined`, `--pid host`, `--network` other than `none`,
  `--ipc host`, `-v` or a `--mount` not listed in REQ-BOX-14. Test: unit
  `test_docker_argv.py::test_argv_matches_golden` and
  `test_docker_argv.py::test_forbidden_flags_are_rejected`.
- **REQ-BOX-09** Inside a docker box the only network interface MUST be `lo`, outbound
  connections MUST fail, `CapEff` MUST be 0 and `NoNewPrivs` MUST be 1; on runc `Seccomp`
  MUST be 2 and the AppArmor label MUST be `docker-default` when the host runs AppArmor (under
  runsc the guest kernel reports its own values and the test checks gVisor's `dmesg` banner
  instead). Test: docker, runtime
  `test_docker_live.py::test_box_has_no_network_caps_or_privileges`.
- **REQ-BOX-10** The root filesystem MUST be read-only; only `/work`, `/tmp` and `/home/hal`
  MAY be writable. Test: docker `test_docker_live.py::test_only_work_tmp_home_are_writable`.
- **REQ-BOX-11** The argv builder MUST CSV-quote every `--mount` field so host paths
  containing `,`, `"`, `:` or `=` work, and MUST reject paths containing NUL or a newline.
  Test: unit `test_docker_argv.py::test_mount_fields_are_csv_quoted`; docker
  `test_docker_live.py::test_project_path_with_comma_and_quote_mounts`.
- **REQ-BOX-12** Box processes MUST run with the invoking user's host uid and gid, so files
  they write keep the user's ownership: `--user <uid>:<gid>` on rootful docker,
  plus `--userns host` when the daemon uses `userns-remap`, and `--user 0:0` on rootless
  docker. Test: unit `test_docker_argv.py::test_user_mapping_per_daemon_mode`; docker, runtime
  `test_docker_live.py::test_files_written_in_box_are_owned_by_user`.
- **REQ-BOX-13** Each session MUST generate `/etc/passwd` and `/etc/group` naming user `hal`
  with home `/home/hal`, mounted read-only. Test: docker
  `test_docker_live.py::test_getpwuid_resolves_hal`.
- **REQ-BOX-14** A docker box MUST have exactly these mounts: the work tree at `/work` (rw),
  git metadata per REQ-BOX-15 (ro), `$R/s/<sid>/run` at `/run/hal` (ro), the generated
  passwd and group files (ro), tmpfs `/tmp` and `/home/hal`, one read-only empty-file bind per
  masked path (REQ-BOX-69), the sanitized git config files (ro) only when REQ-BOX-15 calls
  for them, plus the uv cache (ro) only when
  `[sandbox].mount_uv_cache = true`. It MUST NOT mount `$HOME`, `~/.ssh`, `~/.config`,
  `$XDG_RUNTIME_DIR`, the docker socket or any device. Test: unit
  `test_docker_argv.py::test_mount_list_is_exact`; docker, runtime
  `test_docker_live.py::test_mountinfo_has_only_expected_mounts`.
- **REQ-BOX-15** When `<work>/.git` is a directory it MUST be bind-mounted read-only at
  `/work/.git`. When it is a gitfile (linked worktree or submodule), the gitfile MUST be
  bind-mounted read-only and its git dir and common dir MUST be bind-mounted read-only at the
  paths the gitfile resolves to inside the box. Any write to git metadata from the box MUST
  fail with EROFS. When a mounted git config file (`config`, and `config.worktree` when
  present) holds URL userinfo with a password or on an `http(s)` URL, an
  `http.extraHeader` or `http.<url>.extraHeader` value, or any `credential` section, the
  session MUST bind a sanitized copy read-only over that file in the box (on docker and
  bwrap), MUST set `git_config_sanitized` in `session.json`, and MUST show a notice naming
  the keys removed (never their values); a copy that still matches after sanitizing MUST stop
  the session with an error (see "Sanitized git config"). Test: unit
  `test_gitlayout.py::test_gitfile_layout_resolves_mounts`,
  `test_gitlayout.py::test_config_credentials_are_detected_and_removed`; docker
  `test_docker_live.py::test_git_metadata_is_read_only` (hook planting, `git add`, `git commit`,
  `git stash` all fail), `test_docker_live.py::test_git_config_token_not_readable_in_box` (a
  `remote.origin.url` of `https://user:tok123@example.org/r.git` and an `extraHeader`: `cat
  /work/.git/config` and `git config --list` in the box never print `tok123`).
- **REQ-BOX-16** `/run/hal` MUST be mounted read-only; connecting to its sockets MUST still
  work, on runc and on runsc registered with `--host-uds=open` (RESOLUTIONS E10, G1). Test:
  docker, runtime `test_docker_live.py::test_sockets_connect_through_read_only_bind`.
- **REQ-BOX-17** Every docker box MUST get `--oom-score-adj 500`, so the kernel kills a box
  before the desktop or the inference server. Test: unit golden argv; docker
  `test_docker_live.py::test_oom_score_adj_is_set`.

### Environment and limits

- **REQ-BOX-18** The box environment MUST be exactly the fixed set in "Environment", plus
  host values of the pass-through names, plus names listed in `[sandbox].env_passthrough`.
  A pass-through name matching the secret pattern MUST be a `ConfigError`. Test: unit
  `test_env.py::test_box_env_is_allowlisted`, `test_env.py::test_secret_like_passthrough_is_refused`.
- **REQ-BOX-19** With `HAL_GATEWAY_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `SSH_AUTH_SOCK`, `DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR`, `DISPLAY` and
  `WAYLAND_DISPLAY` set on the host, none of them MUST appear in the box environment. Test:
  unit `test_env.py::test_host_secrets_never_reach_box`; docker
  `tests/e2e/test_box_secrets.py` (REQ-SYS-12).
- **REQ-BOX-20** Limits MUST default to: memory `min(8 GiB, RAM/4)` with swap equal to
  memory, cpus `min(4, nproc/2)`, pids 1024, nofile 8192, `/tmp` 1 GiB, `/home/hal` 512 MiB,
  stop timeout 10 s; each MUST be configurable under `[sandbox.limits]`. Test: unit
  `test_limits.py::test_auto_limits_from_host_size`.
- **REQ-BOX-21** The bwrap backend SHOULD apply the same memory, pids and CPU limits through
  `systemd-run --user --scope` when a user manager answers; without one it MUST mark the
  session `limits = "none"` so the UI says so. Test: unit
  `test_bwrap_argv.py::test_scope_wrapper_when_user_manager_present`,
  `test_bwrap_argv.py::test_no_limits_is_reported`.

### Images

- **REQ-BOX-22** Images MUST be built only by `hal box image build`, which is the only sandbox
  step allowed network access; starting a box MUST never pull (`--pull never`). Test: unit
  `test_images.py::test_build_is_the_only_networked_step` (argv inspection); docker
  `test_images_live.py::test_missing_image_is_an_error_not_a_pull`.
- **REQ-BOX-23** Base images MUST be pinned by digest; Python dependencies MUST install with
  `--require-hashes`; npm CLIs MUST install with `npm ci` from a committed lockfile; release
  tarballs (goose) MUST be checked with a pinned sha256. Test: unit
  `test_images.py::test_every_input_is_pinned`.
- **REQ-BOX-24** Pinned CLI versions SHOULD be at least 14 days old when pinned, recorded in
  `images/pins.toml` with their publish date. Test: unit `test_images.py::test_pins_have_dates`
  (checks the file; the age rule is a review item at bump time, manual).
- **REQ-BOX-25** The `full` image MUST contain bash, coreutils, findutils, grep, sed, gawk,
  diffutils, patch, file, less, procps, git, ripgrep, fd, make, a C toolchain, pkg-config,
  python3 with venv, `uv`, and `/opt/hal/{venv,bin}` (plus node and npm when
  `[sandbox].node = true`); `hal-box-entry --selftest` MUST verify them.
  Test: docker `test_images_live.py::test_selftest_passes`.
- **REQ-BOX-26** On-disk size as reported by `docker images` MUST stay within: `slim` 450 MB,
  `full` 850 MB, `full` with node 1.1 GB, each `hal-box-<cli>` 1.5 GB. Test: docker+slow
  `test_images_live.py::test_image_sizes_within_budget`.
- **REQ-BOX-27** A box MUST run the image by its image ID resolved at session start, and a
  missing image MUST produce an error that names `hal box image build` (headless) or an offer to
  build (TUI, 06). Test: unit `test_images.py::test_missing_image_error_names_build_command`.

### Session directory and transports

- **REQ-BOX-28** Each session MUST own `$R/s/<sid>/` (0700) with `run/` (0700), `etc/`,
  `empty` (the mask source) and `owner.json`, where `$R` is `halkit.paths.runtime_dir()`
  (00-overview, one rule per RESOLUTIONS F6); sockets MUST be 0600; every socket path MUST
  pass `check_socket_path` (107 bytes). Test: unit
  `test_session.py::test_runtime_dir_layout_and_modes`.
- **REQ-BOX-29** The broker socket MUST carry MCP messages framed as newline-delimited UTF-8
  JSON-RPC (the MCP stdio framing), one MCP client per connection, at most
  `max_message_bytes` (default 4 MiB) per message in each direction. An oversize message MUST
  get one JSON-RPC error `-32600` with `id: null`, then the connection MUST close. This spec
  owns the 4 MiB cap; 01-security's broker limits refer to it (RESOLUTIONS E9). Test: unit
  `test_transport.py::test_oversize_message_gets_error_then_close`.
- **REQ-BOX-30** The broker transport MUST time out a partial message after 30 s and MUST
  refuse connections beyond `max_connections` (default 8) with an error frame and close.
  Test: unit `test_transport.py::test_slow_partial_message_times_out`,
  `test_transport.py::test_connection_cap`.
- **REQ-BOX-31** The transport MUST hand each complete message to the broker unchanged and
  MUST NOT apply policy itself. The broker MUST serve every box connection with
  `approvals = "callback"`, so no MCP elicitation is ever sent to a client inside a box
  (RESOLUTIONS B4). Test: unit `test_transport.py::test_messages_pass_unchanged`,
  `test_broker_wiring.py::test_box_connections_never_get_elicitation`.
- **REQ-BOX-32** `Session.start` MUST NOT spawn the box; it MUST return `model_socket` and
  `agent_argv`, and `box.run` MUST call its `start_model` callable before the box starts and
  close it after the box exits. The forwarder itself is `halgateway.SessionForwarder`
  (REQ-GW-63). Test: unit `test_session.py::test_start_prepares_without_spawning`,
  `test_box.py::test_model_service_wraps_box_lifetime` (fake callable).
- **REQ-BOX-76** `Session.broker` MUST be the session's `halsandbox.broker.Broker`, so that
  halcore can call `Broker.on_human_message(session, text, surface)` (01-security, RESOLUTIONS
  F5) for every prompt it sends; with backend `none` and with the unboxed assistant fallback
  (RESOLUTIONS A1) the same broker serves the same socket. Test: unit
  `test_session.py::test_session_exposes_its_broker`.
- **REQ-BOX-33** No token, secret file or secret value MUST appear in the box: not in its
  environment, `session.json`, the home seed, the generated `/etc` files or the argv. Test:
  unit `test_session.py::test_no_token_in_box_visible_files`; docker
  `tests/e2e/test_box_secrets.py`.
- **REQ-BOX-34** The session MUST serve a lifeline on `run/lifeline.sock`; when the host side
  closes it, `hal-box-entry` MUST SIGTERM the child's process group, SIGKILL it after the
  grace period (default 5 s), and exit 143. Test: unit
  `test_box_entry.py::test_lifeline_close_terminates_child`; docker
  `test_docker_live.py::test_box_dies_when_host_session_is_killed`.

### Box-side programs

- **REQ-BOX-35** `hal-box-entry`, `hal-relay` and `hal-mcp-bridge` MUST import only the
  standard library and MUST run on Python 3.11 to 3.13. Test: unit
  `test_boxside_imports.py::test_boxside_modules_import_only_stdlib` (AST scan).
- **REQ-BOX-36** `hal-box-entry` MUST copy the image seed then the session seed into `$HOME`,
  connect the lifeline (exit 69 if it cannot within 2 s), start the relay for `box`,
  `shell` and `deps` kinds, run the child in its own process group with inherited stdio, and
  exit with the child's exit code. Test: unit `test_box_entry.py` (runs on the host with temp dirs).
- **REQ-BOX-37** `hal-relay` MUST listen on `127.0.0.1:<port>` only (default 8741), MUST
  splice bytes to a new connection on its socket (`/run/hal/model.sock`, or
  `/run/hal/egress.sock` in the prep box) per accepted connection, MUST
  cap concurrent connections at 32, and MUST forward bytes as they arrive (an SSE event
  reaches the client within 50 ms of the upstream write). Test: unit
  `test_relay.py::test_relay_streams_without_buffering`; docker
  `test_docker_live.py::test_relay_streams_sse_from_host_socket`.
- **REQ-BOX-38** `hal-mcp-bridge` MUST splice its stdin to `/run/hal/broker.sock` and the
  socket to its stdout, MUST write nothing else to stdout, MUST exit 0 when either side closes,
  and MUST exit 69 with a one-line stderr message when it cannot connect. Test: unit
  `test_bridge.py`.

### Lifecycle, names and orphans

- **REQ-BOX-39** Containers MUST be named `hal-<kind>-<sid>` and carry the labels in
  "Names and labels". Test: unit golden argv.
- **REQ-BOX-40** At session end the box MUST be gone within `stop_timeout_s + 5` s, also when
  the `docker` client process was SIGKILLed. Test: docker
  `test_docker_live.py::test_box_dies_when_host_session_is_killed`.
- **REQ-BOX-41** The orphan sweep MUST remove only containers labelled `hal.sandbox=1` and
  `hal.uid=<own uid>` whose `hal.pid` is not alive with the recorded `hal.pidstart`; it MUST
  never touch any other container. It MUST run at most hourly at `hal` start and on
  `hal-sandbox gc`. Test: unit `test_labels.py::test_sweep_removes_only_dead_own_boxes` (fake
  docker); docker `test_docker_live.py::test_sweep_keeps_foreign_containers`.
- **REQ-BOX-42** The sweep MUST remove `$R/s/<sid>/` directories whose `owner.json` pid is
  dead, and MUST NOT remove an isolated worktree that differs from its base. Test: unit
  `test_labels.py::test_sweep_runtime_dirs`, `test_isolated.py::test_gc_keeps_pending_work`.
- **REQ-BOX-43** Sandbox lifecycle events (backend chosen, box start, stop and exit code,
  checkpoint, restore, accept, reject, sweep removal, masks applied, git config sanitized,
  raw terminal in use, dependency prep) MUST be
  appended as JSONL with UTC `Z` timestamps to `$STATE/sandbox/events.jsonl` through
  `halkit.files.append_jsonl`. This is an operational log, not a second audit log: every
  policy or approval decision (including the dependency-prep approval) goes to 01-security's
  audit log through its `EventSink` (RESOLUTIONS B8). Test: unit `test_events.py`.

### Checkpoints

- **REQ-BOX-44** For a git work tree the host MUST take a checkpoint before every agent turn
  as a commit made with a temporary index and stored at `refs/hal/checkpoints/<sid>/<n>`; the
  user's index file, HEAD, branches and stash MUST stay byte-identical. Test: integration
  `test_checkpoints.py::test_checkpoint_leaves_index_and_branches_untouched`.
- **REQ-BOX-45** A checkpoint MUST contain tracked and untracked non-ignored files, MUST
  exclude ignored files, and MUST leave out files larger than `max_file_bytes` (default
  50 MiB), listing them in the commit message. Unless the project is unmasked (REQ-BOX-70), a
  checkpoint MUST also leave out, by pathspec and by removal from its temporary index, every
  path whose name matches a `[sandbox].mask` pattern or one of 01-security's hard-deny file
  patterns, and MUST record those paths as `masked` in the checkpoint metadata, because
  checkpoint objects land in `<work>/.git/objects`, which the box can read (REQ-BOX-15). A
  masked path recorded at the target that no longer exists MUST appear in `not_restorable`
  (REQ-BOX-47). Test: integration `test_checkpoints.py::test_checkpoint_contents`,
  `test_checkpoints.py::test_masked_paths_left_out_and_listed`; docker
  `test_docker_live.py::test_masked_file_not_readable_through_git_objects` (an untracked,
  non-ignored `deploy/server.key`: `git cat-file --batch-all-objects` and `git show` on every
  `refs/hal/*` ref in the box never print its content).
- **REQ-BOX-46** A checkpoint whose tree equals the previous one MUST NOT create a new ref.
  Test: integration `test_checkpoints.py::test_unchanged_tree_reuses_checkpoint`.
- **REQ-BOX-47** Restore MUST first take a safety checkpoint, then make every non-ignored
  path equal to the target checkpoint (restore changed and deleted files, delete files added
  since), and MUST NOT touch ignored files, skipped large files, masked files or the user's
  index. Its result MUST list, as not restorable, every ignored, skipped or masked path
  recorded at the target checkpoint that no longer exists, and `/undo` MUST show that list rather than skip it
  silently (01-security REQ-SEC-70 and its residual risk for ignored files, security review
  R-new-5, ATK-43). Test: integration
  `test_checkpoints.py::test_restore_round_trip`,
  `test_checkpoints.py::test_deleted_ignored_file_is_reported_not_restorable`.
- **REQ-BOX-48** Every host git call MUST go through `GitRunner`, which passes explicit
  `--git-dir` and `--work-tree`, runs with the working directory outside the work tree,
  removes every inherited `GIT_*` variable, sets `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_CONFIG_GLOBAL=/dev/null`, the hardening `-c` options and a timeout (01-security
  REQ-SEC-67). A nested repository whose `core.fsmonitor` runs a marker command MUST NOT run
  it during checkpoint, diff or restore. Test: integration
  `test_gitops.py::test_nested_repo_fsmonitor_never_runs` (regression for the finding in
  "Path and git safety"), `test_gitops.py::test_inherited_git_env_is_removed`.
- **REQ-BOX-49** A checkpoint failure MUST stop the turn from starting unless the session was
  started with `--no-checkpoint`. Test: unit `test_checkpoints.py::test_failure_blocks_turn`.
- **REQ-BOX-50** `/diff` MUST return a `DiffReport` with patch, stat and flags; the flags MUST
  cover nested repositories, symlinks pointing outside the work tree, newly executable files,
  git attribute files and every pattern in 01-security's "Workspace scan" table, so this
  report is the implementation of REQ-SEC-70. Test: integration
  `test_diffreport.py::test_flags_for_risky_changes`.
- **REQ-BOX-51** Checkpoint refs older than `max_age_days` (14) beyond the newest
  `keep_sessions` (20) sessions MUST be deleted when a session starts in that repository.
  Test: integration `test_checkpoints.py::test_prune_retention`.
- **REQ-BOX-52** For a non-git work tree, checkpoints MUST use a shadow repository under
  `$STATE/shadow/` with the same API and the same `GitRunner` (RESOLUTIONS E8: no tar
  checkpoints anywhere); when the non-ignored content exceeds
  `nongit_max_total_bytes` (512 MiB) the session MUST refuse to start without `--isolated` or
  `--no-checkpoint`. Test: integration `test_shadow.py`.

### Isolated mode

- **REQ-BOX-53** `--isolated` on a git work tree MUST snapshot the current state (including
  uncommitted and untracked non-ignored files) as `refs/hal/isolated/<sid>/base`, check it out
  with `git worktree add --detach` under `$STATE/worktrees/<sid>`, and mount only that
  worktree at `/work`. The snapshot MUST apply the masked-path exclusion of REQ-BOX-45, so
  masked files are neither in its objects nor checked out into the worktree, and mask
  discovery (REQ-BOX-69) MUST run on the worktree that is mounted, not on the project. The
  same holds for the base commit of a non-git isolated copy (REQ-BOX-56). Test: integration
  `test_isolated.py::test_worktree_from_snapshot`,
  `test_isolated.py::test_snapshot_leaves_out_masked_files`; docker
  `test_docker_live.py::test_isolated_box_sees_only_worktree`.
- **REQ-BOX-54** Accept MUST show the `DiffReport`, MUST refuse when a `high` flag is present
  unless the caller passes `allow_flags`, MUST take a checkpoint of the project first, and
  MUST apply with `git apply` to the work tree only, all or nothing; on failure nothing MUST
  change and the patch MUST be saved under `$STATE/patches/<sid>.patch`. Test: integration
  `test_isolated.py::test_accept_is_atomic_and_checkpointed`,
  `test_isolated.py::test_high_flags_block_accept`.
- **REQ-BOX-55** Reject MUST remove the worktree, its admin directory and the
  `refs/hal/isolated/<sid>/` refs. Test: integration `test_isolated.py::test_reject_cleans_up`.
- **REQ-BOX-56** `--isolated` on a non-git directory MUST copy it (cap `max_copy_bytes`,
  2 GiB) and accept through `git apply` outside a repository. Test: integration
  `test_isolated.py::test_nongit_copy_and_accept`.

### Path safety

- **REQ-BOX-57** Host code in halsandbox that opens a path inside the work tree (checkpoint
  helpers, diff viewers, mask discovery, dependency-prep lockfile reads) MUST open it with
  `halkit.fsafe.open_beneath` (one implementation, owned by 00-overview with the semantics in
  01-security "Proposed halkit additions", RESOLUTIONS C10), MUST refuse non-regular files for
  reading, and MUST show a symlink as a link and its target, never its content. halsandbox
  has no second path walker. Test: unit `test_paths.py` (cases listed in "Test plan").
- **REQ-BOX-58** Translation from box paths (`/work/...`) to host paths MUST go through
  `box_to_host`, which MUST reject NUL, `..` escapes and paths outside `/work`. Test: unit
  `test_paths.py::test_box_to_host_rejects_escapes`.

### Secret masking and per-project choices

- **REQ-BOX-69** At session start, every regular file in the work tree whose name matches a
  `[sandbox].mask` pattern MUST be bound over inside the box by an empty read-only file, on
  the docker and bwrap backends, and the backend `none` path confinement (03-agent
  REQ-AGT-37) MUST refuse those paths; the session MUST show a notice naming the count and
  listing the paths, and `session.json` MUST list them (RESOLUTIONS "Masking", 01-security
  REQ-SEC-98, security review R-new-6). The default list `MASK_DEFAULT` MUST be imported from
  01-security, which defines it once as its hard-deny file patterns plus `.npmrc`, `.pypirc`,
  `.netrc`, `.git-credentials`, `*.p12`, `*.pfx` and `*.tfstate` (a superset of the six
  RESOLUTIONS names; Decision D31). The walk MUST descend into directories git ignores,
  skipping only `.git`, `.hal`, `node_modules`, `.venv` and `venv`, within `MASK_MAX_FILES`
  and `MASK_WALK_TIMEOUT_S`, because a secret under an ignored `secrets/` is the common case.
  Test: unit `test_mask.py::test_matches_and_mount_list`,
  `test_mask.py::test_secret_under_ignored_directory_is_masked`,
  `test_mask.py::test_default_equals_security_list`; docker, runtime
  `test_docker_live.py::test_masked_file_reads_empty_and_cannot_be_written`; bwrap
  `test_bwrap_live.py::test_masked_file_reads_empty`.
- **REQ-BOX-70** Per-project choices (masking off after `--unmask`, consent to backend `none`)
  MUST be stored only on the host in `$STATE/sandbox/projects.json`, keyed by the project's
  real path, MUST be set only from a terminal after a typed confirmation, and MUST be revocable
  (`--mask`, `hal-sandbox forget DIR`). The file holds no approval grants. Test: unit
  `test_projects.py::test_unmask_is_remembered_per_project`,
  `test_projects.py::test_choices_need_a_terminal`.

### Dependency preparation

- **REQ-BOX-71** `halsandbox.deps.prepare` (behind `hal code --deps`, 06-core-cli) MUST run
  the install for each detected lockfile (`uv.lock` -> `uv sync --frozen`,
  `package-lock.json` -> `npm ci`, `Cargo.lock` -> `cargo fetch --locked`) in a separate prep
  box of kind `deps` that has no broker socket and no model socket (RESOLUTIONS F3), on the
  docker backend only (any other backend is an error naming docker). Test: unit
  `test_deps.py::test_commands_per_lockfile`, `test_deps.py::test_requires_docker`,
  `test_docker_argv.py::test_deps_box_mounts`.
- **REQ-BOX-72** The prep box MUST keep `--network none`; its only route out MUST be the host
  egress proxy on `run/egress.sock`, reached through `hal-relay` on box loopback, and the proxy
  MUST allow only `CONNECT <host>:443` to hosts in `[sandbox.deps].registries`, after
  resolving the host on the host side and refusing private, loopback and link-local
  addresses through `halkit.netguard`. Every other request MUST get 403 and be logged. Test:
  unit `test_egress.py::test_only_listed_hosts_on_443`,
  `test_egress.py::test_private_address_is_refused`; docker+network
  `test_deps_live.py::test_uv_sync_through_proxy`.
- **REQ-BOX-73** The prep MUST write only inside the work tree: the uv environment at
  `/work/.hal/venv` (`UV_PROJECT_ENVIRONMENT`), caches and `CARGO_HOME` under `/work/.hal/`,
  and `node_modules` where npm puts it; `/work/.hal/` MUST contain a `.gitignore` of `*`.
  Python MUST come from the box image (`UV_PYTHON=/usr/bin/python3`,
  `UV_PYTHON_DOWNLOADS=never`). Test: docker+network
  `test_deps_live.py::test_prep_writes_only_under_work`; unit
  `test_deps.py::test_prep_env_pins_image_python`.
- **REQ-BOX-74** Each prep MUST be approved once per lockfile sha256 through the caller's
  `Approver` (01-security) as tool `sandbox_deps_prep` (pack `sandbox`, tier `act`,
  arguments `manager`, `lockfile`, `sha256`), and MUST be offered (never run unprompted) only when a lockfile's
  sha256 differs from the one recorded after the last successful prep of that project. The
  record is a "done" marker, not a grant: a missing or forged record only causes another
  offer. Test: unit `test_deps.py::test_offer_only_when_lockfile_changed`,
  `test_deps.py::test_denied_approval_runs_nothing`.
- **REQ-BOX-75** The prep box MUST stop after `[sandbox.deps].timeout_s` (default 900 s) and
  MUST take a checkpoint of the project before it starts. Test: unit
  `test_deps.py::test_timeout_stops_prep_box`, `test_deps.py::test_checkpoint_before_prep`.

### hal box

- **REQ-BOX-59** `hal box` MUST support `claude`, `qwen`, `opencode`, `codex`, `crush` and
  `goose`, on the docker backend only; any other backend MUST be an error that says why.
  Test: unit `test_box.py::test_box_requires_docker`.
- **REQ-BOX-60** Each CLI writer MUST point the CLI at the relay URL and at
  `hal-mcp-bridge`, MUST turn off telemetry and update checks per the "Per-CLI templates"
  table, MUST disable the CLI's own sandbox, and MUST map `[box].auto_approve` (default true)
  to the CLI's own auto-approve setting. The CLI's auto-approve covers in-box work only:
  broker `ask` decisions still go to host surfaces (RESOLUTIONS B4). Test: unit
  `test_boxcli_<cli>.py` golden files.
- **REQ-BOX-61** For each CLI, a docker test MUST show that the CLI starts offline in a
  hardened box and lists the `hal` MCP server, and a slow test MUST complete one prompt
  round trip against `FakeModelServer` with one MCP tool call through the bridge. Test:
  docker `test_box_live.py::test_<cli>_lists_hal_mcp_server`; docker+slow
  `test_box_live.py::test_<cli>_round_trip`.
- **REQ-BOX-62** `hal box` MUST take a checkpoint at start and at exit and print the diff
  stat and flags at exit. Test: unit `test_box.py::test_checkpoints_at_start_and_exit`.
- **REQ-BOX-78** For kinds `box` and `shell` with a TTY, on every backend, the box MUST NOT
  get the host terminal: the host MUST run the box on a pty it owns and relay the box's
  output to the user's terminal through the filter in "Terminal filter". The filter MUST pass
  printable text, `\t`, `\n`, `\r`, `\b`, CSI sequences (except the window-title reports
  `CSI 20 t` and `CSI 21 t`), two-byte and charset ESC sequences, and OSC 0 and OSC 2 (title
  text through `halkit.termsafe.strip_terminal_controls`); it MUST drop OSC 52, OSC 8, every
  other OSC, DCS, APC, PM, SOS, C1 controls and every other C0 control. `[box].raw_terminal =
  true` MUST turn the filter off and print a warning at every start. This closes the
  terminal as a third way out of the box besides the two sockets (01-security TB2). Test:
  unit `test_ptyfilter.py::test_box_pty_filter_drops_osc52_and_dcs`,
  `test_ptyfilter.py::test_sequences_split_across_reads`; docker
  `test_box_live.py::test_osc52_from_box_never_reaches_terminal` (a box writes OSC 52 and a
  DCS; the bytes read from the host side of the relay contain neither).
- **REQ-BOX-79** Every `hal box` and `hal-sandbox` output that writes to a TTY a string that
  comes from the box, the model or the work tree (file names and patch text in `diff`, `undo`
  and `ls`, the `hal box` exit summary, a CLI's stderr tail, the masking notice) MUST pass it
  through `halkit.termsafe.strip_terminal_controls`, and `--json` output MUST use
  `json.dumps(..., ensure_ascii=True)`. This mirrors 06-core-cli's rule for the `hal`
  commands it owns. Test: unit `test_cli.py::test_box_strings_are_stripped_before_tty` (one
  OSC 52 case per command).

### Doctor and latency

- **REQ-BOX-63** `hal-sandbox doctor [--json]` (and the sandbox section of `hal doctor`, 06)
  MUST report backend, runtime, runsc probe (and whether `/etc/docker/daemon.json` registers
  runsc with `--host-uds=open`), image status and sizes, AppArmor and seccomp state, daemon
  mode (rootful, userns-remap, rootless), the docker-group notice, the bwrap probe,
  user-manager availability and socket path length, each failure with its fix. Test: unit
  `test_doctor.py` (fake binaries).
- **REQ-BOX-64** On this machine class (desktop, runc or runsc, image cached) a box SHOULD
  start in at most 600 ms p50, `hal code` SHOULD answer ACP `initialize` within 1.5 s p50 of
  launch, and a bwrap box SHOULD start in at most 50 ms p50. Test: docker+slow
  `scripts/bench-sandbox.py` (manual record in `docs/dev/sandbox.md`).

### Peer checks, broker upstreams and result metadata

- **REQ-BOX-65** The broker transport MUST close a connection whose `SO_PEERCRED` uid differs
  from the session's `PeerRule.uid`, and SHOULD close a runc connection whose peer
  `/proc/<pid>/cgroup` does not contain the session's container id (REQ-SEC-64). Test: unit
  `test_transport.py::test_wrong_peer_uid_is_refused` (injected credentials); docker, runtime
  `test_docker_live.py::test_host_process_cannot_pose_as_box` (a host process with the right
  uid but the wrong cgroup is refused on runc; on runsc the probe-recorded uid is enforced).
- **REQ-BOX-66** Every enabled 03-agent `[mcp.servers.<name>]` entry with `host = true` MUST be
  started by the broker as a host stdio MCP client with that entry's `command`, `args`, `env`
  and `timeout_s`; its tools MUST be offered to the box as `<name>__<tool>` under the
  pseudo-pack `mcp-<name>`, which 01-security REQ-SEC-88 gives tier `act`,
  `untrusted_output = true` and `reviewable = false` unless `policy.toml` says otherwise;
  annotations from the upstream MUST NOT change any of these; and a failed upstream MUST yield
  error results without stopping the broker (RESOLUTIONS E6). Test: unit `test_upstreams.py`
  (a stub upstream script speaking MCP on stdio).
- **REQ-BOX-67** Every broker `tools/call` result MUST carry `result._meta.hal` exactly as
  defined in "Broker result metadata": `status`, `untrusted` and `guard`, and no approval id
  (RESOLUTIONS E7, B5). Test: unit `test_broker_meta.py::test_meta_shape_per_status`.
- **REQ-BOX-77** The broker MUST forward an elicitation from a `host = true` upstream to the
  session's `Approver` only when the elicitation's `_meta` carries `hal/approval_id`
  (`apr_` + 16 hex) and `hal/key` (`sha256:` + 64 hex), as 04-hub's elicitation surface sends
  them, and only while a `tools/call` from this session to that upstream is in flight. It
  MUST build the `ApprovalRequest` (01-security) as in "Upstream elicitations": pack
  `mcp-<name>`, `display` from the upstream message through `halkit.termsafe.escape`, scopes
  `(ONCE,)` only, `content = True`, and a key that binds the upstream's id and key; it MUST
  answer the upstream with that same `hal/approval_id` and `hal/key`. Every other
  elicitation (missing or malformed `_meta`, no call in flight, URL mode) MUST be declined
  without being shown and MUST be audited through 01-security's `EventSink`. No elicitation
  ever reaches the box (REQ-BOX-31). Test: unit
  `test_upstreams.py::test_upstream_elicitation_mapping`,
  `test_upstreams.py::test_elicitation_without_hal_meta_is_declined_and_audited`.

## Design

### Overview

```text
host (trusted)                                               box (untrusted, --network none)
-------------------------------------------------            -----------------------------------
hal (halcore) -- ACP JSON-RPC over docker stdio ------------> hal-box-entry -> hal-agent --acp
  Session (halsandbox)                                             |  coding tools on /work
    $R/s/<sid>/run/  ==== bind mount, read-only ====> /run/hal/    |
      broker.sock   <-- LineServer + Broker (halsandbox) <-- agent LiteMcpClient, or hal-mcp-bridge
      model.sock    <-- SessionForwarder (halgateway, started by halcore) <-- agent HTTP, or hal-relay
      lifeline.sock <-- Lifeline (halsandbox.lifeline) <-- hal-box-entry (holds it open)
      session.json, agent.json (rendered by halcore, 06), user/, skills/, home-seed/
    $R/s/<sid>/etc/{passwd,group} == ro bind ==> /etc/passwd, /etc/group
    $R/s/<sid>/empty == ro bind ==> /work/.env and every other masked file
    <work tree>  ==== bind rw ====> /work ; <work>/.git == bind ro ==> /work/.git
    Checkpoints (host git, temp index) -> refs/hal/checkpoints/<sid>/<n>

prep box (kind deps, `hal code --deps` only): no broker.sock, no model.sock
      egress.sock   <-- ConnectProxy (halsandbox.egress) <-- hal-relay on 127.0.0.1:3128 <-- uv, npm, cargo
```

For `hal box`, `hal-box-entry` starts `hal-relay` on `127.0.0.1:8741` inside the box and then
the CLI; the CLI reaches the model through the relay and host tools through
`hal-mcp-bridge`. For `hal code`, the agent talks HTTP to `/run/hal/model.sock` and MCP to
`/run/hal/broker.sock` directly with its stdlib clients (03-agent `LiteMcpClient`); both use
the same framing, so the broker sees one protocol from every client (Decision D9).

halsandbox may import only `halkit` and `halhub` (00-overview "Allowed imports"). It never
imports `halgateway` or `halagent`: halcore starts the model forwarder on the path the session
gives it, and the image installs `halagent` from its wheel. For the same reason 03-agent's
eval `BoxRunner` is implemented in halcore on top of `Session`, not in halsandbox, because its
signature uses halagent types.

Assistant channels (RESOLUTIONS A1, 00-overview Flow 2): `hal chat`, voice and Telegram run
the agent in a long-lived session of kind `chat` per channel, with `--toolset assistant` and
`/work` bound to the per-channel scratch directory `$STATE/assistant/<channel>/work`, never
the vault. `Session.start(kind="chat")` refuses any `project` outside
`$STATE/assistant/`. With no container backend, halcore runs the agent in its own process
(06-core-cli), and that agent uses the broker socket of a `backend="none"` session, so policy,
approvals and audit are identical (REQ-BOX-76).

### Package layout

```text
packages/hal-sandbox/
  pyproject.toml            deps: hal-kit, hal-hub, mcp>=2.3,<3; console scripts below
  images/
    Dockerfile.sandbox.in   template for hal-sandbox:<tag>
    Dockerfile.box.in       template for hal-box-<cli>:<tag>
    pins.toml               base digests, CLI versions with publish dates, goose sha256
    requirements-box.txt    hash-pinned third-party deps of hal-agent (uv export)
    requirements-tools.txt  hash-pinned `uv` wheel for dependency prep
    box/<cli>/package.json, package-lock.json   one npm lockfile per CLI (not goose)
    box/<cli>/home-seed/    image-level home files (may be empty)
  src/halsandbox/
    __init__.py
    config.py               [sandbox] tables registered with halkit.config
    session.py              Session, RuntimeDir, new_session_id
    box.py                  run(), harnesses() for hal box
    ptyfilter.py            TerminalFilter, relay_tty() for box and shell TTYs
    broker/                 Broker (policy, approvals, audit: 01-security); upstreams.py
                            (03-agent host servers), meta.py (result._meta.hal)
    detect.py               detect(), Probe, probe cache
    backends/base.py        Backend protocol, LaunchSpec, Limits, GitMount
    backends/docker.py      DockerBackend, docker_argv()
    backends/bwrap.py       BwrapBackend, bwrap_argv()
    backends/none.py        NoneBackend
    env.py                  box_env(), SECRET_NAME_RE
    gitlayout.py            git metadata discovery for mounts, sanitize_config()
    labels.py               labels(), sweep()
    transport.py            LineServer, LineConnection, anyio adapter
    lifeline.py             Lifeline
    paths.py                box_to_host(), WorkspaceEscape (opens through halkit.fsafe)
    mask.py                 find_masked(), mask mounts
    projects.py             per-project choices in $STATE/sandbox/projects.json
    deps.py                 lockfile detection, prepare(), the prep box
    egress.py               ConnectProxy for the prep box
    gitops.py               GitRunner, HARDEN_ARGS
    checkpoints.py          Checkpoints, Checkpoint, RestoreResult
    diffreport.py           DiffReport, FileChange, Flag, flag rules
    shadow.py               shadow repositories for non-git trees
    isolated.py             IsolatedWorkspace, AcceptResult
    images.py               build(), status(), ImageInfo, wheel discovery
    doctor.py               doctor()
    events.py               event()
    boxcli/base.py          BoxContext, BoxLaunch, CliProfile, registry
    boxcli/{claude,qwen,opencode,codex,crush,goose}.py
    boxside/hal_box_entry.py   stdlib; also copied to /opt/hal/bin/hal-box-entry
    boxside/hal_relay.py       stdlib; /opt/hal/bin/hal-relay
    boxside/hal_mcp_bridge.py  stdlib; /opt/hal/bin/hal-mcp-bridge
    cli.py                  box_main(), sandbox_main() used by halcore and `hal-sandbox`
  tests/
```

Console scripts: `hal-sandbox` (host; `build`, `status`, `doctor`, `gc`, `ls`, `shell`,
`undo`, `diff`, `deps`, `forget`, so the package works without halcore), `hal-mcp-bridge`,
`hal-relay`, `hal-box-entry`. In the image the three box-side modules are copied as single
files with the shebang `#!/usr/bin/python3 -I`, so the image does not need halsandbox
installed.

### Session runtime directory

```text
$R/s/<sid>/                 0700, created by halkit.paths.ensure_private_dir
  owner.json                {"pid": 4242, "pidstart": 9123456, "uid": 1000, "kind": "code",
                             "backend": "docker", "container": "hal-code-0123456789ab",
                             "work": "/home/u/src/proj", "created": "2026-10-23T18:05:01Z"}
  etc/passwd                0644, generated
  etc/group                 0644, generated
  etc/gitconfig             0644, sanitized copy of the repository config, only when needed (REQ-BOX-15)
  etc/gitconfig.worktree    0644, sanitized copy of config.worktree, only when needed
  empty                     0444, zero bytes; bound read-only over each masked file
  cid                       container id written by `docker run --cidfile`
  run/                      0700 -> /run/hal (read-only bind)
    broker.sock             0600, LineServer serving the Broker (not in prep sessions)
    model.sock              0600, SessionForwarder (halgateway, started by halcore; not in prep sessions)
    egress.sock             0600, ConnectProxy (prep sessions only)
    lifeline.sock           0600, Lifeline
    session.json            0644, sandbox capabilities, no secrets
    agent.json              0644, agent settings (03-agent schema), rendered by halcore (06) and written through run_files
    user/AGENTS.md          user-wide instructions, rendered by halcore (06), schema 03-agent
    skills/                 user skills, copied by halcore (06), schema 03-agent
    home-seed/              files copied into /home/hal at box start (CLI configs)
```

`lifeline.sock` is not the approval control socket. That socket is 01-security's
(`halhub.approvals`, RESOLUTIONS B3) at `runtime_dir()/control/<sid>.sock`, outside the
session directory, so the box cannot reach it.

- `<sid>` is 12 lowercase hex characters from `secrets.token_hex(6)` (00-overview fixes this),
  so `/run/user/1000/hal/s/0123456789ab/run/broker.sock` is 50 bytes. `RuntimeDir.create`
  calls `check_socket_path` on every socket and fails before anything starts. The same id is
  the checkpoint ref component, the container name suffix, the ACP `sessionId` and the
  transcript directory `state_dir()/sessions/<sid>/` (RESOLUTIONS E2/E3; the record schema is
  03-agent's).
- The directory lives under `$XDG_RUNTIME_DIR` because a unix socket path is limited to 107
  bytes: binding a socket under a deep scratch directory failed with `OSError: AF_UNIX path
  too long` [verified: `echo_server.py` experiment].
- `owner.json` lets the sweep decide liveness: `pidstart` is field 22 of `/proc/<pid>/stat`,
  so a reused pid does not keep a dead session alive. Its schema (the keys in the example,
  all required) is a public interface: 01-security's active-workspace exclusion reads
  `$R/s/*/owner.json` and treats a session as active when `pid` is alive with the same
  `pidstart`, so it sees workspaces of other `hal` processes too.
- `session.json` schema (version 1), every field:

```json
{
  "schema": 1,
  "session_id": "0123456789ab",
  "kind": "code",
  "created": "2026-10-23T18:05:01Z",
  "sandbox": {
    "backend": "docker",
    "runtime": "runc",
    "isolation": "container",
    "run": true,
    "network": false,
    "paths": "box",
    "limits": {"memory_bytes": 8589934592, "cpus": 4.0, "pids": 1024},
    "isolated": false,
    "checkpoints": "git",
    "masked": [".env", "deploy/server.key"],
    "masked_tracked": [],
    "git_config_sanitized": false
  },
  "sockets": {"broker": "/run/hal/broker.sock", "model": "/run/hal/model.sock",
              "lifeline": "/run/hal/lifeline.sock"},
  "relay": {"enabled": false, "port": 8741},
  "workdir": "/work"
}
```

  `isolation` is `container`, `namespace` (bwrap) or `none`; `paths` is `box` (tools act
  freely inside the box) or `confined` (backend `none`); `checkpoints` is `git`, `shadow` or
  `off`; `masked` lists work-tree paths that read as empty files (REQ-BOX-69), so the agent
  can tell the model why; `masked_tracked` is the subset that git tracks, whose history the
  box can still read; `git_config_sanitized` is true when the box sees a sanitized copy of the
  repository config (REQ-BOX-15). The agent reads its own settings, including the broker URL, from
  `agent.json` (03-agent 3.16.3), which halcore renders (06-core-cli, RESOLUTIONS E4/E5) and
  passes to the session as a run file; halsandbox never parses it.
  With backend `none` the socket paths are host paths under `$R/s/<sid>/run/`.

### Backends and detection

```python
BackendName = Literal["docker", "bwrap", "none"]
Runtime = Literal["runc", "runsc"]

@dataclass(frozen=True)
class Probe:
    backend: BackendName
    ok: bool
    runtime: Runtime | None
    daemon_mode: Literal["rootful", "userns-remap", "rootless"] | None
    detail: str                    # cause and fix when not ok
    checked: str                   # UTC Z

def detect(cfg: SandboxConfig, *, refresh: bool = False) -> Probe: ...
def backend_for(probe: Probe, cfg: SandboxConfig) -> Backend: ...
```

`auto` order and checks, each through `halkit.proc.run` with the timeout shown:

1. docker: the CLI named by `HAL_DOCKER` or `[sandbox].docker` exists;
   `docker version --format '{{.Server.Version}}'` answers within 5 s (fails on a stopped
   daemon or a user outside the `docker` group); `docker info --format '{{json .}}'` (5 s)
   gives `Runtimes`, `SecurityOptions` and the cgroup version.
   - Daemon mode: `name=rootless` in `SecurityOptions` means rootless; `name=userns` means
     userns-remap; otherwise rootful. This machine: rootful, `SecurityOptions`
     `[name=apparmor name=seccomp,profile=builtin name=cgroupns]`, cgroup v2 with the systemd
     driver [verified: `docker info`]; runtimes `runc`, `nvidia` (NVIDIA Container Toolkit
     1.20.1), `runsc` and `runsc-gpu` (gVisor release-20261019.0) [verified: brief "Host
     facts", 2026-10-23]. `/etc/docker/daemon.json` registers `runsc` with no `runtimeArgs`
     [verified: read on 2026-10-24]; the user is adding `--host-uds=open` (RESOLUTIONS G1).
     halsandbox never uses `nvidia` or `runsc-gpu`: boxes get no GPU (REQ-BOX-08).
   - Runtime: when `runsc` is listed and `[sandbox].runtime` is `auto` or `runsc`, run the
     runsc probe: start `docker run --rm --runtime runsc <hardened flags> <image>
     /opt/hal/bin/hal-box-entry --probe /run/hal/probe.sock` against a temporary runtime dir
     with a listening socket (10 s), and record the `SO_PEERCRED` uid the host side saw. A
     hardened runsc container (read-only, cap-drop ALL, no-new-privileges, `--user 1000:1000`)
     starts in 0.26 s, the same as runc, but cannot connect to a bind-mounted host unix socket
     unless runsc is registered with `--host-uds=open` [verified: brief "Host facts",
     2026-10-23], so a failed probe names that fix and the session runs on runc.
   - Fallback guard: every successful detection writes the backend it chose to
     `$STATE/sandbox/last-backend`. When that says `docker` and docker does not answer now,
     `auto` refuses to start (REQ-BOX-02): a stopped daemon must not silently drop the session
     to weaker isolation. An explicit `--backend` for one run leaves the file alone;
     `hal-sandbox doctor --refresh-probe` rewrites it from a fresh probe, which is how a user
     who removed docker on purpose moves on.
2. bwrap: the binary named by `HAL_BWRAP` or `[sandbox].bwrap` exists and the probe argv in
   "bwrap argv" with child `/usr/bin/true` exits 0 within 2 s. On Ubuntu with
   `kernel.apparmor_restrict_unprivileged_userns = 1`, the shipped profile
   `/etc/apparmor.d/bwrap-userns-restrict` lets `/usr/bin/bwrap` create the namespaces; the
   child runs under the `bwrap//&unpriv_bwrap` label [verified: probe argv run unprivileged,
   bubblewrap 0.11.1].
3. none: always available, but used only after the per-project consent of REQ-BOX-68.

Probe results are cached in `$STATE/sandbox/probe.json` (`{"key": "<docker version>|<sorted
runtimes>|<bwrap version>", "probe": {...}}`) for 24 h (REQ-BOX-06), because `docker info`
plus a runsc probe costs about a second and the answer changes only when the host changes.

### Docker argv

The builder `docker_argv(spec: LaunchSpec, daemon_mode) -> list[str]` emits exactly this, in
this order. `<U>:<G>` is the user's uid and gid; values in angle brackets come from config.

```text
docker run
  --rm                                   remove the container when it exits
  -i                                     stdin attached: ACP for code, keystrokes for box
  [-t]                                   only for kind box or shell when stdin is a TTY; the
                                         TTY is a host-owned pty behind the terminal filter
  --name hal-<kind>-<sid>
  --cidfile $R/s/<sid>/cid               the broker matches peer cgroups to this container
  --label hal.sandbox=1 --label hal.kind=<kind> --label hal.session=<sid>
  --label hal.uid=<U> --label hal.pid=<host pid> --label hal.pidstart=<ticks>
  --label hal.version=<halsandbox version> --label hal.started=<UTC Z>
  --pull never                           images come only from `hal box image build`
  --runtime <runc|runsc>
  --network none                         loopback only; no route out
  --read-only                            rootfs immutable: no dropped binaries, no persistence
  --cap-drop ALL                         no capabilities, even for uid 0 in rootless mode
  --security-opt no-new-privileges=true  setuid and file caps cannot raise privilege
  --user <U>:<G>                         files in /work keep the user's ownership
  [--userns host]                        only on a userns-remap daemon (see uid mapping)
  --init                                 docker-init as pid 1 reaps zombies, forwards signals
  --ipc private                          no shared memory with other containers
  --pids-limit <pids>                    fork bombs stop at the cap
  --memory <M> --memory-swap <M>         equal values forbid swap
  --cpus <C>                             leaves cores for llama.cpp CPU and hybrid presets
  --oom-score-adj 500                    the box dies before the desktop under memory pressure
  --ulimit core=0                        no core dumps (they would land in /work)
  --ulimit nofile=<n>:<n>
  --stop-timeout <stop_timeout_s>
  --log-driver none                      stdout is the ACP channel; json-file would copy the
                                         whole session into /var/lib/docker
  --hostname hal-box                     no host name leaks into prompts or files
  --tmpfs /tmp:rw,nosuid,nodev,size=<tmp_size>
  --tmpfs /home/hal:rw,nosuid,nodev,size=<home_size>,uid=<U>,gid=<G>,mode=0700
  --mount type=bind,"src=<work>","dst=/work"
  --mount type=bind,"src=<work>/.git","dst=/work/.git",readonly        (git layouts below)
  --mount type=bind,"src=$R/s/<sid>/run","dst=/run/hal",readonly
  --mount type=bind,"src=$R/s/<sid>/etc/passwd","dst=/etc/passwd",readonly
  --mount type=bind,"src=$R/s/<sid>/etc/group","dst=/etc/group",readonly
  [--mount type=bind,"src=$R/s/<sid>/empty","dst=/work/<masked path>",readonly]   one per masked file
  [--mount type=bind,"src=$R/s/<sid>/etc/gitconfig","dst=<box path of the git config>",readonly]
                                         only when the config holds credentials (REQ-BOX-15)
  [--mount type=bind,"src=<uv cache>","dst=/home/hal/.cache/uv",readonly]   opt-in only
  --workdir /work
  --env NAME=VALUE ...                   the allowlist in "Environment"
  --entrypoint /opt/hal/bin/hal-box-entry
  <image id>
  [--relay-port 8741] -- <child argv>
```

Notes on the flags:

- Not passed on purpose: `--security-opt seccomp=...` and `apparmor=...`. The daemon applies
  the builtin seccomp profile and `docker-default` AppArmor by default
  [verified: `Seccomp: 2` and `docker-default (enforce)` read inside a box]; `hal-sandbox
  doctor` fails when `docker info` does not list both. `--privileged`, devices, GPUs and
  extra capabilities are refused by the builder (REQ-BOX-08).
- `/tmp` keeps `exec`: build tools and test runners execute from it, and `/work` is
  executable anyway, so `noexec` would break tools without stopping anything.
- tmpfs pages count against the memory cgroup, so `tmp_size + home_size` must stay below the
  memory limit; config validation enforces it.
- Mount fields are written by `csv.writer(..., quoting=csv.QUOTE_ALL)` per field. A path with
  `,` and `:` mounted, and a path with `"` mounted once the quote was doubled; an unquoted
  `"` failed with `bare " in non-quoted-field` [verified: `docker run --mount` with such
  paths]. `-v` is never used because `:` in a path breaks it.
- Measured cost: `--network none` makes the start faster, not slower (no bridge or veth
  setup), see "Startup latency".
- `-i` without `--rm` would leave stopped containers; with `--rm` and a SIGKILLed `docker`
  client the container keeps running when its child does not read stdin (a `sleep`
  survived; a `cat` exited on EOF) [verified: `orphan.py`]. The lifeline closes that gap.

Verified as a whole [verified: `docker run` of `python:3.13-slim-trixie` with these flags, a
host echo server on a socket in the bound directory]: the socket answered; uid 1000; write to
`/work` succeeded and the file was owned by the user on the host; writes to `/work/.git`,
`/etc` and `/usr` failed with EROFS; `/home/hal` was 0700 owned by 1000; `CapEff` 0,
`NoNewPrivs` 1, `Seccomp` 2; outbound connect failed with `ENETUNREACH`; `/proc/net/dev`
listed only `lo`; a TCP listener on `127.0.0.1` inside the box accepted and round-tripped;
`getpwuid(1000)` failed until the generated passwd file was mounted; `unshare -U` inside the
box failed with EPERM, so nested sandboxes of third-party CLIs cannot run there.

#### Git layouts

`gitlayout.discover(work: Path) -> tuple[GitMount, ...]` reads `<work>/.git` without running
git:

| `<work>/.git` is | Mounts (all read-only) |
| --- | --- |
| a directory | `<work>/.git` at `/work/.git` |
| a gitfile `gitdir: <abs>` (linked worktree) | the gitfile at `/work/.git`; `<abs>` at `<abs>`; the common dir named by `<abs>/commondir` at its resolved absolute path |
| a gitfile `gitdir: <rel>` (submodule) | the gitfile; `<work>/<rel>` resolved on the host, mounted at `normpath("/work/" + rel)`; its common dir likewise |
| absent | nothing; checkpoints use a shadow repository |
| a symlink | resolved first, then as above; a target outside the user's home or `/tmp` is an error |

Mounting at the resolved absolute path keeps git inside the box working without rewriting the
user's gitfile. It exposes the repository's history read-only, which the plain layout exposes
too. In the box, `git status`, `git diff` and `git log` work, and `git add`, `git commit` and
`git stash` fail with `Unable to create '/work/.git/index.lock': Read-only file system`
[verified: box with a ro `.git` bind]. The agent gets history rewriting from nobody; the host
offers checkpoints instead. `GIT_OPTIONAL_LOCKS=0` in the box keeps `git status` from trying
to refresh the index.

#### Sanitized git config

A read-only `.git` still shows the repository config, and users put credentials there:
`remote.origin.url = https://user:token@host/r.git`, `http.<url>.extraHeader =
Authorization: Bearer ...`, or an inline `credential.helper = !f() { echo password=...; }; f`.
The box has no network, so it needs none of these.

```python
GIT_CONFIG_CREDENTIAL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^/@\s]+@", re.I)   # scheme://userinfo@

def sanitize_config(src: Path, dst: Path, git: GitRunner) -> tuple[str, ...] | None: ...
    # None when src holds no credentials (nothing written); else the removed keys, names only
```

- Files checked: the `config` of the common dir and, when present, `config.worktree` of the
  per-worktree git dir, at the paths "Git layouts" resolves. Each is read with `git config
  --file <path> --null --list` through `GitRunner` (no `--includes`, so an `include.path`
  pointing outside the work tree is not followed; its target is not mounted in the box
  anyway).
- A key is a credential when its name or value matches `GIT_CONFIG_CREDENTIAL_RE` with a
  password part (`user:secret@`) or on an `http` or `https` URL (a token is often the user
  name: `https://ghp_x@github.com`), when it is `http.extraheader` or
  `http.<url>.extraheader`, or when it is in a `credential` section. `ssh://git@host` and
  `git@host:path` have a user name and no secret and stay.
- The copy: `cp` to `$R/s/<sid>/etc/gitconfig` (or `gitconfig.worktree`), then `git config
  --file <copy>` with `--unset-all` for each `extraheader` key, `--remove-section` for each
  `credential` section and each section whose name carries userinfo (`[url
  "https://tok@host/"]`, `[http "https://tok@host"]`), and `--replace-all` for each value
  with userinfo, writing the URL without it. The copy is read again with the same rules;
  any remaining match stops the session with an error naming the file, because a half-clean
  copy would look safe.
- Both backends bind the copy read-only over the original's box path, after the git
  metadata binds (a file bind inside the read-only `.git` bind, as the mask binds sit inside
  `/work`) [unverified: proven by `test_git_config_token_not_readable_in_box` on runc and
  runsc]. The user's file is never changed, and host git calls (checkpoints, diff, restore)
  keep using it; none of them talks to a remote.
- The notice names the keys, never the values: "git config holds credentials
  (remote.origin.url, http.https://example.org/.extraheader); the box sees a copy without
  them". `session.json` sets `git_config_sanitized: true`.
- Not covered: credentials git itself writes elsewhere in `.git` (`FETCH_HEAD`, reflogs).
  Git strips userinfo from the URLs it records there [unverified: check `FETCH_HEAD` and
  `logs/` after a fetch from a URL with userinfo]; if it does not, those files join this
  list. A committed secret is the history case of "Secret masking".

### bwrap argv

```text
[systemd-run --user --scope --quiet --collect
   -p MemoryMax=<M> -p MemorySwapMax=0 -p TasksMax=<pids> -p CPUQuota=<C*100>% --]
bwrap
  --unshare-all --unshare-user           new user, pid, ipc, uts, cgroup and net namespaces
  --disable-userns --assert-userns-disabled   no nested user namespaces inside
  --die-with-parent                      the box dies with the hal process
  --new-session                          no TIOCSTI injection into the host terminal
  --cap-drop ALL
  --hostname hal-box
  --clearenv  --setenv NAME VALUE ...    the allowlist in "Environment"
  --ro-bind /usr /usr
  --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 --symlink usr/sbin /sbin
                                         (merged-/usr hosts; otherwise --ro-bind each)
  --ro-bind /etc/ld.so.cache /etc/ld.so.cache
  --ro-bind-try /etc/alternatives /etc/alternatives
  --ro-bind-try /etc/localtime /etc/localtime
  --ro-bind-try /etc/nsswitch.conf /etc/nsswitch.conf
  --ro-bind $R/s/<sid>/etc/passwd /etc/passwd
  --ro-bind $R/s/<sid>/etc/group /etc/group
  --ro-bind <sys.base_prefix> <same>     Hal's Python, when not under /usr
  --ro-bind <venv prefix> <same>         Hal's venv (hal-agent, hal-mcp-bridge)
  --proc /proc --dev /dev
  --size <tmp_bytes> --tmpfs /tmp
  --size <home_bytes> --perms 0700 --tmpfs /home/hal
  --bind <work> /work
  --ro-bind <git mounts as for docker>
  --ro-bind $R/s/<sid>/run /run/hal
  [--ro-bind $R/s/<sid>/empty /work/<masked path>]   one per masked file
  [--ro-bind $R/s/<sid>/etc/gitconfig <git config path>]   only when sanitized (REQ-BOX-15)
  --chdir /work
  -- <venv>/bin/hal-box-entry -- <child argv>
```

- No `/etc` bind beyond the listed files: the host's `/etc` holds the user list, network
  config and sometimes credentials readable by the user.
- No seccomp filter in v1: bwrap takes a compiled BPF program on an fd (`--seccomp FD`), and
  generating one needs libseccomp or a hand-written BPF assembler; the bwrap box therefore has
  the host's full syscall surface inside its namespaces (documented as weaker than docker).
- Verified unprivileged on this machine [verified: bwrap 0.11.1 runs]: the socket under the
  read-only `/run/hal` bind answered; uid 1000; `/work/.git`, `/etc` and `/usr` writes failed
  with EROFS; `CapEff` 0, `NoNewPrivs` 1, `Seccomp` 0; outbound connect failed with
  `ENETUNREACH`; `/home/me` did not exist inside; `--disable-userns` needs `--unshare-user`
  (an error without it), and with it `unshare -U` inside failed; `--size 67108864 --tmpfs`
  produced a 64 MiB tmpfs; the hostname was `hal-box`.
- `systemd-run --user --scope` is used when `systemctl --user is-system-running` answers
  `running` or `degraded` within 2 s [unverified: not run here because it creates a transient
  unit; check with `systemd-run --user --scope -p MemoryMax=64M true`].
- The bwrap backend serves `hal code`, `hal chat` and `hal -p` only. The agent runs from
  Hal's own venv; tools come from the host's `/usr`. ripgrep and fd are not installed on this
  host [verified: `which rg fd fdfind` printed nothing], so the agent's `grep` and `glob` must
  fall back to Python (03-agent). `hal box` needs CLIs and node in an image, so it is
  docker-only (Decision D7).

### Backend none

With backend `none`, `agent_argv` is `<venv>/bin/hal-agent --acp --config $R/s/<sid>/run/agent.json`
on the host with `cwd = <work>`, the
same environment allowlist (sockets as host paths, `HAL_SANDBOX=none`,
`HAL_WORKSPACE=<work>`) and `start_new_session=True`. `session.json` says `run: false` and
`paths: "confined"`; the agent then omits `run` and opens files through
`halkit.fsafe.open_beneath` (the one implementation, RESOLUTIONS C10; halagent imports halkit),
refusing the masked paths listed in `session.json`. The shared path vectors live with halkit
(`packages/hal-kit/tests/data/path_cases.json`). There is no network isolation: the agent
process is Hal's own code and has no tool that opens a socket, so the model reaches the
network only through the broker; the agent refuses user MCP servers not marked `host`
(03-agent, RESOLUTIONS F4), and there is no host `run` at all (RESOLUTIONS G4: no
`allow_host_run`). The session starts only after the one-time per-project consent of
REQ-BOX-68, and the UI shows a persistent banner (06). 01-security lists this mode as a
residual risk (security review R-new-8).

### uid and gid mapping

| Daemon mode | `--user` | Extra | Result on host |
| --- | --- | --- | --- |
| rootful (this machine) | `<U>:<G>` | none | box processes run as the user; files owned by the user [verified] |
| userns-remap | `<U>:<G>` | `--userns host` | remap would map uid 1000 to a subordinate uid and break ownership; a non-root, cap-less box gains nothing from remap [unverified: no remap daemon here] |
| rootless | `0:0` | none | container root is the host user; `--cap-drop ALL` still applies [unverified: no rootless daemon here] |
| bwrap | n/a | `--unshare-user` maps the user to itself | same uid inside and out [verified] |

The generated passwd file maps the in-box uid to `hal`:

```text
root:x:0:0:root:/root:/usr/sbin/nologin
hal:x:<U>:<G>:hal:/home/hal:/bin/bash
```

(on rootless the single line is `hal:x:0:0:hal:/home/hal:/bin/bash`). `/etc/group` has
`root:x:0:` and `hal:x:<G>:`. Without it, `getpwuid` fails and git, ssh-style tools and
node's `os.userInfo()` misbehave [verified: `getpwuid(): uid not found: 1000` before, `hal
/home/hal` after].

Members of the `docker` group are root-equivalent on the host, because they can start a
privileged container. Hal does not change that; `hal-sandbox doctor` says it once and points
to rootless Docker (residual risk, 01-security).

### Environment

```python
SECRET_NAME_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)
PASSTHROUGH = ("TERM", "COLORTERM", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ",
               "NO_COLOR", "FORCE_COLOR", "COLUMNS", "LINES")

def box_env(kind: str, sid: str, host_env: Mapping[str, str], cfg: SandboxConfig,
            extra: Mapping[str, str] = {}) -> dict[str, str]: ...
```

Fixed variables:

| Name | Value | Reason |
| --- | --- | --- |
| `HOME` | `/home/hal` | tmpfs home |
| `USER`, `LOGNAME` | `hal` | matches the generated passwd |
| `SHELL` | `/bin/bash` | |
| `PATH` | `/opt/hal/venv/bin:/opt/hal/bin:/usr/local/bin:/usr/bin:/bin` (box CLIs prepend their `node_modules/.bin`) | |
| `LANG` | `C.UTF-8` unless passed through | UTF-8 tool output |
| `TMPDIR` | `/tmp` | |
| `HAL_SESSION` | `<sid>` | |
| `HAL_SANDBOX` | `docker`, `bwrap` or `none` | |
| `HAL_BROKER_SOCKET` | `/run/hal/broker.sock` | |
| `HAL_MODEL_SOCKET` | `/run/hal/model.sock` | |
| `HAL_MODEL_URL` | `http://127.0.0.1:8741` (kinds `box`, `shell`) | relay |
| `GIT_OPTIONAL_LOCKS` | `0` | `.git` is read-only |
| `GIT_CONFIG_NOSYSTEM` | `1` | |
| `PIP_NO_INDEX`, `PIP_DISABLE_PIP_VERSION_CHECK` | `1` | no network; fail fast |
| `UV_OFFLINE` | `1` | same |
| `UV_PROJECT_ENVIRONMENT` | `/work/.hal/venv` | the environment `hal code --deps` built; never a host venv whose interpreter the box cannot see |
| `UV_PYTHON`, `UV_PYTHON_DOWNLOADS` | `/usr/bin/python3`, `never` | the image's Python, as in the prep box |
| `CARGO_HOME`, `CARGO_NET_OFFLINE` | `/work/.hal/cargo`, `true` | crates fetched by the prep box |
| `NPM_CONFIG_OFFLINE`, `NPM_CONFIG_UPDATE_NOTIFIER` | `true`, `false` | same |
| `NO_UPDATE_NOTIFIER`, `DO_NOT_TRACK` | `1` | quiet offline CLIs |
| `HAL_HARNESS` | `1` (kind `box` only) | 01-security REQ-SEC-26: the control socket refuses a `decide` from a harness, so `hal approve` cannot run from inside |

Pass-through: the `PASSTHROUGH` names when set on the host, then `[sandbox].env_passthrough`
names (each must match `^[A-Z_][A-Z0-9_]*$` and must not match `SECRET_NAME_RE`). Everything
else is dropped. Values are passed with `--env NAME=VALUE`; none is secret, so their
visibility in `docker inspect` is harmless.

### Resource limits

`[sandbox.limits]`, resolved by `limits.resolve(cfg) -> Limits`:

| Key | Default | Reason |
| --- | --- | --- |
| `memory` | `"auto"` = `min(8 GiB, MemTotal/4)` | 8 GiB fits a compile plus a test run; RAM/4 gives 4 GiB on a 16 GiB laptop, the value an earlier batch of SWE-task sandboxes ran with. This machine: 61 GiB, so 8 GiB |
| `cpus` | `0` = `min(4, nproc/2)` | llama.cpp CPU and hybrid presets need the remaining cores; this machine has 24, so 4 |
| `pids` | `1024` | stops fork bombs; parallel builds stay well below [unverified: measure `make -j4` plus pytest-xdist peak] |
| `nofile` | `8192` | node tooling opens many files |
| `tmp_size` | `"1g"` | counts against memory |
| `home_size` | `"512m"` | CLI caches and configs |
| `stop_timeout_s` | `10` | gives the agent time to flush its transcript |
| `oom_score_adj` | `500` | box dies first under host memory pressure |

```python
@dataclass(frozen=True)
class Limits:
    memory_bytes: int
    cpus: float
    pids: int
    nofile: int
    tmp_bytes: int
    home_bytes: int
    stop_timeout_s: int
    oom_score_adj: int
```

### Images

Two image families:

- `hal-sandbox:<version>-<flavor>-<ctx12>`: the agent box for `hal code`, `hal chat`,
  `hal -p`, the dependency prep box and `hal-sandbox shell`. `flavor` is `full` (default),
  `slim` (no C toolchain), or either with a `-node` suffix when `[sandbox].node = true`
  (node and npm copied from the pinned node image, as for the box images; +192 MB measured).
- `hal-box-<cli>:<cliversion>-<ctx12>`: built `FROM` the `full` sandbox image, adds node and
  one CLI. One image per CLI, built on demand (`hal box image build <cli>`), because all
  five npm CLIs in one image measured 2.79 GB.

`<ctx12>` is the first 12 hex characters of the sha256 over the rendered Dockerfile and every
file in the build context, so a changed pin or a new Hal version gives a new tag. The session
resolves the tag to an image ID with `docker image inspect --format '{{.Id}}'` (5 s) and runs
the ID (REQ-BOX-27).

Base: `debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad`
[verified: `docker pull` 2026-10-23, created 2026-10-20]. Debian's own `python3` (3.13.5)
is used rather than `python:3.13-slim`, so Python, git and the tools share one package source
and security updates (Decision D12). Node comes from
`node:22-trixie-slim@sha256:8eb619e402eaede1748a3c8173bffab391592812e85136452f781741c7242ddf`
(node v22.23.4) [verified: pulled 2026-10-23] by `COPY --from`, because claude-code and
qwen-code declare `engines.node >= 22` [verified: `npm view <pkg> engines.node`] and Debian
trixie ships an older node [unverified: `apt-cache policy nodejs` in the image].

`images/Dockerfile.sandbox.in` (rendered by `images.py`; `${...}` are filled in Python):

```dockerfile
# syntax=docker/dockerfile:1.7
FROM ${BASE_DIGEST}
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash coreutils findutils grep sed gawk diffutils patch file less procps \
      ca-certificates git ripgrep fd-find python3 python3-venv make \
      ${FULL_PACKAGES} ${EXTRA_APT} \
 && ln -s /usr/bin/fdfind /usr/local/bin/fd \
 && rm -rf /var/lib/apt/lists/*
COPY requirements-box.txt requirements-tools.txt /opt/hal/
COPY wheels/ /opt/hal/wheels/
RUN python3 -m venv /opt/hal/venv \
 && /opt/hal/venv/bin/pip install --no-cache-dir --no-deps --require-hashes \
      -r /opt/hal/requirements-box.txt -r /opt/hal/requirements-tools.txt \
 && /opt/hal/venv/bin/pip install --no-cache-dir --no-deps --no-index /opt/hal/wheels/*.whl \
 && rm -rf /opt/hal/wheels
COPY bin/ /opt/hal/bin/
RUN chmod 0755 /opt/hal/bin/* \
 && dpkg-query -W -f '${Package} ${Version}\n' > /opt/hal/packages.txt
LABEL org.opencontainers.image.title="hal-sandbox" hal.image.context="${CTX}" \
      hal.image.flavor="${FLAVOR}" hal.version="${VERSION}"
USER 65534:65534
WORKDIR /work
ENTRYPOINT ["/opt/hal/bin/hal-box-entry"]
```

`FULL_PACKAGES` is `build-essential pkg-config` for `full` and empty for `slim`. `EXTRA_APT`
is `[sandbox].extra_apt_packages`, each name checked against `^[a-z0-9][a-z0-9+.-]+$` (for
example `cargo` for Rust projects, which `hal code --deps` needs for `cargo fetch`). With
`[sandbox].node = true` the template adds the same `FROM ${NODE_DIGEST} AS node` stage and
`COPY --from=node` lines as `Dockerfile.box.in`. `uv` comes from `requirements-tools.txt`, a
hash-pinned wheel whose version is recorded in `pins.toml` under the 14-day rule
[unverified: the exact `uv` version is chosen at implementation; check with
`pip index versions uv` and the PyPI upload date].
`USER 65534` is a safe default for anyone running the image by hand; Hal always passes
`--user`.

Build context (a temp dir, removed after the build):

- `requirements-box.txt`: `uv export --package hal-agent --no-emit-workspace --format
  requirements-txt` at release time, with hashes; CI checks it matches `uv.lock`.
- `wheels/`: `hal_kit-<v>-py3-none-any.whl` and `hal_agent-<v>-py3-none-any.whl`. Found in
  `$DATA/wheels/` (written by `scripts/install.sh`), or built with
  `uv build --wheel --package hal-kit --package hal-agent -o <ctx>/wheels` when halsandbox
  runs from a workspace checkout (detected by a `uv.lock` above its source directory).
  Their versions must equal halsandbox's version (REQ-SYS-18), else the build fails.
- `requirements-tools.txt`: the `uv` wheel with its sha256, committed with the pins.
- `bin/`: the three box-side modules, copied with the `#!/usr/bin/python3 -I` shebang.

`images/Dockerfile.box.in`:

```dockerfile
# syntax=docker/dockerfile:1.7
FROM ${NODE_DIGEST} AS node
FROM ${SANDBOX_IMAGE_ID}
USER 0:0
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
COPY box/${CLI}/ /opt/hal/box/${CLI}/
RUN cd /opt/hal/box/${CLI} \
 && npm ci --omit=dev --no-audit --no-fund \
 && npm cache clean --force
ENV PATH=/opt/hal/box/${CLI}/node_modules/.bin:${PATH}
USER 65534:65534
```

For goose (no npm package) the `npm` lines are replaced by
`ADD --checksum=sha256:${GOOSE_SHA256} ${GOOSE_URL} /tmp/goose.tar.gz` and an extract to
`/opt/hal/box/goose/bin/` (no node stage). npm install scripts run, because the CLIs select
their native binaries in them; they run inside the build container, which has no host mounts.

`images/pins.toml` (every field):

```toml
[base]
debian = "debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad"
node = "node:22-trixie-slim@sha256:8eb619e402eaede1748a3c8173bffab391592812e85136452f781741c7242ddf"

[tools.uv]
version = ""       # filled at implementation (>= 14 days old); the build refuses an empty value
published = ""

[cli.claude]
package = "@anthropic-ai/claude-code"
version = "2.1.295"
published = "2026-10-08"

[cli.qwen]
package = "@qwen-code/qwen-code"
version = "0.25.0"
published = "2026-10-05"

[cli.opencode]
package = "opencode-ai"
version = "1.18.35"
published = "2026-10-06"

[cli.codex]
package = "@openai/codex"
version = "0.162.0"
published = "2026-10-08"

[cli.crush]
package = "@charmland/crush"
version = "0.98.0"
published = "2026-10-08"

[cli.goose]
url = "https://github.com/aaif-goose/goose/releases/download/v1.54.0/goose-x86_64-unknown-linux-gnu.tar.gz"
version = "1.54.0"
published = "2026-10-08"
sha256 = ""   # filled at implementation from the release asset; the build refuses an empty value
```

Each npm version is the newest release at least 14 days old on 2026-10-23 [verified: `npm
view <pkg> time --json`; latest were claude-code 2.1.309, qwen-code 0.26.0, opencode-ai
1.19.4, codex 0.164.0, crush 0.98.2, all 1 to 2 days old]. The goose tag and asset name are
from the GitHub releases API [verified: `api.github.com/repos/aaif-goose/goose/releases`];
its sha256 is [unverified]. A 14-day age gives the npm registry and the projects time to pull
a compromised release before Hal pins it.

Measured sizes [verified: test builds from the pinned digests, `docker images` SIZE and
`docker image inspect .Size`]:

| Image | Disk (`docker images`) | Compressed |
| --- | --- | --- |
| `debian:trixie-slim` | 119 MB | 29.9 MB |
| + base tools (slim flavor without Hal wheels) | 346 MB | 85.3 MB |
| + `build-essential pkg-config` (full flavor) | 706 MB | 179.2 MB |
| + node 22 | 898 MB | 227.3 MB |
| + all five npm CLIs in one image | 2.79 GB | 739.2 MB |

Per-CLI `node_modules` sizes in that image: crush 126 MB, qwen-code 153 MB, claude-code
245 MB, opencode 355 MB, codex 432 MB. The `mcp` 2.3.0 dependency tree is 37 MB installed
[verified: `uv pip install mcp==2.3.0` into a scratch venv]. Budgets (REQ-BOX-26): slim
450 MB, full 850 MB, each box image 1.5 GB. Tool versions in the image: Python 3.13.5, git
2.47.3, ripgrep 14.1.1, fd 10.2.0, bash 5.2.37 [verified: `--version` in the test image].

`hal box image build`:

```text
hal box image build [NAME] [--no-cache] [--json]   NAME: sandbox (default), sandbox-slim, a CLI, all
hal box image status [--json]
```

- Runs `docker build --network default --pull=false -t <tag> -f - <ctx>` through
  `halkit.proc.run` with a 1800 s timeout; base images are pulled by digest when missing
  (`docker pull <digest>`, 600 s). Network use is limited to this command (REQ-BOX-22).
- After each build it runs `hal-box-entry --selftest` in a hardened box (REQ-BOX-25) and
  records `{tag, id, size, built, ctx}` in `$STATE/sandbox/images.json`.
- Old tags of the same family are removed after a successful build (`docker rmi`, never
  forced, never another family).
- Custom images: `[sandbox].image = "<ref>@sha256:..."` runs that image instead; it must be
  built `FROM` a `hal-sandbox` image (checked through the `hal.image.context` label), or the
  session refuses it.

### Broker socket transport

The broker (host, halsandbox, policy in 01-security) serves MCP on `run/broker.sock`. The
transport carries the MCP stdio framing over the socket, as the MCP spec allows for custom
byte-stream transports [source: understand/research-interop.md section 1]:

- each message is one JSON-RPC object, UTF-8, terminated by `\n`, with no embedded newline;
- one connection is one MCP client: `hal-mcp-bridge` opens one connection per process, and
  the broker keys client state by `(sid, conn_id)`; there is no multiplexing;
- at most `max_message_bytes` (4 MiB, the request cap of the `mcp` 2.x SDK [source:
  understand/research-interop.md section 1]) per message, both directions;
- a partial message older than 30 s is a slowloris: error frame, close;
- at most `max_connections` (8) concurrent connections per session.

Example exchange (the protocol revision comes from the client):

```text
box -> host  {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}\n
host -> box  {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"web_read","description":"...","inputSchema":{...}}],"ttlMs":60000}}\n
box -> host  <4 MiB + 1 bytes without a newline>
host -> box  {"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"message exceeds 4194304 bytes"}}\n   then close
```

```python
class MessageTooLarge(Exception): ...

class LineConnection:
    """One accepted client; reads and writes whole newline-framed messages."""
    conn_id: int
    peer: tuple[int, int, int]                       # SO_PEERCRED (pid, uid, gid), logged only
    def read_message(self) -> bytes | None: ...      # without the newline; None at EOF
    def write_message(self, data: bytes) -> None: ...   # adds the newline; serialised by a lock
    def close(self) -> None: ...

@dataclass(frozen=True)
class PeerRule:
    uid: int                                         # expected peer uid
    container_id: str | None = None                  # runc: must appear in /proc/<pid>/cgroup

class LineServer:
    """Accepts clients on a unix socket and runs `handler` per connection in a thread."""
    def __init__(self, path: Path, handler: Callable[[LineConnection], None], *,
                 peer: PeerRule, max_message_bytes: int = 4 * 2**20,
                 max_connections: int = 8, partial_timeout_s: float = 30.0) -> None: ...
    def start(self) -> None: ...                     # bind, chmod 0600, listen, accept thread
    def close(self, timeout_s: float = 5.0) -> None: ...   # stop accepting, close all, unlink

def anyio_streams(conn: LineConnection) -> tuple[AsyncLineReader, AsyncLineWriter]: ...
```

`anyio_streams` adapts a connection to the text streams that 04-hub's
`halhub.mcp.serve_streams(hub, read_stream, write_stream, options)` and the SDK's
`mcp.server.stdio.stdio_server(stdin=..., stdout=...)` accept, so the broker runs the
official SDK server per connection while the size cap stays in the transport
[verified: `inspect.signature(stdio_server)` in mcp 2.3.0 is
`(stdin: anyio.AsyncFile[str] | None = None, stdout: anyio.AsyncFile[str] | None = None)`;
explicit streams skip its fd claim]. The broker passes `ServeOptions(approvals="callback",
approver=...)` (04-hub "Entry points and options"), where the approver is the session's
01-security `halhub.approvals.Approver` (RESOLUTIONS B1). Mode `callback` never sends an
elicitation to the client (04-hub REQ-HUB-42), which is what REQ-BOX-31 needs: an in-box
client could otherwise answer its own approval (RESOLUTIONS B4).

Access control, two layers (REQ-SEC-64):

- The 0700 session directory: only the user's processes and the box reach the socket.
- `SO_PEERCRED` on every accepted connection: the peer uid must equal `PeerRule.uid`, else the
  connection is closed and logged. With docker on runc, the peer pid's `/proc/<pid>/cgroup`
  must also name the session's container id (SHOULD in 01-security; done when the container
  id is known, which is after `docker run` prints it through `--cidfile`, a file under
  `$R/s/<sid>/`). `PeerRule.uid` is the user's uid for runc, bwrap, none and rootless docker
  (container root is the user). For runsc the connection arrives through gVisor's host-UDS
  support, and the peer may be a gVisor process rather than the box process, with a uid that
  may differ [unverified: needs runsc registered with `--host-uds=open`, which this host does
  not have yet; check with the runsc probe, which logs the peer uid and pid]. The probe
  records the uid it saw, and that uid becomes `PeerRule.uid` for runsc sessions; the cgroup
  check is skipped on runsc because the peer pid is not the box process (Decision D10).

### Broker upstreams

Host-side MCP servers that the box may use through the broker. There is one user-facing
table, 03-agent's `[mcp.servers.<name>]` (03-agent 3.13.1, RESOLUTIONS E6); this spec owns
only what the broker does with entries marked `host = true`. Examples are a user's mail MCP
server and the desktop hub reached over SSH from a laptop, which 07-remote's setup writes as
an ordinary entry (07-remote REQ-RMT-45):

```toml
[mcp.servers.hal-desktop]
host = true
command = "/usr/bin/ssh"
args = ["-F", "/home/me/.config/hal/remote/desktop/ssh_config", "-T", "hal-desktop-mcp"]
```

What the broker reads from such an entry (every key is 03-agent's; the broker adds none):

| 03-agent key | Broker use |
| --- | --- |
| `host = true` | The entry becomes an upstream; entries without it never run on the host |
| `enabled` | `false`: not started |
| `command`, `args` | Run as an argv list, never through a shell (03-agent rejects `url` entries in v1) |
| `env` | Added to the base environment below |
| `cwd` | Host working directory; empty means the user's home |
| `timeout_s` | Per-call timeout |
| `tools_allow`, `tools_deny` | Applied before tools are offered |
| `[mcp].restart_limit` | Restarts per upstream per session |

Behaviour: the broker starts an upstream on the first `tools/list` that needs it, with the
`mcp` SDK's stdio client and a base environment of `PATH`, `HOME`, `LANG` and
`SSH_AUTH_SOCK` only (an SSH upstream needs the user's agent; the box never sees this
environment). Startup, meaning process start plus `initialize` plus `tools/list`, has a fixed
limit `UPSTREAM_STARTUP_TIMEOUT_S = 20` (an SSH connection plus the far side's
`tools/list`), and the broker restarts an upstream at most `[mcp].restart_limit` times per
session (03-agent default 1). Its tools
appear to the box as `<name>__<tool>` in the pseudo-pack `mcp-<name>`, and 01-security
REQ-SEC-88 decides their metadata: tier `act`, `untrusted_output = true`,
`reviewable = false`, unless `policy.toml` names the tool. Upstream annotations are never a
security input. Calls go through the same `decide`, approval and audit path as hub tools.
Elicitations from an upstream (approvals on the far side) go to the session's host `approver`
only as described in "Upstream elicitations" (REQ-BOX-77); the box can never answer them. The
broker registers upstream tools with the hub registry
through the external-provider hook listed in "Interfaces used from other specs", so
`hal_find_tools` and policy see one registry.

#### Upstream elicitations

The case is a laptop session calling a desktop tool through the `hal-desktop` upstream
(07-remote REQ-RMT-45): the desktop hub decides `ask` and, having no surface of its own on
the laptop, sends an MCP elicitation through 04-hub's elicitation surface, with `_meta`
`{"hal/approval_id": "apr_...", "hal/key": "sha256:..."}` and the schema whose enum is
`["once", "deny"]` (04-hub "Approvals over MCP"; the message is the desktop's host-rendered
`display`). The broker's stdio client declares the `elicitation` capability with `form` only,
so the far side may use this surface, receives the request in its elicitation callback and
handles it like this:

1. Check: the upstream has a `tools/call` from this session in flight (the broker tracks its
   own requests per upstream), the request is form mode, `hal/approval_id` matches
   `^apr_[0-9a-f]{16}$` and `hal/key` matches `^sha256:[0-9a-f]{64}$`. Otherwise answer
   `{"action": "decline"}`, write an audit event through 01-security's `EventSink`
   (upstream name, reason, the in-flight tool if any; the message is never logged raw), and
   stop. Nothing is shown to the user.
2. Build the local `ApprovalRequest` (fields as 01-security "The approvals module
   (`halhub.approvals`)" defines them): a new local `id`; `pack = "mcp-<name>"`; `tool = "<name>__<tool>"` of the call in
   flight; `tier = act`; `args = {"upstream_approval_id": <id>, "upstream_key": <key>,
   "upstream_tool": "<tool>", "message": <message>}`; `key` computed by 01-security's key
   function over that pack, tool and `args`, so the local answer is bound to exactly that
   upstream id and key and can never match a grant made for anything else; `display` = the
   upstream id, the upstream tool and `halkit.termsafe.escape(message, multiline=True)`;
   `summary` = "Approve a request from host MCP server <name>" (host-made, no upstream text);
   `content = True`, so the request goes only to surfaces with `shows_all_args`
   (RESOLUTIONS "Surfaces"); `scopes = (ONCE,)`, because the far side accepts `once` only;
   `reason = "upstream elicitation"`.
3. Ask the session's `Approver`; the request takes the same timeout as any other.
4. Answer the upstream: on `allow`, `{"action": "accept", "content": {"decision": "once"},
   "_meta": {"hal/approval_id": <id>, "hal/key": <key>}}`; on deny, timeout or error,
   `{"action": "decline", "_meta": {...the same two names}}`. The far side's router checks
   the id and key against its own pending request (04-hub, 01-security REQ-SEC-23).

The user sees two prompts for one remote call when the local policy also says `ask` for the
upstream tool: the laptop's decision to call the desktop, and the desktop's decision to run
the tool. They answer different questions, so neither replaces the other.

### Broker result metadata

Every `tools/call` result the broker returns carries Hal outcome data in `result._meta.hal`
(RESOLUTIONS E7). This spec owns the shape; 03-agent reads it.

```json
{"jsonrpc":"2.0","id":2,"result":{
  "content":[{"type":"text","text":"<the 01-security untrusted envelope around the page text>"}],
  "isError":false,
  "_meta":{"hal":{"status":"ok","untrusted":true,"guard":"clean"}}}}
```

| Field | Type | Values |
| --- | --- | --- |
| `status` | string | `ok` (the tool ran and succeeded); `error` (the tool ran and failed, or timed out); `denied` (policy said `deny`, or the approval was denied or timed out; the text names the rule); `unavailable` (unknown tool, upstream down, broker shutting down) |
| `untrusted` | bool | `true` when the tool is `untrusted_output` (for upstreams always, REQ-SEC-88) |
| `guard` | string or null | null when `untrusted` is false; else the `guard` value of 01-security's untrusted result envelope: `clean`, `flagged`, `error` or `off` |

`isError` is `false` only for `ok`. There is no approval id and no `approval_required`
status: an `ask` decision blocks the call until a host surface answers or the request times
out, so the agent never sees a pending approval (RESOLUTIONS B5). The text content is passed
through unchanged: untrusted results already carry 01-security's envelope, which 04-hub emits
(RESOLUTIONS B11), and the broker adds no second wrapping. `halsandbox.broker.meta` builds
the object; `test_broker_meta.py` checks one case per status.

### Model socket and forwarder

The model socket is served by `halgateway.SessionForwarder` (05-gateway REQ-GW-63). halcore
starts it, because halsandbox may not import halgateway (RESOLUTIONS A5):

1. `Session.start(...)` returns `model_socket` = `$R/s/<sid>/run/model.sock`; nothing listens
   there yet.
2. halcore calls `GatewayClient.create_session(label="hal <kind> <work>")` (REQ-GW-60); the
   token stays in host process memory.
3. halcore starts `SessionForwarder(listen_path=session.model_socket, ...)` with the
   arguments 05-gateway's "Python API" defines: the local `gateway.sock` and the session
   token, or, in client mode on a laptop, `[gateway].url` and a client token (RESOLUTIONS
   D6). Only then does it spawn `agent_argv`.
4. At teardown halcore closes the forwarder after `session.close()` and calls
   `GatewayClient.revoke_session(id)`.

`hal box` follows the same order through the `start_model` callable it receives
(`halsandbox.box.run`). The forwarder's route allowlist is 01-security's (REQ-SEC-66,
RESOLUTIONS D3) and 05-gateway implements it, with credential stripping, the size cap (8 MiB
default, `[gateway.sessions]`), the concurrency cap and timeouts. This spec
requires that the path works end to end with streaming (REQ-BOX-37, REQ-SYS-16). Inside the
box the agent sends HTTP/1.1 directly over `/run/hal/model.sock`; third-party CLIs go
through `hal-relay`. Clients may send any dummy credential (`hal-box`); the forwarder
strips it.

### Box-side programs

All three are stdlib-only modules in `halsandbox/boxside/`, run as files in the image and as
console scripts on the host (bwrap and tests).

#### hal-box-entry

```text
hal-box-entry [--relay-port N] [--relay-socket PATH] [--seed DIR]... [--grace S] -- CHILD [ARG...]
hal-box-entry --selftest
hal-box-entry --probe SOCKET
```

1. Copy `/opt/hal/box/<cli>/home-seed/` (image) and then `/run/hal/home-seed/` (session)
   into `$HOME`, files 0600 and directories 0700; never follow symlinks in a seed.
2. Connect to `/run/hal/lifeline.sock`, 20 tries 100 ms apart; on failure write
   one stderr line and exit 69 (`EX_UNAVAILABLE`) without starting the child.
3. For kinds `box`, `shell` and `deps`, start the relay in a thread on `127.0.0.1:<port>`,
   splicing to `--relay-socket` (default `/run/hal/model.sock`; `/run/hal/egress.sock` in the
   prep box).
4. Start the child with `start_new_session=True` (own process group), stdio inherited.
5. A watcher thread reads the lifeline. On EOF, a read error or a `{"hal":"stop"}` message it
   sends SIGTERM to the child's group, SIGKILL after `--grace` (default 5 s), and the entry
   exits 143.
6. SIGTERM and SIGINT received by the entry (from docker-init) are forwarded to the child's
   group; SIGWINCH is left to the TTY.
7. Exit with the child's exit code (signal deaths as `128 + signum`).

Exit codes: child's code; 64 usage; 69 lifeline or socket unavailable; 70 internal error;
143 terminated by the lifeline. Logs go to `/tmp/hal-box-entry.log` at WARNING, never to
stdout (stdout is ACP or the CLI's TTY).

`--selftest` checks the tools in REQ-BOX-25 (`--version` of each with a 5 s timeout), imports
`halagent` and `mcp` with `/opt/hal/venv/bin/python`, and prints one JSON object of versions.
`--probe SOCKET` connects, reads one line, exits 0; it backs the runsc probe.

Lifeline protocol on `lifeline.sock`, newline-delimited JSON:

```text
host -> box (on accept)  {"hal":"lifeline","v":1,"session":"0123456789ab"}
host -> box (optional)   {"hal":"stop","grace_s":5}
box  -> host             nothing
```

```python
class Lifeline:
    """Holds the box's lifeline socket; closing it makes the box terminate itself."""
    def __init__(self, path: Path, session_id: str) -> None: ...
    def start(self) -> None: ...
    def request_stop(self, grace_s: float = 5.0) -> None: ...
    def close(self) -> None: ...
```

Why a lifeline: a SIGKILLed host `hal` leaves its `docker run` client dead and the box
running when the child ignores stdin [verified: `orphan.py`]. The kernel closes the
listening socket when the host process dies, so the box notices within one read.

#### hal-relay

```text
hal-relay [--port 8741] [--socket /run/hal/model.sock] [--max-conns 32]
```

Listens on `127.0.0.1:<port>` only (never `0.0.0.0`; under `--network none` loopback is the
only interface anyway). It takes no token: it is reachable only from inside one box, and the
host forwarder behind it injects the session token. 00-overview lists it as one of the two
exceptions to REQ-SYS-10 (RESOLUTIONS G6). In the prep box the same program runs as
`hal-relay --port 3128 --socket /run/hal/egress.sock`, so uv, npm and cargo use it as their
HTTPS proxy. Per accepted TCP connection it opens one unix connection to its
socket and splices bytes both ways with two threads (`recv(65536)`/`sendall`),
half-closing with `shutdown(SHUT_WR)` on EOF. No HTTP parsing: keep-alive, chunked encoding
and SSE pass through unchanged. Over 32 concurrent connections, new ones are closed at once.
An SSE stream through such a relay arrived at its client with the upstream spacing intact
(events at +0.01 s, +0.31 s, +0.61 s for writes 0.3 s apart) [verified: `box_relay.py` in a
hardened box against a host `ThreadingUnixStreamServer`].

#### hal-mcp-bridge

```text
hal-mcp-bridge [--socket PATH]      default $HAL_BROKER_SOCKET, else /run/hal/broker.sock
```

Connects, then splices stdin to the socket and the socket to stdout byte for byte, with
`shutdown(SHUT_WR)` when stdin reaches EOF. Exits 0 when the socket closes; exits 69 with
`hal-mcp-bridge: cannot connect to <path>: <error>` on stderr when the connect fails. It
never writes to stdout itself. The six `hal box` CLIs and any stdio MCP client in the box
use it as a stdio MCP server named `hal`; hal-agent connects to the socket directly (D9).

### Names and labels

| Label | Value | Use |
| --- | --- | --- |
| `hal.sandbox` | `1` | the sweep's filter |
| `hal.kind` | `code`, `chat`, `job`, `box`, `shell`, `deps` | display |
| `hal.session` | `<sid>` | joins the container to `$R/s/<sid>/` |
| `hal.uid` | host uid | the sweep never removes another user's boxes on a shared daemon |
| `hal.pid` | pid of the host `hal` process | liveness |
| `hal.pidstart` | field 22 of `/proc/<pid>/stat` | liveness that survives pid reuse |
| `hal.version` | halsandbox version | |
| `hal.started` | UTC Z | display |

The sweep borrows a batch evaluation harness's container-labeller pattern (label every
container with the run and the labelling pid, then remove labelled containers whose pid is
dead) and adds the uid
filter and `pidstart` against pid reuse.

```python
def labels(kind: str, sid: str, *, version: str) -> dict[str, str]: ...
@dataclass(frozen=True)
class SweepResult:
    removed: tuple[str, ...]       # container names
    kept: tuple[str, ...]
    dirs_removed: tuple[Path, ...]
    error: str | None
def sweep(*, dry_run: bool = False) -> SweepResult: ...
```

`sweep` lists `docker ps -a --no-trunc --filter label=hal.sandbox=1 --filter
label=hal.uid=<U> --format ...` (60 s), keeps rows whose pid is alive with the same start
time, removes the rest with `docker rm -f` (180 s, the value that harness uses), then removes dead
`$R/s/*` dirs. It never removes a container without both labels; other tools'
containers and anything else on the daemon are out of its reach. At `hal`
start it runs at most once per hour (timestamp in `$STATE/sandbox/last-sweep`).

Teardown order for a session (`Session.close`): `Lifeline.request_stop(grace)`; wait for
the box process up to `stop_timeout_s + 5` s; `docker rm -f hal-<kind>-<sid>` if it still
exists; close the broker `LineServer` and any upstreams; take the exit checkpoint
(`hal box`); remove `$R/s/<sid>/`; log the event. halcore (or `box.run`) then closes the
forwarder and revokes the session token.

### Session

`halsandbox.Session` is the facade halcore uses (06-core-cli "Interfaces consumed"). `start`
prepares everything and returns the argv; halcore then starts the model forwarder on
`model_socket` and spawns `agent_argv` itself, because halcore owns the ACP pipes and is the
only package allowed to import both halsandbox and halgateway.

```python
class Closeable(Protocol):
    def close(self, timeout_s: float = 5.0) -> None: ...

@dataclass(frozen=True)
class GitMount:
    host: Path
    box: str                                          # always read-only

@dataclass(frozen=True)
class LaunchSpec:
    sid: str
    kind: Literal["code", "chat", "job", "box", "shell", "deps"]
    masked: tuple[str, ...]                           # work-relative paths bound over by `empty`
    work: Path                                        # host path mounted at /work
    git_mounts: tuple[GitMount, ...]
    runtime_dir: RuntimeDir
    child_argv: tuple[str, ...]
    env: Mapping[str, str]
    limits: Limits
    tty: bool
    relay_port: int | None
    image_id: str | None                              # docker only
    runtime: Literal["runc", "runsc"] | None          # docker only

class Backend(Protocol):
    name: ClassVar[Literal["docker", "bwrap", "none"]]
    def argv(self, spec: LaunchSpec) -> list[str]: ...

class Session:
    """One sandboxed session: runtime dir, broker, lifeline, checkpoints, optional isolated tree."""
    @classmethod
    def start(cls, project: Path, *, kind: str = "code", backend: str = "auto",
              isolated: bool = False, image: str | None = None, approver: Approver,
              channel: str, policy_profile: str, checkpoints: bool = True,
              mask: bool | None = None, interactive: bool = False,
              run_files: Mapping[str, bytes] | Callable[[str], Mapping[str, bytes]] = {},
              home_seed: Mapping[str, bytes] = {},
              child_argv: Sequence[str] = ("hal-agent", "--acp", "--config",
                                           "/run/hal/agent.json"), tty: bool = False,
              extra_env: Mapping[str, str] = {}) -> "Session": ...
    id: str                          # 12 hex
    backend: str                     # "docker-runsc", "docker-runc", "bwrap" or "none"
    work: Path                       # project, or the isolated worktree
    model_socket: Path               # $R/s/<sid>/run/model.sock; halcore serves it
    broker_socket: Path              # $R/s/<sid>/run/broker.sock (host path)
    broker: Broker                   # halsandbox.broker.Broker; on_human_message per 01-security
    masked: tuple[str, ...]          # REQ-BOX-69
    agent_argv: list[str]            # the full docker or bwrap argv, or the host argv for none
    def capabilities(self) -> dict[str, object]: ...   # the "sandbox" object of session.json
    def checkpoint(self, label: str = "") -> int: ...  # REQ-BOX-44; returns n
    def undo(self, to: int) -> RestoreResult: ...       # restore; includes not_restorable
    def diff(self, since: int | None = None) -> str: ...        # patch text
    def report(self, since: int | None = None) -> DiffReport: ...   # patch, stat and flags
    def apply_isolated(self, *, allow_flags: bool = False) -> list[Path]: ...
    def reject_isolated(self) -> None: ...
    def close(self, timeout: float = 15.0) -> int | None: ...   # teardown; box exit code
```

`Approver` is 01-security's single approval protocol in `halhub.approvals` (it returns a
`Decision(verdict, scope)`; RESOLUTIONS B1); halsandbox only passes it to the broker and to
the dependency-prep request. `mask = None` takes the per-project choice (REQ-BOX-70), `True`
or `False` overrides it for this session; `interactive` says whether a terminal is attached,
which the backend-`none` consent (REQ-BOX-68) needs. `run_files` are written into `run/`
before the box starts: halcore renders `agent.json`, `user/AGENTS.md` and `skills/...`
(06-core-cli, schemas in 03-agent, RESOLUTIONS E4); `home_seed` goes to `run/home-seed/`.
`run_files` is either a mapping or a callable that `start` calls once with the new session
id after the runtime dir exists and before the box starts, because `agent.json` carries the
id that `start` generates (06-core-cli launch sequence, step 3); a callable that raises stops
`start` with that error and removes the runtime dir. Both reject absolute paths, `..` and
symlinks, and both are scanned for the session token value before writing (REQ-BOX-33).

`start` runs, in order: detect the backend (REQ-BOX-02) and, for `none`, check the
per-project consent (REQ-BOX-68); resolve the work tree (isolated or not; for kind `chat`,
check it is under `$STATE/assistant/`); find masked files (REQ-BOX-69); create the runtime
dir; take checkpoint 0; start the broker `LineServer` (`halsandbox.broker.Broker` with
01-security's policy, approvals and audit, given `approver`, `channel` and
`policy_profile`, plus the upstreams of REQ-BOX-66); start the lifeline; resolve the image
ID; build `agent_argv`. It does not spawn the box. `close` runs the teardown order in "Names
and labels" except the forwarder, which halcore closes and whose token halcore revokes.

`hal code` (06): `Session.start(...)`, then the forwarder on `session.model_socket`, then
`subprocess.Popen(session.agent_argv, stdin=PIPE, stdout=PIPE)` and ACP over the pipes;
`session.checkpoint()` and `session.broker.on_human_message(...)` before each
`session/prompt` (RESOLUTIONS F5). Long-lived assistant boxes (00-overview Flow 2) are
sessions of kind `chat` that halcore keeps open, started with
`child_argv = ("hal-agent", "--acp", "--toolset", "assistant", "--config",
"/run/hal/agent.json")` (03-agent, RESOLUTIONS F3). Long jobs (`core_long_task`, RESOLUTIONS
F2) run `hal -p`, whose session has kind `job`.

### Checkpoints

#### Host git hardening

Any host git call on a model-written tree is a code-execution risk: a nested repository's
config is read by recursive commands. A nested repo with `core.fsmonitor = "touch <marker>"`
ran its command on the host when `git status` ran in the parent with a temporary index that
held the nested repo as a gitlink; with `-c core.fsmonitor=false` (which propagates to child
git processes through `GIT_CONFIG_PARAMETERS`) and `--ignore-submodules=all`, checkpoint,
status, diff and restore never ran it; a plain `git status` in the parent with the nested
repo untracked did not run it either [verified: `cprepo` experiment, five cases].

```python
GIT_TIMEOUT_S = 60
HARDEN_ARGS = (
    "-c", "core.fsmonitor=false",          # nested repo config cannot run commands
    "-c", "core.hooksPath=/dev/null",      # no hooks from host-side plumbing
    "-c", "diff.ignoreSubmodules=all",     # never recurse into nested repos
    "-c", "status.submoduleSummary=false",
    "-c", "submodule.recurse=false",
    "-c", "core.untrackedCache=false",
    "-c", "protocol.allow=never",          # no transport, ever
    "-c", "core.pager=cat",
)

class GitError(Exception): argv: list[str]; returncode: int; stderr: str

class GitRunner:
    """Runs host git on a model-written tree with hardening, a timeout and a clean config."""
    def __init__(self, work_tree: Path, *, git_dir: Path | None = None,
                 timeout_s: float = GIT_TIMEOUT_S) -> None: ...
    def run(self, *args: str, index: Path | None = None, input: bytes | None = None,
            check: bool = True) -> subprocess.CompletedProcess[bytes]: ...
```

`run` always passes `--git-dir` and `--work-tree` explicitly, runs with `cwd` set to the git
dir's parent outside the work tree (never a directory inside it), starts from an environment
with every inherited `GIT_*` variable removed (01-security REQ-SEC-67), sets
`GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_TERMINAL_PROMPT=0`,
`GIT_OPTIONAL_LOCKS=0`, `LC_ALL=C`, author and committer `Hal <hal@localhost>`, and passes
the user's `core.excludesFile` explicitly (read once with `git config --global --get`), so
globally ignored files stay out of checkpoints while the rest of the global config (filters,
aliases, external diff tools) is ignored. Diff calls also pass `--no-ext-diff --no-textconv`.
Repository-local config is still read: it is the user's own file and the box cannot write it.

#### Taking a checkpoint (git)

```text
idx = mktemp in $STATE/sandbox/tmp/
cp <git-dir>/index idx                          (stat cache; absent in a new repo)
GIT_INDEX_FILE=idx git add -A -- . ':!<skipped files>' ':!<masked files>'
GIT_INDEX_FILE=idx git rm --cached --quiet --ignore-unmatch -- <masked files>   (tracked ones)
tree = GIT_INDEX_FILE=idx git write-tree
if tree == previous checkpoint's tree: return previous           (REQ-BOX-46)
commit = git commit-tree <tree> [-p HEAD] -m <message>
git update-ref refs/hal/checkpoints/<sid>/<n> <commit>
rm idx
```

Skipped files: before `git add`, `git ls-files -z --others --modified --exclude-standard`
gives candidates; any regular file over `max_file_bytes`, and any FIFO, socket or device, is
excluded by pathspec and listed.

Masked files: `git ls-files -z --cached --others --exclude-standard` gives every non-ignored
path; each whose name matches a `[sandbox].mask` pattern or one of 01-security's hard-deny
file patterns (`fnmatch.fnmatchcase` on the last component, the rule `find_masked` uses) is
left out of the checkpoint by pathspec, removed from the temporary index when it is tracked,
and listed as masked. This runs on every checkpoint, so a secret file the model creates during
the session stays out of `.git/objects` too. A checkpoint object is readable in the box
because `.git` is mounted read-only, not hidden (REQ-BOX-15); without this rule an untracked,
non-ignored `deploy/server.key` would be one `git show refs/hal/checkpoints/<sid>/0:deploy/server.key`
away from the model even though the work tree file reads as empty. When the project is
unmasked (REQ-BOX-70) nothing is left out for this reason. A masked file that the user has
committed is already in the repository's own objects; neither masking nor checkpoints can hide
it, and the session notice says so (see "Secret masking").

Ignored paths are recorded too, so `/undo` can say what it cannot bring back:
`git ls-files -z --others --ignored --exclude-standard --directory` gives them (a whole
ignored directory is one entry), and `/work/.hal/` is left out because `hal code --deps`
rebuilds it. The metadata is one JSON object (`json.dumps`, `ensure_ascii=True`)
`{"ignored": [...], "masked": [...], "truncated": false}`, stored as a blob
(`git hash-object -w --stdin`) under the ref `refs/hal/checkpoint-meta/<sid>/<n>`, outside the
checkpoint tree so a restore never writes it into the work tree. It holds names only, never
content. The ignored list is capped at `IGNORED_LIST_MAX = 10000` entries (`truncated` says
when it was cut), and `prune` deletes the blob with its checkpoint.
The message (internal, ASCII):

```text
hal checkpoint 0123456789ab 3

label: turn 3
created: 2026-10-23T18:07:44Z
skipped: data/big.bin (73400320 bytes)
masked: deploy/server.key
```

Verified end to end [verified: `cprepo` experiment with git 2.53.0]: a checkpoint of a repo
with a staged and an unstaged change captured both working-tree contents; after the "model"
modified, deleted and added files, `git diff --name-status` showed `M`, `D`, `A`; restore
brought back the checkpoint state, deleted the added files and the emptied directory, left
ignored files alone, and `.git/index` was byte-identical (sha256) with `git status
--porcelain` unchanged.

#### Restore and diff

```text
safety = take("before restore to <n>")
now_idx = snapshot index of the current tree (as in "take", without commit)
GIT_INDEX_FILE=now_idx git restore --source=<target commit> --worktree -- .
```

`git restore` without `--staged` writes the work tree only, and its no-overlay mode deletes
paths that are in the (temporary) index but not in the source, which removes files added
after the checkpoint. Ignored, skipped and masked files are in neither index (the snapshot of
the current tree applies the same exclusions), so they stay.
`/undo` restores the checkpoint taken before the last turn; `/undo N` restores checkpoint N;
the safety checkpoint makes every undo undoable. `not_restorable` compares the target's
ignored, skip and masked lists with the work tree: every recorded path that no longer exists
is listed with its reason, and the TUI prints it after the restore ("not restorable:
data/local.db (ignored)", ".env (masked)") (REQ-BOX-47, ATK-43).

```python
@dataclass(frozen=True)
class Checkpoint:
    sid: str
    n: int
    ref: str
    commit: str
    tree: str
    created: str
    label: str
    skipped: tuple[str, ...]
    masked: tuple[str, ...]           # left out by REQ-BOX-45; names only

@dataclass(frozen=True)
class RestoreResult:
    restored: tuple[str, ...]
    deleted: tuple[str, ...]
    not_restorable: tuple[tuple[str, Literal["ignored", "skipped", "masked"]], ...]
                                      # recorded at the target and now missing
    safety: Checkpoint

class Checkpoints:
    """Snapshots of the work tree for /undo and /diff; never touches the user's index or refs."""
    @classmethod
    def for_work(cls, work: Path, sid: str, cfg: CheckpointConfig) -> "Checkpoints": ...
    def take(self, label: str) -> Checkpoint: ...
    def list(self) -> list[Checkpoint]: ...
    def diff(self, since: Checkpoint | None = None, *, max_patch_bytes: int = 2 * 2**20
             ) -> DiffReport: ...            # since=None: the session's first checkpoint
    def restore(self, target: Checkpoint) -> RestoreResult: ...
    def prune(self, *, keep_sessions: int, max_age_days: int) -> int: ...
```

Checkpoint refs appear in `git log --all` and graphical tools that show all refs; the user
guide says so and how to hide them (`git log --exclude=refs/hal/* --all`).

#### DiffReport and flags

```python
@dataclass(frozen=True)
class FileChange:
    path: str
    status: Literal["A", "M", "D", "T"]          # T = type change (file <-> symlink)
    old_mode: str | None
    new_mode: str | None

@dataclass(frozen=True)
class Flag:
    path: str
    kind: Literal["nested_repo", "symlink_outside", "executable_added", "git_metadata",
                  "automation", "skipped_large", "special_file"]
    severity: Literal["high", "medium"]
    detail: str

@dataclass(frozen=True)
class DiffReport:
    base: str                    # commit
    head_tree: str
    files: tuple[FileChange, ...]
    flags: tuple[Flag, ...]
    stat: str
    patch: str                   # git diff --binary, cut at max_patch_bytes
    truncated: bool
```

| Flag kind | Detected by | Severity |
| --- | --- | --- |
| `nested_repo` | a mode `160000` entry added (a nested `.git`) | high |
| `symlink_outside` | a mode `120000` blob whose target is absolute or normalises outside the root | high |
| `git_metadata` | `.gitattributes`, `.gitmodules`, `.gitconfig` anywhere, `.git` file (gitfile) | high |
| `executable_added` | new mode `100755`, or `100644 -> 100755` | medium |
| `automation` | a path matching any pattern in 01-security's "Workspace scan" table (the single list; this spec does not restate it) | medium |
| `skipped_large`, `special_file` | from the checkpoint's skip list | medium |

These are the files a user may later execute without reading them (01-security residual
risk); the flags make the review point at them. `halsandbox.diffreport` is the workspace scan
of 01-security REQ-SEC-70: the turn summary and the end-of-session summary count its flags,
and there is no separate scan module.

#### Non-git work trees

A shadow repository at `$STATE/shadow/<sha256(realpath)[:16]>.git` (bare, `core.worktree`
unset; every call passes `--git-dir` and `--work-tree`) gives the same `Checkpoints` API.
Its `info/exclude` holds defaults: `.venv/`, `venv/`, `node_modules/`, `__pycache__/`,
`.mypy_cache/`, `.pytest_cache/`, `.cache/`, `target/`, `dist/`, `build/`, `*.pyc`, plus the
tree's own `.gitignore` files which git reads anyway. Before the first checkpoint the session
sums regular file sizes under the tree (excluding those patterns); over
`nongit_max_total_bytes` it refuses to start unless `--isolated` or `--no-checkpoint` is
given (REQ-BOX-52). The shadow repo lives outside the work tree, so the box never sees it.
This replaces the brief's tarball checkpoint (Decision D6, settled by RESOLUTIONS E8); every
shadow-repo call goes through `GitRunner`.

### Isolated mode

```python
@dataclass(frozen=True)
class AcceptResult:
    applied: bool
    files: tuple[str, ...]
    safety: Checkpoint | None
    patch_path: Path
    error: str | None

class IsolatedWorkspace:
    """A throwaway copy of the project for one session; changes reach the project on accept."""
    @classmethod
    def create(cls, project: Path, sid: str, cfg: IsolatedConfig) -> "IsolatedWorkspace": ...
    project: Path
    path: Path                        # $STATE/worktrees/<sid>
    base: str                         # commit
    def report(self) -> DiffReport: ...
    def accept(self, *, allow_flags: bool = False) -> AcceptResult: ...
    def reject(self) -> None: ...
    def export_patch(self, dest: Path) -> Path: ...
```

Git projects:

1. `base` = a checkpoint-style commit of the project's current state (temp index, includes
   uncommitted and untracked non-ignored files, leaves out masked files exactly as a
   checkpoint does, REQ-BOX-45), stored at `refs/hal/isolated/<sid>/base`. Masked files are
   therefore absent from the worktree, not only empty; the notice says which, and accept
   never deletes them from the project because the patch is computed against this base.
2. `git worktree add --detach $STATE/worktrees/<sid> <base>` (hooks off through
   `HARDEN_ARGS`). The worktree's `.git` is a gitfile, so "Git layouts" mounts its admin dir
   and the common dir read-only.
3. Mask discovery (REQ-BOX-69) runs on the worktree, which is what the box mounts; the box
   mounts only the worktree at `/work`; checkpoints run inside the worktree.
4. Accept: `report()`; refuse on `high` flags unless `allow_flags`; `take("before accept
   <sid>")` in the project; `git diff --binary <base> <worktree snapshot tree>` to
   `$STATE/patches/<sid>.patch`; `git apply --whitespace=nowarn <patch>` in the project.
   `git apply` without `--index` changes the work tree only (the user's index stays as it
   is), and fails as a whole when any hunk fails [unverified: git-apply documentation; test
   `test_accept_is_atomic_and_checkpointed` proves it].
5. Reject or a finished accept: `git worktree remove --force`, `git worktree prune`, delete
   `refs/hal/isolated/<sid>/`.

`git apply` refused a hostile patch that wrote `.git/hooks/pre-commit`, `../escape.txt`,
`sub/.GIT/config`, `sub2/.git/config`, or a file through a symlink created earlier in the
same patch ("beyond a symbolic link"); it did create an absolute symlink `lnk -> /tmp`
[verified: `ap` experiment]. That symlink case is why `symlink_outside` is a high flag and
blocks accept by default.

Non-git projects: copy with `cp -a --reflink=auto <project>/. <path>` (600 s timeout) after a
size walk against `max_copy_bytes` (2 GiB); mask discovery runs on the copy, which is what the
box mounts; the base is a shadow-repo commit of the copy, with the same masked-path exclusion,
made before the box starts; accept runs `git apply` with `cwd=<project>`, which works outside a
repository.

`hal-sandbox ls` lists isolated sessions with changes not yet accepted or rejected; the
sweep never deletes those (REQ-BOX-42); empty ones older than 7 days are removed.

### Path and git safety

```python
class WorkspaceEscape(Exception): path: str; reason: str

def box_to_host(box_path: str, work: Path) -> Path: ...
def read_beneath(root: Path, rel: str, *, max_bytes: int) -> bytes: ...
```

Rules:

- `box_to_host` accepts `/work` and `/work/<rel>` only; it rejects NUL, a `..` that leaves
  `/work`, and every other absolute path.
- Every host open inside a work tree goes through `halkit.fsafe.open_beneath(root, rel,
  flags)` (00-overview signature; walk rules in 01-security "Proposed halkit additions"): a
  component-wise walk with `O_NOFOLLOW` relative to an already-open directory fd, which
  refuses `..`, absolute components and any symlink with `UnsafePathError`. That avoids the
  check-then-open race of `realpath` checks. halsandbox keeps no walker of its own
  (RESOLUTIONS C10).
- `read_beneath` is the halsandbox wrapper: it calls `open_beneath`, then `fstat`s and refuses
  anything but a regular file (a FIFO would block, a device would leak), and reads at most
  `max_bytes`. Diff viewers show a symlink as `link -> target` from the git tree, never its
  content (REQ-SEC-68).
- Inside a box, symlinks resolve inside the box's own mount namespace, so a link to
  `/etc/passwd` reads the box's file, not the host's. The danger is host code following
  model-written links later, which is what `open_beneath` and the diff flags cover.
- Hard links: the box cannot see files outside its mounts, and a hard link across mounts
  fails with EXDEV, so it cannot link a host file into `/work`. Hard links the user created
  are the user's choice.
- Disk use: `/work` is a bind of the user's filesystem; the box can fill it. Docker has no
  quota for binds. This is a documented residual risk; the checkpoint size caps limit the
  damage to `.git`.

### Secret masking

Host hard-deny paths (01-security) do not apply inside the box, so a `.env` or a private key
in the project would be readable by the model (security review R-new-6). Masking
closes the common cases with one decision per project instead of one per call (RESOLUTIONS
"Masking").

```python
from halhub.policy import MASK_DEFAULT   # 01-security's single list (REQ-BOX-69, Decision D31)
MASK_SKIP_DIRS = (".git", ".hal", "node_modules", ".venv", "venv")   # large, never hold user secrets
MASK_MAX_FILES = 256          # more matches than this means a data folder, not secrets
MASK_WALK_TIMEOUT_S = 10      # a tree that takes longer to walk fails loudly

def find_masked(work: Path, patterns: Sequence[str]) -> tuple[str, ...]: ...
```

- `find_masked` walks the work tree with `os.scandir` (no symlink following), skipping only
  the directories named in `MASK_SKIP_DIRS`, and matches each regular file's name with
  `fnmatch.fnmatchcase`. It descends into directories git ignores: a `.env` or a
  `secrets/prod.pem` is usually git-ignored and is exactly what masking is for. The walk does
  not ask git which directories are ignored. Large ignored trees (`build/`, `target/`) count
  against `MASK_WALK_TIMEOUT_S`; a tree that exceeds it fails loudly rather than leaving
  files unchecked. A symlink whose name matches is
  resolved with `os.path.realpath` and checked with `halkit.fsafe.is_beneath`: a target
  inside the work tree is masked at the target path; a target outside is invisible in the box
  already.
- Docker and bwrap bind `$R/s/<sid>/empty` (0444, zero bytes) read-only over each path. In
  the box the file reads as empty; writing fails with EROFS, and `rm` or `mv` fail with EBUSY
  because the path is a mount point [unverified: check both errors in
  `test_masked_file_reads_empty_and_cannot_be_written`].
- More than `MASK_MAX_FILES` matches, or a walk over `MASK_WALK_TIMEOUT_S`, stops the session
  with an error that names `--unmask` and `[sandbox].mask`.
- Files created during the session are not masked; the model created them.
- The session prints one notice ("2 files masked: .env, deploy/server.key; `--unmask` to show
  them to the agent"), and `session.json` lists the paths so the agent can explain empty
  reads.
- `hal code --unmask` (flag in 06-core-cli) asks on a terminal ("Show masked files to the
  agent in <project>? Type yes") and records `unmask = true` for the project in
  `$STATE/sandbox/projects.json`; `--mask` removes it. Checkpoints and isolated snapshots
  leave masked files out unless the project is unmasked (REQ-BOX-45, REQ-BOX-53), because
  their objects sit in the read-only `.git` the box can read.
- A masked file that git tracks is in the repository's history, which the box can read
  through `/work/.git`. The notice marks such files ("deploy/server.key is committed: its
  history is readable in the box") and `session.json` lists them in `masked_tracked`; removing a
  committed secret from history is the user's job (01-security residual risk 14).

Per-project choices file (`halsandbox.projects`), 0600, written with
`halkit.files.write_atomic`:

```json
{"schema": 1,
 "projects": {"/home/u/src/proj": {"unmask": true, "unmask_set": "2026-10-24T09:12:00Z",
                                   "allow_none": false, "deps": {"uv.lock": "sha256:..."}}}}
```

`allow_none` is the one-time consent to backend `none` (REQ-BOX-68); `deps` records the
lockfile sha256 of the last successful prep (REQ-BOX-74). None of these is an approval grant
(RESOLUTIONS B3 forbids grant files): `unmask` and `allow_none` are sandbox settings the user
typed on a terminal, and `deps` only decides whether to offer a prep again. `hal-sandbox
forget DIR` deletes a project's entry.

### Dependency preparation

The box has no network, and existing project venvs point at host interpreters the box cannot
see, so without help the agent cannot run a project's tests (security review F3). `hal code
--deps` (flag in 06-core-cli; also `hal-sandbox deps DIR`) prepares dependencies in a separate
prep box (RESOLUTIONS F3).

```python
@dataclass(frozen=True)
class Lockfile:
    manager: Literal["uv", "npm", "cargo"]
    path: str                     # work-relative
    sha256: str                   # "sha256:<hex>"

@dataclass(frozen=True)
class PrepResult:
    lockfile: Lockfile
    ok: bool
    exit_code: int | None
    log_tail: str                 # last 4000 characters of the prep output
    denied_hosts: tuple[str, ...]

def detect(work: Path) -> list[Lockfile]: ...
def pending(work: Path) -> list[Lockfile]: ...      # sha256 differs from projects.json
def prepare(project: Path, *, approver: Approver, lockfiles: Sequence[Lockfile] | None = None,
            timeout_s: float = 900.0) -> list[PrepResult]: ...
```

| Lockfile (work root) | Command in the prep box | Writes | Needs in the image |
| --- | --- | --- | --- |
| `uv.lock` | `uv sync --frozen` | `/work/.hal/venv`, cache `/work/.hal/cache/uv` | `uv`, python3 (always present) |
| `package-lock.json` | `npm ci --no-audit --no-fund` | `/work/node_modules`, cache `/work/.hal/cache/npm` | `[sandbox].node = true` |
| `Cargo.lock` | `cargo fetch --locked` | `/work/.hal/cargo` | `cargo` in `extra_apt_packages` |

Flow:

1. `pending(work)` compares each lockfile's sha256 with `projects.json`. halcore offers
   `--deps` at session start when the list is not empty (06-core-cli renders the offer); it
   never runs a prep unprompted.
2. For each lockfile, one approval request through `approver`: tool `sandbox_deps_prep`, pack
   `sandbox`, tier `act`, arguments `{"manager": "uv", "lockfile": "uv.lock", "sha256":
   "sha256:..."}`, so the key (01-security) binds the exact lockfile content. Deny or
   timeout runs nothing. The decision goes to the audit log like any other.
3. A checkpoint of the project (REQ-BOX-75), then the prep box: the docker argv of "Docker
   argv" with kind `deps`, the same image, mounts and limits, no broker or model socket,
   `run/egress.sock` served by `ConnectProxy`, `--entrypoint hal-box-entry --relay-port 3128
   --relay-socket /run/hal/egress.sock`, and the environment below. The prep box is
   docker-only in this release (Decision D30): on bwrap or `none`, `--deps` is an error that
   names docker.
4. On exit 0 the lockfile sha256 is recorded in `projects.json`; on failure the result
   carries the log tail and any hosts the proxy refused, and nothing is recorded.

Prep box environment, added to "Environment" (and replacing its offline entries):
`HTTPS_PROXY` and `https_proxy` = `http://127.0.0.1:3128`, `NPM_CONFIG_HTTPS_PROXY` and
`CARGO_HTTP_PROXY` with the same value (each manager reads a different name [unverified:
`test_deps_live.py` proves uv and npm; cargo is checked by hand]), `HTTP_PROXY` unset,
`NO_PROXY=""`,
`UV_PROJECT_ENVIRONMENT=/work/.hal/venv`, `UV_CACHE_DIR=/work/.hal/cache/uv`,
`UV_LINK_MODE=copy`, `UV_PYTHON=/usr/bin/python3`, `UV_PYTHON_DOWNLOADS=never`,
`NPM_CONFIG_CACHE=/work/.hal/cache/npm`, `CARGO_HOME=/work/.hal/cargo`. `UV_OFFLINE`,
`NPM_CONFIG_OFFLINE` and `CARGO_NET_OFFLINE` are left out here only. Before the box starts the
host creates `/work/.hal/` with a `.gitignore` of `*`, so checkpoints, `/diff` and the user's
`git status` ignore it.

Egress proxy (`halsandbox.egress`):

```python
DEFAULT_REGISTRIES = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
                      "index.crates.io", "static.crates.io", "crates.io")
CONNECT_PORT = 443            # TLS only; plain HTTP to a registry would let a proxy-side attacker swap files
HEAD_TIMEOUT_S = 10           # a client that cannot send a CONNECT line in 10 s is not a package manager
IDLE_TIMEOUT_S = 120          # large wheels stream for minutes but never stall that long

class ConnectProxy:
    """HTTP CONNECT proxy on a unix socket that tunnels only to allowlisted registries."""
    def __init__(self, path: Path, *, registries: Collection[str], on_event: Callable[[dict], None],
                 max_tunnels: int = 16) -> None: ...
    def start(self) -> None: ...
    def close(self, timeout_s: float = 5.0) -> None: ...
    denied: list[str]
```

- It accepts only `CONNECT host:443 HTTP/1.1`; any other method, port or host gets
  `403 Forbidden` and a `denied` entry. The host must equal a registry name exactly (no
  suffix match).
- It resolves the host on the host side and connects only when every address passes
  `halkit.netguard`'s private, loopback and link-local refusal; it connects to the checked
  address, so DNS cannot change between the check and the connect.
- TLS stays end to end between the package manager and the registry: the proxy never
  terminates TLS and holds no CA, so content integrity comes from TLS plus the hashes in
  `uv.lock`, `package-lock.json` (`integrity`) and `Cargo.lock` (`checksum`).
- Install scripts (npm `postinstall`, sdist builds under uv) run inside the prep box with the
  same confinement as the agent box and the registry allowlist as their only egress. They
  can change the work tree, which the model can too; the checkpoint from step 3 lets
  `/undo` restore it. A registry itself can still serve a malicious package version that the
  lockfile names; this is the supply-chain risk any `npm ci` has, listed in 01-security.

### hal box

```text
hal box [--dir DIR] [--backend B] [--isolated] [--image REF] [--model ALIAS] [--ask]
        [--no-mcp] [--no-checkpoint] -- CLI [CLI-ARGS...]
hal box harnesses
hal box image build [NAME] | status
```

06-core-cli parses the options before `--` and calls `halsandbox.box.run`; everything after
`--` reaches the CLI unchanged (REQ-CLI-42).

- `CLI` is one of `claude`, `qwen`, `opencode`, `codex`, `crush`, `goose`.
  `hal box harnesses` prints each with its pinned version and whether its image is built.
- Backend must be docker (REQ-BOX-59); `--backend` other than `docker` or `auto` is an error
  that names `hal-sandbox doctor`.
- `--image REF` runs a custom image built `FROM` the CLI's box image (label check as for
  `[sandbox].image`).
- `--ask` sets `auto_approve = false` for this run, so the CLI keeps its own prompts.
- `--model` is a gateway alias; default `[box].model`, else the role alias `hal-code`
  (05-gateway, RESOLUTIONS D1).
- The box env gets `HAL_HARNESS=1`: 01-security's control socket refuses a `decide` from a
  process whose environment has it (REQ-SEC-26), so the CLI cannot approve its own calls.
- Approvals: a `hal box` CLI never gets MCP elicitation (REQ-BOX-31, RESOLUTIONS B4). Broker
  `ask` decisions go to host surfaces only: `hal approve` / `hal approvals watch` in another
  terminal through the control socket, or a desktop notification for `once` requests whose
  tool has no `content` parameter (01-security "Surfaces"). `hal box` prints one line at
  start naming `hal approvals watch`, and `docs/user/sandbox.md` says so.
- With a TTY, the CLI runs on a host-owned pty behind the terminal filter (REQ-BOX-78,
  "Terminal filter"); the exit summary and any stderr tail are stripped of terminal controls
  (REQ-BOX-79).
- The command blocks until the CLI exits and returns its exit code.

```python
def run(argv: Sequence[str], *, project: Path, backend: str = "auto", isolated: bool = False,
        image: str | None = None, model: str | None = None, ask: bool = False,
        mcp: bool = True, checkpoints: bool = True, mask: bool | None = None,
        approver: Approver, start_model: Callable[[Path, str], Closeable],
        context_window: int | None = None) -> int: ...
def harnesses() -> list[dict[str, object]]: ...   # name, version, image tag, built: bool
```

`start_model(socket_path, sid)` is supplied by halcore and starts the 05-gateway
`SessionForwarder` with a fresh session token; `box.run` calls it before the box starts and
closes the returned object at the end, so halsandbox never imports halgateway.

```python
@dataclass(frozen=True)
class BoxContext:
    model: str                          # gateway alias, e.g. "hal-code"
    context_window: int                 # from GET /v1/models metadata, else 32768
    max_output_tokens: int              # [box].max_output_tokens, default 8192
    relay_url: str                      # "http://127.0.0.1:8741"
    mcp_command: tuple[str, ...]        # ("hal-mcp-bridge",)
    auto_approve: bool
    user_args: tuple[str, ...]
    home: str = "/home/hal"

@dataclass(frozen=True)
class BoxLaunch:
    argv: tuple[str, ...]
    env: Mapping[str, str]              # added to the box env; no secrets
    home_seed: Mapping[str, bytes]      # relative path under $HOME -> content

class CliProfile(Protocol):
    name: ClassVar[str]
    def launch(self, ctx: BoxContext) -> BoxLaunch: ...

PROFILES: dict[str, CliProfile]
```

`context_window` comes from the gateway's `/v1/models` entry for the alias (05-gateway
exposes `context_length`), fetched by halcore and passed in; 32768 is the fallback, matching
the old launcher's `CLAUDE_CODE_MAX_CONTEXT_TOKENS` default (`hal:168`).

#### Per-CLI templates

Network is cut by `--network none`, so the telemetry and update settings below are there to
avoid startup stalls and log noise, not for privacy. Every CLI gets `DO_NOT_TRACK=1` and
`HAL_API_KEY=hal-box` (a dummy that the forwarder strips).

| CLI | Model API | Files written in `$HOME` | Env | argv added | Auto-approve | Own sandbox off | Checked |
| --- | --- | --- | --- | --- | --- | --- | --- |
| claude 2.1.295 | Anthropic Messages at `http://127.0.0.1:8741` | `.claude.json` `{"hasCompletedOnboarding": true, "bypassPermissionsModeAccepted": <auto>}`; `.config/hal-box/claude-mcp.json` `{"mcpServers": {"hal": {"type": "stdio", "command": "hal-mcp-bridge", "args": []}}}` | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN=hal-box`, `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` = model; `CLAUDE_CODE_MAX_OUTPUT_TOKENS`; `CLAUDE_CODE_MAX_CONTEXT_TOKENS`; the telemetry block from `hal:254-261` (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`, `DISABLE_AUTOUPDATER=1`, `DISABLE_UPDATES=1`, ... `CLAUDE_CODE_DISABLE_FAST_MODE=1`) | `--mcp-config ~/.config/hal-box/claude-mcp.json --strict-mcp-config` | `--dangerously-skip-permissions` | bash sandbox not enabled (default off) | `--version` offline in a hardened box; `--mcp-config` and `--strict-mcp-config` in `--help` [verified]; `.claude.json` keys [unverified] |
| qwen 0.25.0 | OpenAI Chat Completions at `.../v1` | `.qwen/settings.json` `{"$version": 4, "security": {"auth": {"selectedType": "openai"}}, "model": {"name": M}, "modelProviders": {"openai": [{"id": M, "name": "Hal", "baseUrl": "http://127.0.0.1:8741/v1", "envKey": "HAL_API_KEY"}]}, "privacy": {"usageStatisticsEnabled": false}, "telemetry": {"enabled": false}, "general": {"enableAutoUpdate": false}, "mcpServers": {"hal": {"command": "hal-mcp-bridge", "args": []}}}` | `HAL_API_KEY` | none | `--approval-mode yolo` | `QWEN_SANDBOX` unset | `qwen mcp list` parsed it offline [verified]; `SETTINGS_VERSION=4` in the bundle [verified] |
| opencode 1.18.35 | OpenAI-compatible via `@ai-sdk/openai-compatible` | `.config/opencode/opencode.json` `{"autoupdate": false, "share": "disabled", "provider": {"hal": {"npm": "@ai-sdk/openai-compatible", "name": "Hal", "options": {"baseURL": "http://127.0.0.1:8741/v1", "apiKey": "{env:HAL_API_KEY}"}, "models": {M: {"name": M}}}}, "model": "hal/" + M, "mcp": {"hal": {"type": "local", "command": ["hal-mcp-bridge"], "enabled": true}}, "permission": <auto>}` | `OPENCODE_DISABLE_AUTOUPDATE=1`, `OPENCODE_DISABLE_MODELS_FETCH=1`, `OPENCODE_DISABLE_SHARE=1`, `OPENCODE_DISABLE_LSP_DOWNLOAD=1` | none | `"permission": {"edit": "allow", "bash": "allow", "webfetch": "deny"}` | n/a | `opencode mcp list` and `opencode models hal` parsed it offline [verified]; env names present in the binary [verified: strings]; `permission` keys and whether the provider npm package loads offline [unverified: REQ-BOX-61 round trip; if it needs a download, the box image pre-seeds it under `/opt/hal/box/opencode/home-seed/.cache/opencode/`] |
| codex 0.162.0 | OpenAI Responses at `.../v1` | `.codex/config.toml`: `model`, `model_provider = "hal"`, `approval_policy`, `sandbox_mode = "danger-full-access"`, `check_for_update_on_startup = false`, `[analytics] enabled = false`, `[otel] exporter = "none"`, `[model_providers.hal] name, base_url = "http://127.0.0.1:8741/v1", env_key = "HAL_API_KEY", wire_api = "responses"`, `model_context_window`, `[mcp_servers.hal] command = "hal-mcp-bridge", args = [], startup_timeout_sec = 20` | `HAL_API_KEY` | none | `approval_policy = "never"` | `sandbox_mode = "danger-full-access"`: codex's bwrap cannot create namespaces in the box (`unshare -U` fails) | `codex mcp list` parsed it offline [verified]; key names present in the binary [verified: strings]; needs `/v1/responses` through the gateway (05-gateway) |
| crush 0.98.0 | OpenAI-compatible | `.config/crush/crush.json` `{"providers": {"hal": {"type": "openai-compat", "base_url": "http://127.0.0.1:8741/v1", "api_key": "$HAL_API_KEY", "models": [{"id": M, "name": M, "context_window": W, "default_max_tokens": T}]}}, "mcp": {"hal": {"type": "stdio", "command": "hal-mcp-bridge"}}, "options": {"disable_metrics": true, "disable_provider_auto_update": true}}` | `CRUSH_DISABLE_METRICS=1`, `CRUSH_DISABLE_PROVIDER_AUTO_UPDATE=1` | none | `--yolo` | n/a | `--version` offline [verified]; key and env names present in the binary [verified: strings]; config accepted [unverified: REQ-BOX-61] |
| goose 1.54.0 | OpenAI Chat Completions | `.config/goose/config.yaml`: `GOOSE_PROVIDER: openai`, `GOOSE_MODEL: M`, `OPENAI_HOST: http://127.0.0.1:8741`, `OPENAI_BASE_PATH: v1/chat/completions`, `extensions: {hal: {type: stdio, cmd: hal-mcp-bridge, args: [], enabled: true, timeout: 300}}` | `OPENAI_API_KEY=hal-box`, `GOOSE_DISABLE_KEYRING=1`, `GOOSE_TELEMETRY_ENABLED=false` | none | `GOOSE_MODE=auto` | n/a | [unverified: not installed in the test image; REQ-BOX-61] |

`M` is the model alias, `W` the context window, `T` the max output tokens. Writers emit JSON
with `json.dumps(..., ensure_ascii=True, indent=2)`, TOML and YAML from small stdlib
renderers that quote every string, and golden files pin the output (REQ-BOX-60).

The six drafts above were written into a hardened box (`--network none`, read-only rootfs,
tmpfs home) and each CLI that has a list command parsed them: `codex mcp list` showed `hal
hal-mcp-bridge enabled`, `qwen mcp list` showed `hal: hal-mcp-bridge (stdio)`, `opencode mcp
list` showed `hal` (failing only because the test bridge was `cat`), and `opencode models
hal` printed `hal/hal-default` [verified]. `claude mcp list` does not take `--mcp-config`
[verified: `error: unknown option`]; the docker test uses `claude --mcp-config ... -p` with
`FakeModelServer` instead.

State: `$HOME` is tmpfs, so CLI histories and sessions vanish at exit (open question Q1).

#### Terminal filter

With `-t`, a box process that writes to its TTY writes to the user's terminal emulator.
Escape sequences then act outside the box: OSC 52 sets the clipboard (a planted command
pasted into a host shell runs as the user), OSC 8 makes hidden links, and DCS, APC and PM
reach terminal features such as DECRQSS replies or terminal-specific commands. That is a way
out that is neither socket, so `hal box` and `hal-sandbox shell` never hand the box the host
terminal (REQ-BOX-78).

```python
# halsandbox.ptyfilter
SEQ_MAX_BYTES = 4096      # longer than any CSI or title a TUI sends; a longer one is dropped whole
TITLE_MAX_CHARS = 256     # window titles are one line

class TerminalFilter:
    """Byte-stream filter for box output; keeps state across reads."""
    def feed(self, data: bytes) -> bytes: ...
    def flush(self) -> bytes: ...          # at EOF; an unfinished sequence is dropped

def relay_tty(argv: Sequence[str], *, raw: bool = False) -> int: ...   # returns the child's exit code
```

`relay_tty` opens a pty pair (`os.openpty`), copies the user's window size to it
(`TIOCSWINSZ`), starts `docker run -t ...` (or the bwrap argv) with the pty's slave side as
stdin, stdout and stderr, puts the user's terminal in raw mode, and then relays: user
keystrokes go to the master unchanged; master output goes through `TerminalFilter.feed` to
the user's terminal. SIGWINCH copies the new size to the pty, and the docker client passes it
on. The terminal mode is restored in a `finally` and on SIGTERM. Without a TTY on the user's
side there is no `-t` and nothing to filter (the CLI's output is then a byte stream to a
pipe or file).

Filter rules (input is decoded as UTF-8 incrementally; invalid bytes become U+FFFD so no
partial sequence slips through):

| Input | Output |
| --- | --- |
| Printable characters (U+0020 and up, except U+007F to U+009F) | passed |
| `\t`, `\n`, `\r`, `\b` | passed |
| Other C0 controls, DEL, C1 controls U+0080 to U+009F (8-bit CSI, OSC, DCS included) | dropped |
| `ESC [` parameters (0x30 to 0x3F), intermediates (0x20 to 0x2F), final (0x40 to 0x7E) | passed, except `CSI 20 t` and `CSI 21 t` (title reports, which echo text back as input) |
| `ESC` intermediates (0x20 to 0x2F), final 0x30 to 0x7E, other than the string starters below (`ESC 7`, `ESC 8`, `ESC M`, `ESC ( B`, `ESC =`) | passed |
| `ESC ] 0 ;` or `ESC ] 2 ;` text, ended by BEL or `ESC \` | passed with the text through `halkit.termsafe.strip_terminal_controls`, cut to `TITLE_MAX_CHARS`, always ended with `ESC \` |
| Any other OSC (52 clipboard, 8 hyperlink, 7, 1337, ...) | dropped with its payload |
| DCS `ESC P`, SOS `ESC X`, PM `ESC ^`, APC `ESC _`, each up to `ESC \` | dropped with its payload |
| A sequence longer than `SEQ_MAX_BYTES`, or a CSI with a byte outside its ranges | dropped; the filter returns to text |

The filter keeps one state between reads, so a sequence split across two reads is handled
like a whole one (`test_sequences_split_across_reads`). Keystrokes are not filtered: replies
the terminal sends (cursor position reports) must reach the box for TUIs to work, and the
title reports that could echo text into the box's input are the ones dropped above.

`[box].raw_terminal = true` passes the box's output straight through (for a CLI that needs a
dropped feature, such as OSC 52 copy); `hal box` and `hal-sandbox shell` then print
"raw terminal: the box can write to your clipboard and terminal" at every start and log a
`raw_terminal` event (REQ-BOX-43).

### Commands

The `hal` command tree is 06-core-cli's. This spec provides the behaviour behind it and a
standalone `hal-sandbox` script (host), so the package works without halcore:

```text
hal box image build [NAME] [--no-cache] [--json]   (hal-sandbox build)
hal box image status [--json]                      (hal-sandbox status)
hal box harnesses
hal-sandbox doctor [--json] [--refresh-probe]      also a section of `hal doctor` (06)
hal-sandbox gc [--dry-run] [--json]                the sweep; also run at `hal` start, hourly
hal-sandbox ls [--json]                            sessions, boxes, isolated worktrees, checkpoints
hal-sandbox shell [--dir DIR] [--isolated]         bash in a box (kind shell), relay on
hal-sandbox undo SID [N]                           restore checkpoint N (default 0) of a session
hal-sandbox diff SID [N]                           print the DiffReport since checkpoint N
hal-sandbox deps [DIR] [--json]                    dependency prep (behind `hal code --deps`)
hal-sandbox forget [DIR]                           drop the project's per-project choices
```

Flags that 06-core-cli adds to `hal code`, `hal acp` and `hal -p` for this spec's behaviour:
`--deps` (run the dependency prep before the session, REQ-BOX-71), `--unmask` and `--mask`
(REQ-BOX-69, REQ-BOX-70), `--no-checkpoint` (REQ-BOX-49).

```python
# halsandbox.images
def build(name: str = "sandbox", *, no_cache: bool = False) -> ImageInfo: ...
def status() -> list[ImageInfo]: ...
@dataclass(frozen=True)
class ImageInfo:
    name: str            # "sandbox", "sandbox-slim", or a CLI name
    tag: str
    image_id: str | None
    size_bytes: int | None
    built: str | None    # UTC Z
    current: bool        # tag matches this version's context hash

# halsandbox.doctor
def checks(*, refresh_probe: bool = False) -> list[Check]: ...
@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    severity: Literal["error", "warn", "info"]
    detail: str
    fix: str
```

Exit codes: 0 ok, 1 operation failed, 2 config or usage error, 3 backend unavailable or
image missing (matches 06-core-cli's headless exit 3).

Output safety (REQ-BOX-79): file names, patch text, diff stats and flags, CLI stderr tails
and masked paths come from the box or the work tree, so the text renderers in `cli.py` pass
each such string through `halkit.termsafe.strip_terminal_controls` before it reaches a TTY,
and `--json` writes `json.dumps(..., ensure_ascii=True)`. A file whose name
holds an OSC 52 sequence lists with no escape sequence left (what remains is 01-security's
rule for `strip_terminal_controls`). `hal-sandbox shell` uses the terminal filter like
`hal box`.

### Configuration

All keys are registered with `halkit.config` (strict: unknown keys fail, REQ-SYS-06).

```toml
[sandbox]
backend = "auto"            # auto | docker | bwrap | none
runtime = "auto"            # auto | runc | runsc (docker only)
require = false             # true: refuse to run with backend none
docker = "docker"           # docker CLI; env HAL_DOCKER overrides (tests use a fake)
bwrap = "bwrap"             # env HAL_BWRAP overrides
image = "auto"              # auto | "<ref>@sha256:<digest>" built FROM hal-sandbox
flavor = "full"             # full | slim
node = false                # true: node and npm in the sandbox image (npm projects, npm ci prep)
extra_apt_packages = []     # added to the image at build time
env_passthrough = []        # extra env names copied into the box (no secret-like names)
mount_uv_cache = false      # bind ~/.cache/uv read-only at /home/hal/.cache/uv
mask = "default"            # "default" = 01-security's MASK_DEFAULT; or a list; [] turns masking off

[sandbox.deps]
registries = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
              "index.crates.io", "static.crates.io", "crates.io"]
timeout_s = 900

[sandbox.limits]
memory = "auto"             # auto = min(8 GiB, RAM/4); or "6g"
cpus = 0                    # 0 = min(4, nproc/2)
pids = 1024
nofile = 8192
tmp_size = "1g"
home_size = "512m"
stop_timeout_s = 10
oom_score_adj = 500

[sandbox.transport]
max_message_bytes = 4194304
max_connections = 8
partial_timeout_s = 30

[sandbox.checkpoints]
enabled = true
max_file_bytes = 52428800          # 50 MiB
nongit_max_total_bytes = 536870912 # 512 MiB
keep_sessions = 20
max_age_days = 14
git_timeout_s = 60

[sandbox.isolated]
max_copy_bytes = 2147483648        # 2 GiB

[box]
auto_approve = true
model = ""                  # empty = "hal-code"
max_output_tokens = 8192
relay_port = 8741
raw_terminal = false        # true: box output reaches the terminal unfiltered (warned)
```

| Key | Reason for the default |
| --- | --- |
| `backend = "auto"` | Works on every host; the strongest available backend wins |
| `runtime = "auto"` | runsc when installed and working, runc otherwise (brief) |
| `require = false` | The brief keeps a degraded no-backend mode; users who want fail-closed set it |
| `flavor = "full"` | A coding agent compiles and tests; +360 MB on disk is cheap next to model files |
| `node = false` | +192 MB for every user, and most Python projects never need it; npm projects turn it on once |
| `mount_uv_cache = false` | The cache may hold packages from private indexes; opt-in only |
| `mask = "default"` | 01-security's list: its hard-deny file patterns plus common token files (RESOLUTIONS "Masking", Decision D31); a list replaces it, `[]` turns masking off (checkpoints still leave out the hard-deny file patterns, REQ-BOX-45) |
| `deps.registries` | The public registries of the three supported managers and their download hosts; a private index is added here by the user |
| `deps.timeout_s = 900` | Bounds a stalled download while leaving room for a cold install of a large lockfile [unverified: measure a cold `npm ci` and `uv sync` of a large project through the proxy] |
| `max_message_bytes` | Matches the `mcp` 2.x SDK request cap, so the broker never sees a message its SDK would reject |
| `max_connections = 8` | One bridge per MCP client; 8 covers an agent plus sub-agents with room to spare |
| `partial_timeout_s = 30` | No legitimate client takes 30 s to write one line |
| `max_file_bytes = 50 MiB` | Keeps datasets and model files out of `.git/objects` |
| `nongit_max_total_bytes = 512 MiB` | A shadow checkpoint of more than that per session is a sign of a data folder, not a project |
| `keep_sessions = 20`, `max_age_days = 14` | Two weeks of undo for recent sessions without unbounded ref growth |
| `max_copy_bytes = 2 GiB` | A copy above that takes minutes on a laptop disk |
| `auto_approve = true` | The box is the permission boundary (brief); prompts inside it add fatigue, not safety |
| `raw_terminal = false` | Box output can set the clipboard and drive the terminal (REQ-BOX-78); a CLI that needs a dropped feature is the exception, so it is opt-in with a warning |
| `relay_port = 8741` | Same number as the host gateway, so box and `hal connect` configs look alike (00-overview) |

### Interfaces used from other specs

| From | Interface | Used for |
| --- | --- | --- |
| 00-overview `halkit` | `paths.runtime_dir` (one rule, RESOLUTIONS F6), `session_dir`, `ensure_private_dir`, `check_socket_path`, `config`, `files.write_atomic`, `files.append_jsonl`, `proc.run`, `timeutil.iso_z`, `fsafe.open_beneath`, `fsafe.is_beneath`, `termsafe.escape` and
`termsafe.strip_terminal_controls` (rules in 01-security), `netguard` (address checks for the egress proxy), `testing.FakeModelServer/FakeBroker/TempHalHome/FakeResolver` | everything |
| 05-gateway | `SessionForwarder` and its constructor in "Python API" (local socket, or `[gateway].url` with a client token, D6), `GatewayClient.create_session/revoke_session`, `/v1/models` `context_length` (D5), role alias `hal-code` (D1) | model socket |
| 05-gateway | forwarder must match routes on the path without the query string | Claude Code calls `/v1/messages?beta=true` [unverified for 2.1.295: check with `FakeModelServer` request log in REQ-BOX-61] |
| 01-security | Policy, approvals and audit that `halsandbox.broker.Broker` wires: `halhub.approvals` (`Approver`, `ApprovalRequest`, keys, the control socket at `runtime_dir()/control/<sid>.sock`), `Broker.on_human_message` (REQ-SEC-93), the guard and reviewer in `halhub`, the untrusted result envelope, `EventSink` audit, REQ-SEC-88 for upstream tool metadata, REQ-SEC-64 `PeerRule` semantics, the forwarder route allowlist (REQ-SEC-66), the "Workspace scan" pattern table, the active-workspace rule that reads `owner.json` (REQ-SEC-09), `MASK_DEFAULT` and the hard-deny file patterns (REQ-BOX-45, 69), and TB2, which lists the box's TTY as a crossing closed by REQ-BOX-78 (01 must add it) | broker socket, diff flags, masking, terminal filter |
| 04-hub | `halhub.mcp.serve_streams(hub, read, write, ServeOptions(approvals="callback", approver=...))` with 01's `Approver`; an external tool provider hook so broker upstream tools join the registry and policy path (04 must add it); the elicitation request shape of "Approvals over MCP" (`_meta` `hal/approval_id`, `hal/key`, enum `["once", "deny"]`) that REQ-BOX-77 accepts from an upstream | broker socket, upstreams |
| 07-remote | writes a `[mcp.servers.hal-desktop]` entry with `host = true` at setup | desktop tools on a laptop |
| 03-agent | `[mcp.servers.<name>]` (3.13.1); `hal-agent --acp [--toolset coding|assistant] --config /run/hal/agent.json` (3.16.3) with `unix:///run/hal/model.sock` and `unix:///run/hal/broker.sock`; `LiteMcpClient` on the broker socket; reads `result._meta.hal`; backend `none` path rules (REQ-AGT-37: open through `halkit.fsafe.open_beneath` and refuse paths whose target is listed in `session.json` `masked`, which 03 must state and test); the eval `BoxRunner` protocol (implemented by halcore over `Session`) | the agent in the box |
| 06-core-cli | `Session.start`, renders `agent.json` and run files (E4), starts the forwarder and spawns `agent_argv` (A7), `session.checkpoint()` and `session.broker.on_human_message(...)` before each prompt, the flags `--deps`, `--unmask`, `--mask`, `--no-checkpoint`, the no-backend banner and the unboxed assistant fallback over a `backend="none"` session (A1), `hal box` option parsing, `hal doctor` sandbox section; before `Session.start`, the check that the work tree holds no registered module source root or editable `hal-*` distribution (06 and 04 own it; halsandbox mounts what it is given); the terminal-output rule for the `hal` commands that REQ-BOX-79 mirrors | UI |

### Startup latency

Measured with `bench.py` (10 runs after one warm-up, `perf_counter` around
`subprocess.run`) on this machine: Docker 29.2.1, runc 1.3.4, kernel 7.0.0-38, 24 cores
[verified: 2026-10-23]:

| Case | Median | Min | Max |
| --- | --- | --- | --- |
| `docker run --rm alpine:3.22 true`, no extra flags | 415 ms | 414 | 465 |
| same with the hardening flags | 314 ms | 314 | 315 |
| full argv (hardening, five mounts, two tmpfs, labels) on the tool image, `true` | 314 ms | 314 | 315 |
| full argv, `python3 -c pass` | 289 ms | 214 | 364 |
| bwrap with the same mounts, `true` | 7 ms | 7 | 8 |
| bwrap, `python3 -I -c pass` | 16 ms | 16 | 32 |
| host `python3 -I -c pass` (baseline) | 15 ms | 7 | 16 |

Importing `mcp.client.stdio` and `mcp.client.session` takes 0.36 s on the host
[verified: three timed runs]. Budget for `hal code` with runc: about 0.3 s container start
plus about 0.4 s imports plus agent setup, within the 1.5 s target (REQ-BOX-64). A hardened
runsc container (`--network none`, read-only, cap-drop ALL, no-new-privileges, `--user
1000:1000`) running `alpine true` took 0.26 s, the same as runc [verified: brief "Host
facts", 2026-10-23]; the full argv on runsc is measured by `scripts/bench-sandbox.py` once
runsc is registered with `--host-uds=open`. The per-turn cost inside a running box is a
socket hop, not a container start; REQ-SYS-16 (150 ms TTFT overhead) is measured by the e2e
benchmark.

## Failure modes

| Layer | Failure | Behaviour | Stance |
| --- | --- | --- | --- |
| Detection | Docker CLI missing, daemon down, no permission, and `last-backend` is not `docker` | `auto`: bwrap, then none, each with a banner naming the cause; explicit `docker`: error with the fix (start the daemon, join the group, or use rootless) | Degraded (auto) / closed (explicit) |
| Detection | Docker down but `last-backend` says docker | Session refuses to start, naming the daemon and `--backend bwrap` or `--backend none` for this run (REQ-BOX-02) | Closed |
| Detection | runsc listed but probe fails (this host until `--host-uds=open` is registered) | `auto`: runc with a status note naming `--host-uds=open`; `runtime = "runsc"`: error | Degraded / closed |
| Detection | bwrap blocked (no AppArmor profile, userns disabled) | none, after the per-project consent, with banner; doctor names the sysctl and profile | Degraded |
| Detection | none and `require = true` | Session refuses to start | Closed |
| Detection | none, no recorded consent, no terminal | Session refuses to start (REQ-BOX-68) | Closed |
| Masking | More than 256 matches, or the walk exceeds 10 s | Session refuses to start, naming `--unmask` and `[sandbox].mask` | Closed |
| Git config | Credentials in `.git/config` or `config.worktree` | Sanitized copy bound over it, notice naming the keys (REQ-BOX-15) | Closed |
| Git config | Sanitized copy still matches, or `git config` fails | Session refuses to start, naming the file | Closed |
| Terminal | Box writes OSC 52, OSC 8, DCS, APC, PM or SOS | Dropped by the terminal filter; the rest of the output is unchanged (REQ-BOX-78) | Closed |
| Terminal | `[box].raw_terminal = true` | Output unfiltered, warning at every start, event logged | Open (opt-in) |
| Terminal | Host `hal` killed while the terminal is raw | Terminal left in raw mode; `reset` fixes it (the lifeline still ends the box) | Closed (cosmetic) |
| Upstream | Elicitation without `hal/approval_id` and `hal/key`, or with no call in flight | Declined unseen and audited (REQ-BOX-77) | Closed |
| Deps | Approval denied or timed out | Nothing runs; nothing recorded | Closed |
| Deps | Install fails, times out, or needs an unlisted host | Prep box stopped; result names the exit code, log tail and refused hosts; checkpoint taken before the prep lets `/undo` revert partial writes | Closed |
| Deps | Manager missing from the image (`npm` without `node = true`, `cargo` without the apt package) | Error naming the config key; nothing runs | Closed |
| Upstream | Host MCP server fails to start or crashes | Its tools return `status = "unavailable"`; one restart per `[mcp].restart_limit`; the broker keeps serving other tools | Closed |
| Image | Image missing or tag points elsewhere | Error naming `hal box image build`; TUI offers to build; never pulls at run time | Closed |
| Image | Build fails (network, apt, npm) | Command exits 1 with the failing step; previous image kept | Closed |
| Image | Custom image not built FROM hal-sandbox | Session refuses it | Closed |
| Session dir | Socket path over 107 bytes | Error before any process starts, naming `HAL_RUNTIME_DIR` | Closed |
| Session dir | `$R` owned by another user or wrong mode | `UnsafePathError` from halkit | Closed |
| Transport | Broker thread crashes or socket closed | Bridge exits 0 or 69; the agent gets tool errors and continues without host tools | Closed |
| Transport | Oversize or slow message | Error frame, connection closed; client reconnects | Closed |
| Model | Gateway down | Forwarder answers 502 (05-gateway); agent reports it; no other route out | Closed |
| Model | Session token rejected | Forwarder event; halcore re-mints and calls `set_token` (REQ-GW-64) | Closed until re-minted |
| Box | Lifeline lost (host killed) | Box terminates its child, exits 143, `--rm` removes it | Closed |
| Box | OOM kill or crash | Exit 137 or the child's code; session marked crashed, transcript and checkpoints kept | Closed |
| Box | docker client killed but box alive | Lifeline ends it; the sweep removes any survivor | Closed |
| Sweep | docker unavailable | Logged; nothing removed; retried next start | Open (cleanup only) |
| Checkpoint | git error, timeout, disk full | Turn does not start; UI offers `--no-checkpoint` | Closed |
| Checkpoint | Non-git tree over the cap | Session refuses to start without `--isolated` or `--no-checkpoint` | Closed |
| Restore | User edited files since the checkpoint | Restore overwrites them; the safety checkpoint taken first lets the user undo the restore | Closed (recoverable) |
| Isolated | Patch does not apply | Nothing applied, patch saved, message names the path | Closed |
| Isolated | High flags present | Accept refused unless the user confirms each flag | Closed |
| Paths | Symlink escape, FIFO, device | `UnsafePathError` from `halkit.fsafe`, `WorkspaceEscape` from `box_to_host`, or a refusal from `read_beneath`; the caller gets an error result | Closed |
| Deps | `--deps` on bwrap or `none` | Error naming docker (Decision D30) | Closed |
| hal box | Non-docker backend | Error | Closed |
| hal box | CLI fails offline (missing asset) | CLI exits; `hal box` prints the CLI's stderr tail and `hal-sandbox doctor` | Closed |

The box itself is fail-closed by construction: with no network namespace route, a failure of
any host-side component removes a capability; nothing failing opens a path out.

## Migration

Today's code has no sandbox [verified: `grep -i 'docker\|bwrap\|sandbox'` over `hal`,
`hal-serve`, `setup.sh` and `halcore/*.py` finds only `hal eval`'s temp-config "sandbox" in
`halcore/evaluate.py:78-120`, which is a different thing and keeps its name].

| Today | Becomes |
| --- | --- |
| `hal here` (`hal:117-118`), `exec claude` with env redirection (`hal:123-263`) | `hal box -- claude` (sandboxed, this spec) and `hal claude` (host, warned, 06) |
| The telemetry-off block (`hal:254-261`) | The claude writer's env set, now described as quiet offline operation |
| The proxy-to-127.0.0.1:9 "no network" trick (`hal:262-267`) | `--network none`; the trick stays only in `hal claude` and is no longer a security claim |
| `--permission-mode acceptEdits` injection (`hal:270-275`) | `[box].auto_approve` inside the box; `hal claude` keeps 06's behaviour |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` from preset files (`hal:162-168`) | `BoxContext.context_window` from the gateway's model metadata |
| `deep_work` running `claude -p` on the host (`halcore/brain.py:203-227`) | `core_long_task`, a hub tool in halcore's built-in module (tier `act`, RESOLUTIONS F2), running a sandboxed `hal -p` in a box of kind `job` |
| Project venvs that point at host interpreters (`<project>/.venv`; security review F3) | `hal code --deps` builds `/work/.hal/venv` with the box image's Python; the host `.venv` is left alone |
| An external batch harness's container labeller and cleanup | Ported idea: `halsandbox.labels` with uid and pidstart; no code is copied, the harness is not a dependency |

How existing users keep working: nothing in `~/.config/hal/config.toml` needs to change; all
new keys have defaults. `hal here` keeps working through the alias table (REQ-SYS-23), which
prints the new name. Users without Docker get bwrap on Ubuntu 24.04 and later (AppArmor
profile shipped) and the none backend elsewhere, after a one-time consent per project and
with a banner. First use of `hal code`
on docker needs one `hal box image build` (about a minute with network; 20 s for the tool
layer and 21 s for the toolchain layer in the test builds [verified: `time docker build`]).

Removed: the claim that the old launcher keeps a local model off the network; the
`acceptEdits` default for the host Claude Code path moves to 06 and its warning.

## Test plan

Hermetic unit tests (default run), one file per module in `packages/hal-sandbox/tests/`:

- `test_session.py`: runtime dir layout and modes, `owner.json` (every key present),
  `session.json` schema including `masked`, no secret values in box-visible files (scan for
  the fake token), service start order with fake services, teardown order, `Session.broker`
  exposed, kind `chat` refused outside `$STATE/assistant/`.
- `test_detect.py`: auto order, explicit failures, runsc probe gating and the recorded peer
  uid, refusal when docker is down and `last-backend` says docker, cache expiry, daemon mode
  parsing from canned `docker info` JSON (rootful, userns, rootless, no AppArmor; runtimes
  `runc nvidia runsc runsc-gpu` as on this host).
- `test_docker_argv.py`: golden argv per kind (including `deps`) and daemon mode
  (`tests/golden/docker_<kind>_<mode>.txt`), forbidden flags, CSV quoting with `,` `"` `:`
  `=` and rejection of NUL and newline, mount list exactness with mask mounts, `-t` only
  with a TTY, the prep box without broker and model sockets.
- `test_bwrap_argv.py`: golden argv; merged and split `/usr`; scope wrapper on and off; mask
  binds.
- `test_backend_none.py`, `test_env.py` (including the prep box env and the absence of
  offline variables there only), `test_limits.py`, `test_gitlayout.py` (dir, absolute
  gitfile, relative submodule gitfile, symlinked `.git`; credential detection and removal for
  userinfo with a password, an `https` token user name, `ssh://git@` kept, `extraHeader` per
  URL, `credential` sections, userinfo in a section name, a copy that still matches is an
  error), `test_labels.py`, `test_events.py`,
  `test_doctor.py` (including the `daemon.json` `--host-uds=open` check), `test_images.py`
  (rendered Dockerfiles with and without node, context hash stability, pins file
  completeness including `uv`, wheel version mismatch error, never `--network` at run).
- `test_transport.py`: framing, partial reads across packets, 4 MiB boundary (exactly the
  limit passes, one byte over fails), error frame then close, slowloris timeout with a
  0.2 s test value, connection cap, unchanged pass-through, `anyio_streams` with a minimal
  `mcp` server answering `tools/list`.
- `test_broker_wiring.py`: box connections served with `approvals="callback"`, no
  elicitation request ever written to a box connection.
- `test_broker_meta.py`: `_meta.hal` per status (`ok`, `error`, `denied`, `unavailable`),
  `untrusted` and `guard` pairs, no approval id.
- `test_box_entry.py`, `test_relay.py`, `test_bridge.py`: run the stdlib modules on the host
  with temp sockets: seed copy (no symlink following), lifeline EOF kills a child that
  ignores SIGTERM after the grace, exit code passthrough, relay streams spaced SSE from a fake
  upstream with gaps preserved within 50 ms, 33rd relay connection closed, `--relay-socket`,
  bridge byte fidelity and exit codes.
- `test_boxside_imports.py`: AST scan, stdlib only (`sys.stdlib_module_names`).
- `test_paths.py`: `box_to_host` escapes (NUL, `..`, `/workx` prefix confusion, other
  absolute paths) and `read_beneath` refusing FIFOs and devices; the walk itself is
  `halkit.fsafe`'s and is tested there (`packages/hal-kit/tests/test_fsafe.py`, vectors in
  `packages/hal-kit/tests/data/path_cases.json`).
- `test_mask.py`: `MASK_DEFAULT` is 01-security's object (identity, not a copy), nested
  matches, a secret under a git-ignored directory is masked, the five skipped directories,
  symlink inside and outside the tree, the 256-match cap, the walk timeout with a fake clock.
- `test_projects.py`: unmask remembered and revoked, backend-`none` consent asked once,
  refusal without a terminal, `forget`, file mode 0600.
- `test_deps.py`: lockfile detection, commands per manager, offer only on a changed sha256,
  approval request fields (tool, pack, tier, arguments), denied approval runs nothing,
  timeout stops the prep box, checkpoint before prep, missing manager errors.
- `test_egress.py`: `CONNECT` to a listed host on 443 tunnels to a fake upstream; other
  ports, methods and hosts get 403; a listed host resolving to a private address is refused
  (`FakeResolver`); the connect uses the checked address; idle and head timeouts.
- `test_boxcli_<cli>.py` (six files): golden home files, env and argv for auto-approve on and
  off; no value matching the fake gateway token.
- `test_box.py`: docker-only check, checkpoints at start and exit, `--ask`, `start_model` called
  before the box and closed after it, `HAL_HARNESS=1` in the box env.
- `test_upstreams.py`: a stub upstream script speaking MCP on stdio, configured as an 03-agent
  `[mcp.servers.<name>]` entry with `host = true`; tool names `<name>__<tool>`, pack
  `mcp-<name>`, REQ-SEC-88 metadata applied, annotations ignored, `tools_allow` and
  `tools_deny`, crash yields `unavailable` results and `restart_limit` restarts, entries
  without `host` never started; an elicitation with 04-hub's `_meta` during a call becomes an
  `ApprovalRequest` with pack `mcp-<name>`, scopes `(ONCE,)`, `content = True`, an escaped
  `display` (a message holding ESC and U+202E shows them as `\u` escapes) and a key that
  changes when the upstream id or key changes, and the answer echoes the upstream id and key
  for allow and deny; elicitations without the `_meta`, with a malformed id, in URL mode or
  with no call in flight are declined, never reach the fake `Approver`, and produce one
  audit event each.
- `test_ptyfilter.py`: OSC 52 (BEL and ST ended), OSC 8, OSC 7, DCS, APC, PM, SOS and 8-bit
  C1 forms are dropped with their payloads; colours, cursor moves, alternate screen,
  bracketed paste and `ESC ( B` pass byte for byte; OSC 0 and 2 titles pass with controls
  stripped and length capped; `CSI 21 t` dropped; every sequence split at every byte offset
  across two `feed` calls gives the same output; an unterminated OSC over `SEQ_MAX_BYTES` is
  dropped and later text passes; `raw = True` passes everything; `relay_tty` restores the
  terminal mode after the child exits (pty-based test, no docker).
- `test_cli.py`: for `hal-sandbox diff`, `undo`, `ls`, the `hal box` exit summary, the stderr
  tail and the masking notice, a file name or text holding OSC 52 reaches the fake TTY
  without ESC, and `--json` output is ASCII.

Integration tests (real git, default run): `test_gitops.py` (nested repo fsmonitor marker
never created across take, diff, restore; hooks never run; inherited `GIT_*` variables
removed; `cwd` outside the work tree; timeout raises `GitError`), `test_checkpoints.py`
(including the ignored-file report of ATK-43), `test_diffreport.py` (every pattern of
01-security's "Workspace scan" table), `test_shadow.py`, `test_isolated.py` (including a
hostile patch set from the `ap` experiment: `.git/` paths, `..`, beyond-symlink, outward
symlink flagged high). All use `TempHalHome` and repos under `tmp_path` with
`GIT_CONFIG_GLOBAL=/dev/null`.

Marked tests:

- `docker`: `test_docker_live.py` (REQ-BOX-09 to 17, 34, 37, 40, 41, 45, 53, 65, 69), marked
  `runtime` where the requirement names both runtimes, `test_images_live.py` (selftest, sizes
  with `slow`, no pull), `test_box_live.py` (per CLI list check; round trip with `slow`; the
  OSC 52 and DCS check of REQ-BOX-78 through `relay_tty`), and
  the cross-package `tests/e2e/test_box_secrets.py` and `tests/e2e/test_flow_box.py`. They
  build the `slim` flavor once per session as `hal-sandbox:test-<ctx>`; this replaces
  00-overview's earlier `hal-box-test` image (Decision D12, as 00-overview's test plan now
  says).
- `docker` + `network`: `test_deps_live.py` (a tiny `uv.lock` project and a tiny
  `package-lock.json` project prepared through the real proxy; writes only under `/work`;
  an unlisted host refused).
- `bwrap`: `test_bwrap_live.py` (probe, confinement checks as in the verified list, socket
  through ro bind, masked file reads empty).
- `systemd`: `test_bwrap_live.py::test_scope_limits_apply` (`systemd-run --user --scope`
  with a 64 MiB memory cap).
- `slow`: `scripts/bench-sandbox.py` (REQ-BOX-64), image size test, CLI round trips.

Fakes: `HAL_DOCKER` and `HAL_BWRAP` point at `tests/fakes/docker.py` and `bwrap.py`, which log
argv as JSON lines and answer from `tests/fakes/responses/` (canned `docker info`, `version`,
`ps`, `image inspect`), following the common environment-variable seam for the `docker` binary; `FakeModelServer`,
`FakeBroker` and `FakeResolver` from `halkit.testing`; a fake `start_model` callable that
records start and close order; a fake `Approver` that answers from a script; a
`FakeGitRepo` fixture helper that builds repos with staged, unstaged, ignored, nested and
symlinked content.

Requirement coverage: every test names the REQ ids it proves in its docstring; the
cross-package `tests/test_docs.py` (REQ-SYS-19) checks each REQ-BOX id appears in at least
one test.

## Implementation plan

Each step is one commit, tests green at every commit, subject in `<Verb> <object>` form
(60 characters or fewer). Docs for a behaviour land in the commit that adds it; steps 33 and
34 add the overview pages. Prerequisites: 00-overview phases 0 and 2 (halkit, including
`fsafe` and `netguard`; skeleton package); 01-security's `halhub.approvals` and broker
policy for step 21 and 04-hub's `serve_streams` for step 13 (fakes stand in until they land);
05-gateway `SessionForwarder` for the end-to-end tests; 04-hub's external-provider hook for
step 22.

1. `Add sandbox config tables with documented defaults` - `config.py`, limits resolution,
   validation (tmpfs below memory, apt names, env names, mask patterns); tests.
2. `Add session runtime dirs with short socket paths` - `RuntimeDir`, `owner.json`,
   `session.json`, passwd and group generation, the `empty` mask source; tests.
3. `Add box environment allowlist` - `env.py`; tests.
4. `Add box path translation over halkit.fsafe` - `paths.py`; tests.
5. `Add per-project sandbox choices` - `projects.py`; tests.
6. `Add hardened git runner for host-side git` - `gitops.py`; nested-repo regression test.
7. `Add git checkpoints with temporary index` - take, list, dedupe, skip list, ignored list;
   tests.
8. `Add checkpoint diff report with risk flags` - `diffreport.py` with 01's scan table; tests.
9. `Add checkpoint restore with safety checkpoint` - restore, `not_restorable`, prune; tests.
10. `Add shadow-repo checkpoints for non-git folders` - `shadow.py`; tests.
11. `Add workspace secret masking` - `mask.py`; tests.
12. `Add lifeline socket for box shutdown` - `lifeline.py`; tests.
13. `Add line-framed unix socket server for broker` - `transport.py` with anyio adapter,
    `PeerRule` uid and cgroup checks; tests.
14. `Add box entry, relay and MCP bridge scripts` - `boxside/`, console scripts, import scan;
    tests.
15. `Add git layout discovery for read-only mounts` - `gitlayout.py`; tests.
16. `Add hardened docker argv builder` - `backends/docker.py`, golden files, mask mounts;
    tests.
17. `Add bubblewrap backend` - `backends/bwrap.py`, scope wrapper; tests and `bwrap` marker.
18. `Add none backend with confined capabilities` - `backends/none.py`, consent check; tests.
19. `Add backend detection with probe cache` - `detect.py`, runsc probe, docker-down refusal,
    fake docker and bwrap; tests.
20. `Add broker result metadata` - `broker/meta.py`; tests.
21. `Add sandbox session lifecycle` - `session.py` `Session`, broker wiring with
    `approvals="callback"`, run files, teardown order, events; tests.
22. `Add broker upstreams for host MCP servers` - `broker/upstreams.py` reading 03-agent's
    `[mcp.servers]`, hub provider hook; tests.
23. `Add container labels and orphan sweep` - `labels.py`; tests.
24. `Add sandbox image template and build command` - `images/`, `images.py`, pins,
    `requirements-box.txt`, `requirements-tools.txt`, node option, selftest; unit tests and
    `test_images_live.py`.
25. `Add docker confinement tests` - `test_docker_live.py` with runc and runsc variants.
26. `Add isolated worktree mode with accept and reject` - `isolated.py`; tests.
27. `Add egress proxy for dependency prep` - `egress.py`; tests.
28. `Add dependency prep box` - `deps.py`, `hal-sandbox deps`; unit tests and
    `test_deps_live.py`.
29. `Add box images for agent CLIs with lockfiles` - `images/box/<cli>/`, goose sha256;
    build tests.
30. `Add box config writers for claude, qwen, opencode` - three profiles, golden files.
31. `Add box config writers for codex, crush, goose` - three profiles, golden files.
32. `Add hal box commands and hal-sandbox script` - `box.py`, `cli.py`, doctor, ls,
    shell, undo, diff, forget; `test_box_live.py`; `docs/user/sandbox.md`.
33. `Add sandbox how-tos and developer notes` - `docs/howto/run-an-agent-cli-in-the-box.md`,
    `docs/howto/undo-agent-changes.md`, `docs/howto/install-gvisor.md` (registering runsc with
    `--host-uds=open`), `docs/howto/prepare-project-dependencies.md` (each ending with a
    symptom -> fix table), `docs/dev/sandbox.md` (measurements, residual risks), index rows.
34. `Add sandbox startup benchmark script` - `scripts/bench-sandbox.py`, recorded numbers in
    `docs/dev/sandbox.md` for runc and runsc.

Steps 35 to 38 change code from steps 15 to 32, so they follow them; each updates
`docs/user/sandbox.md` or `docs/dev/sandbox.md` for its behaviour.

35. `Add sanitized git config for box mounts` - `gitlayout.sanitize_config`, the copy binds in
    both argv builders, `git_config_sanitized` in `session.json`, the notice; unit tests and
    `test_git_config_token_not_readable_in_box` (REQ-BOX-15).
36. `Add terminal filter for box and shell TTYs` - `ptyfilter.py`, `relay_tty` used by
    `box.run` and `hal-sandbox shell`, `[box].raw_terminal` with its warning; unit and docker
    tests (REQ-BOX-78).
37. `Map upstream elicitations to host approvals` - `broker/upstreams.py` elicitation callback,
    decline and audit path; tests (REQ-BOX-77).
38. `Strip terminal controls in sandbox command output` - text renderers in `cli.py` and
    `box.py`, ASCII `--json`; tests (REQ-BOX-79).

## Decisions

- **D1 `/run/hal` is mounted read-only.** The brief says "(rw)". A read-only bind still
  allows `connect()` on its sockets, and `unlink` of a socket then fails with EROFS
  [verified: box test]. The box never needs to create files there, and read-only stops it
  from replacing a socket or planting files the host might later read. RESOLUTIONS E10 makes
  this binding; 00-overview and 01-security (REQ-SEC-62) follow.
- **D2 Sessions live under `$XDG_RUNTIME_DIR`.** The 107-byte `sun_path` limit rules out
  state or scratch paths [verified]; `$R` is `halkit.paths.runtime_dir()` with 00-overview's
  one rule (RESOLUTIONS F6) and 12-hex ids.
- **D3 Generated `/etc/passwd` and `/etc/group`.** Without them `getpwuid` fails for the
  host uid [verified]; an image-baked user cannot match every host uid.
- **D4 A lifeline socket in addition to stdio.** A SIGKILLed host leaves boxes running when
  the child ignores stdin [verified]; the lifeline ends them within one read, and the
  labelled sweep catches anything left.
- **D5 Every host git call is hardened.** A nested repo's `core.fsmonitor` executed on the
  host through a parent `git status` [verified]; `-c core.fsmonitor=false` propagates to child
  git processes and blocks it. Explicit `--git-dir`/`--work-tree` and a scrubbed `GIT_*`
  environment follow 01-security REQ-SEC-67.
- **D6 Shadow git repository instead of a tarball for non-git checkpoints.** A shadow repo
  stores each version once (deduplicated objects), reuses the git restore and diff code and
  its flags, and costs one `git add` per turn instead of a full archive per turn. The size cap
  and the `--isolated` alternative from the brief stay. Settled by RESOLUTIONS E8.
- **D7 `hal box` is docker-only.** The CLIs need node and pinned packages from an image;
  binding a host node install into bwrap would expose `~/.npm`, `~/.local` and other user
  paths.
- **D8 One image per CLI, built on demand.** All five npm CLIs in one image measured 2.79 GB;
  per-CLI images stay under 1.5 GB and users build only what they use.
- **D9 One framing for every broker client.** hal-agent connects to `broker.sock` with its
  stdlib `LiteMcpClient` (03-agent); third-party CLIs and stdio MCP clients in the box use
  `hal-mcp-bridge`, a byte pipe. Both produce the same newline framing, so the host has one
  transport and one set of tests.
- **D10 `SO_PEERCRED` uid check with a per-runtime expected uid.** 01-security requires the
  check (REQ-SEC-64). Under runsc the socket connection passes through gVisor's host-UDS
  support and the peer may be a gVisor process [unverified until runsc is registered with
  `--host-uds=open` on this host], so the expected uid comes from the runsc probe rather than
  being assumed, and the cgroup check applies to runc only; the 0700 directory stays the
  first layer. The runc cgroup check uses `--cidfile` to learn the container id.
- **D11 No token inside the box.** The socket is the capability; the forwarder adds the
  session token on the host side (matches 05-gateway and REQ-SYS-12).
- **D12 Base image `debian:trixie-slim` with Debian `python3`.** One package source for
  Python and tools; the test image is the `slim` flavor of the real image, so tests exercise
  the shipped Dockerfile.
- **D13 Markers `bwrap` and `systemd`.** bwrap tests need unprivileged user namespaces and the
  scope test needs a user manager, which CI runners and some distributions lack. 00-overview
  registers the full set `gpu docker network slow bwrap systemd live` (RESOLUTIONS E12).
- **D14 `/tmp` keeps exec.** `/work` is executable anyway; `noexec` on `/tmp` breaks build
  tools without blocking anything.
- **D15 Pinned CLIs and tools are at least 14 days old.** Gives the registry time to pull a
  compromised release before Hal pins it.
- **D16 Codex and other CLIs run with their own sandboxes off.** Nested user namespaces fail
  in the box [verified]; the box is the boundary.
- **D17 Requirement prefix REQ-BOX.** Settled by RESOLUTIONS E11; 00-overview's index uses it.
- **D18 No seccomp filter for bwrap in v1.** Building a BPF program needs libseccomp or a
  hand assembler; the bwrap backend is documented as weaker than docker.
- **D19 Command names and the Python facade follow 06-core-cli.** `hal box image build|status`,
  `hal box harnesses`, `halsandbox.Session`, `halsandbox.box.run` and `halsandbox.images` keep
  the names 06 assumed; maintenance commands live in the standalone `hal-sandbox` script and
  `hal doctor`, so the `hal` tree stays 06's.
- **D20 `Session.start` prepares, halcore spawns.** halcore owns the ACP pipes and is the only
  package that may import both halsandbox and halgateway, so it starts the forwarder on
  `model_socket` and then runs `agent_argv`; halsandbox stays free of gateway code
  (RESOLUTIONS A7).
- **D21 Dependency prep in a separate box, not network in the agent box.** The agent box
  keeps `--network none` at all times. A prep box runs only the lockfile's install command,
  after an `act` approval bound to the lockfile sha256, with no broker or model socket, so a
  model cannot steer it beyond choosing to ask for it (RESOLUTIONS F3).
- **D22 The prep box reaches registries through a CONNECT proxy on a unix socket.** It keeps
  the argv builder's rule that `--network` is always `none`, reuses `hal-relay`, and puts the
  allowlist and the private-address check on the host, where the box cannot change them. TLS
  is not intercepted, so the lockfile hashes plus TLS carry content integrity and Hal holds no
  CA key.
- **D23 Prep output lives in `/work/.hal/`, ignored by git.** The box can use it (the agent's
  environment points there), the user's own `.venv` and host interpreters are not touched,
  and checkpoints do not copy hundreds of megabytes of dependencies per turn.
- **D24 Masking binds an empty read-only file over each match.** It needs no change to the
  user's files, works the same on docker and bwrap, and cannot be undone from inside the box
  (a mount point cannot be removed or replaced). Files the model creates later are not
  masked, because it wrote them.
- **D25 Per-project choices are sandbox settings, not grants.** `projects.json` holds unmask,
  the backend-`none` consent and the last prepared lockfile hashes. None of them lets a tool
  call skip policy, which keeps RESOLUTIONS B3 ("no grant files on disk") intact.
- **D26 A stopped docker daemon refuses instead of degrading.** When the cached probe last saw
  docker, a silent fall to bwrap or `none` would drop isolation without the user noticing
  (RESOLUTIONS "Fallback").
- **D27 Upstreams come from 03-agent's table only.** One config location for user MCP servers
  (RESOLUTIONS E6); policy, not config, decides an upstream tool's tier, so a config entry can
  never relax policy for itself.
- **D28 `result._meta.hal` carries `status`, `untrusted` and `guard` only.** Approvals block
  in the broker, so there is no pending state to report (RESOLUTIONS E7, B5); `guard` reuses
  01-security's envelope values so the model-facing text and the metadata agree.
- **D29 Node is optional in the sandbox image.** +192 MB for every user against npm projects
  only; `[sandbox].node = true` turns it on once.
- **D30 Dependency prep is docker-only in this release.** The prep needs `uv` and pinned
  toolchains from the image; on bwrap the box sees the host's `/usr`, which has neither a
  pinned `uv` nor the interpreter a host venv points at, so a bwrap prep would build an
  environment the bwrap agent box cannot reproduce. bwrap users get an error naming docker.

- **D31 `MASK_DEFAULT` is a superset of the RESOLUTIONS list, defined once in 01-security.**
  RESOLUTIONS "Masking" names six patterns; 01-security's hard-deny file patterns already
  include `id_ecdsa*`, `credentials*.json` and `*.kdbx`, and `.npmrc`, `.pypirc`, `.netrc`,
  `.git-credentials`, `*.p12`, `*.pfx` and `*.tfstate` are where tokens usually sit. A box that
  could read a file the host tools refuse would make the hard-deny list pointless, so the
  mask default is that list plus the token files, imported from `halhub.policy` (security
  gaps review G06). The walk enters git-ignored directories because ignored is where secrets
  live.
- **D32 Checkpoints and isolated snapshots leave masked files out.** Their objects sit in the
  read-only `.git` the box can read, so a masked but untracked file would otherwise be one
  `git show` away (security gaps review G01). The cost is that `/undo` cannot bring back a
  deleted masked file; REQ-BOX-47 reports it as not restorable.
- **D33 The box sees a sanitized git config, not none.** Hiding `.git/config` would break
  `git status` and `git log` in the box; a copy without credentials keeps git working and
  leaves the user's file alone (G07). The copy is checked again after sanitizing so a missed
  form fails loudly instead of leaking.
- **D34 The box never owns the host terminal.** A filter on a host-owned pty costs one relay
  thread and keeps every TUI feature that CLIs use (colours, cursor, alternate screen, titles)
  while dropping the sequences that act outside the box: clipboard, hyperlinks and device
  control strings (G04). An allowlist, not a blocklist, because terminals keep adding OSC
  and DCS commands. `raw_terminal` exists for a CLI that needs a dropped feature and warns at
  every start.
- **D35 Upstream elicitations become local approvals bound to the upstream id and key.** The
  laptop user is the only one who can answer the desktop's question, so the broker shows it
  on a host surface; putting the upstream id and key into the local request's arguments makes
  the local key unique to that one remote request, and `content = True` keeps free text from
  the far side off voice and notifications (G15). Elicitations without Hal's `_meta` are
  declined, because the broker cannot tell what an unstructured one would approve.
- **D36 Terminal output of sandbox commands is stripped, not escaped.** `escape` is for
  approval displays, where the user must see every character; listings and summaries are
  read at a glance, and the rule for the `hal` commands that 06-core-cli owns names the same
  function (security gaps review G21).

## Open questions

- **Q1** `hal box` keeps no CLI state between runs (tmpfs home), so Claude Code and the other
  CLIs lose their session history at exit. Keep it ephemeral, or add an opt-in per-CLI state
  directory under `$STATE/box/<cli>/` bound at the CLI's data path (config files would still
  be rewritten at every start)?
