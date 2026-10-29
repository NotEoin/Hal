# hal-remote: using the desktop's Hal from a laptop over SSH

`hal-remote` is the optional add-on that lets a laptop (Linux or Windows) use the models and
tools of the desktop that runs Hal. It pairs the two machines with two restricted SSH keys,
keeps a supervised tunnel to the desktop's gateway, exposes the desktop's tools to any MCP
client over SSH stdio, and prints the settings each harness needs. The desktop never listens
on anything but loopback and sshd; every request still carries a Hal token.

Status: draft 2026-10-23, revised 2026-10-24 to follow
`~/src/.dev-notes/specs/RESOLUTIONS.md` (binding) and the brief's host facts.

## Scope and non-goals

This spec owns (per the index in [00-overview.md](00-overview.md)):

- the `hal-remote` distribution (`halremote` package) and its console scripts;
- pairing: key generation on the laptop, `authorized_keys` lines on the desktop, host key
  pinning, token delivery, revocation;
- the forced-command protocol on the desktop (`hal-remote serve`);
- `hal remote server-check` and the content of the sshd drop-in `scripts/host-setup.sh --ssh`
  writes;
- the tunnel supervisor (`up`, `down`, `status`) on Linux and Windows;
- `hal remote env` and `hal remote connect` for harnesses on the laptop;
- what runs where when a laptop is involved, including `hal code` on a laptop;
- the remote threat model, and the requirements the `remote` channel profile must meet
  (01-security owns the profile, its rule syntax and its defaults);
- documentation of SSH over a Tailscale tailnet for use away from the home network.

Non-goals:

- Reaching the desktop from outside the home network without a VPN. Hal documents running
  the same SSH pairing over Tailscale but does not set Tailscale up.
- Any Hal listener on a non-loopback address, TLS, mTLS or OAuth. SSH is the only network
  boundary (REQ-SYS-09).
- Remote desktop, file sync, or editing desktop files from the laptop's editor.
- A sandboxed agent on a native Windows laptop (00-overview non-goals). Windows laptops are
  clients only.
- A Tailscale backend inside `hal remote`, and Tailscale Serve in front of the gateway
  (Decision D17).
- macOS support. The Linux code path without systemd may work there; it is untested and
  undocumented in v1.
- SSH multiplexing (`ControlMaster`): Windows OpenSSH does not support it, and one
  connection per MCP session is simple to reason about.
- Forwarding the console (8740), the hub HTTP port (8742), gpu-light, TTS or hal-employment
  ports (Decisions D13 and D16).

## Requirements

Test kinds: **unit** (hermetic pytest, runs by default), **sshd** (pytest marked `slow`,
starts an unprivileged `sshd` on loopback; skipped when no `sshd` binary is found, see "Test
plan"), **docker** (marked `docker`), **windows** (unit tests that run on the Windows CI
job), **manual** (checklist in `docs/dev/remote.md`). Test files live in
`packages/hal-remote/tests/` unless a path is given.

### Keys and pairing

- **REQ-RMT-01** `hal remote setup` MUST create two distinct ed25519 key pairs, `tunnel` and
  `mcp`, with the system `ssh-keygen`, without passphrase, under
  `config_dir()/remote/<profile>/`, and MUST NOT read, reuse or modify any file in the user's
  `~/.ssh`. Test: unit (`test_keys.py` with `FakeRunner` checks argv and paths) and sshd
  (`test_sshd_pairing.py` with the real `ssh-keygen`).
- **REQ-RMT-02** `setup` MUST refuse to replace existing keys of a profile unless `--rotate`
  is given; `--rotate` MUST create new keys before the desktop line is replaced, so a failed
  rotation leaves the old pair working. Test: unit (`test_setup.py`).
- **REQ-RMT-03** On POSIX the profile directory MUST be 0700 and private keys, the token file
  and `profile.toml` MUST be 0600; on Windows `setup` SHOULD restrict the token file to the
  current user with `icacls` and MUST warn when that fails. Test: unit (`test_setup.py`,
  POSIX modes; `FakeRunner` for the `icacls` argv) and manual (Windows ACL check).
- **REQ-RMT-04** The tunnel key line MUST be exactly the options
  `restrict,port-forwarding,from="<pattern>",permitopen="127.0.0.1:<port>"...,permitlisten="127.0.0.1:1",command="/bin/false"`
  in that order, with one `permitopen` per forwarded port and only `127.0.0.1` targets.
  `permitlisten="127.0.0.1:1"` names a port a normal user cannot bind, so no TCP `-R`
  listener can open. Test: unit (`test_authkeys.py` golden) and sshd
  (`test_sshd_restrictions.py`).
- **REQ-RMT-05** The mcp key line MUST be
  `restrict,from="<pattern>",command="<abs hal-remote> serve --client <name> --hub <abs hal-hub>"`
  with both paths absolute, shell-quoted inside the option, and no `port-forwarding`. Test:
  unit (`test_authkeys.py` golden) and sshd (`test_sshd_restrictions.py`).
- **REQ-RMT-06** `from=` MUST default to a range derived from the client address seen by the
  desktop (rules in "from= derivation"); a public address MUST be refused unless `--from` is
  given; `--from any` MUST omit the option, print a warning and record `from = "any"` in the
  client registry. Test: unit (`test_netrange.py`).
- **REQ-RMT-07** `authorize` MUST rewrite `authorized_keys` atomically under a lock with a
  10 s timeout, MUST keep every line not tagged `hal-remote:<client>:` byte for byte, MUST
  replace the lines of the same client, and MUST leave the file 0600 and `~/.ssh` 0700.
  Test: unit (`test_authkeys.py`, `test_authorize.py`).
- **REQ-RMT-08** `authorize` MUST run the server checks first and MUST refuse while any
  `critical` check fails, unless each failing check id is named with `--accept-risk <ID>`;
  accepted risks MUST be recorded in the client registry. Test: unit (`test_authorize.py`).
- **REQ-RMT-09** `authorize` MUST resolve the `hal-remote` and `hal-hub` paths it writes, and
  the `hal` path it runs, to absolute paths of existing executables, and MUST refuse a path
  containing `"`, `\`, a newline or a NUL. Test: unit (`test_authorize.py`).
- **REQ-RMT-10** `authorize` MUST obtain a named client token for the client from the
  gateway (`hal gateway token issue`, 05-gateway), MUST hold it for delivery in a 0600 file,
  and the `hello` verb MUST deliver it at most once; an undelivered token MUST be deleted after
  `remote.server.delivery_ttl_s`. Test: unit (`test_serve.py`, `test_authorize.py` with a
  fake `hal`) and sshd (`test_sshd_pairing.py`).
- **REQ-RMT-11** `hal remote revoke NAME` MUST remove the client's lines, its registry entry,
  any pending delivery, and revoke its gateway token; it MUST exit non-zero naming what it
  could not remove. Test: unit (`test_authorize.py`).
- **REQ-RMT-12** The client MUST pin the desktop host key in its own `known_hosts` under a
  `HostKeyAlias`, MUST connect with `StrictHostKeyChecking yes`, and MUST NOT trust a key
  from `ssh-keyscan`. In the manual flow, `setup` MUST show the fingerprint and require
  either an interactive yes or `--host-key-fingerprint` matching it. Test: unit
  (`test_sshconfig.py`, `test_setup.py`) and sshd (`test_sshd_pairing.py` checks a changed
  host key is refused).
- **REQ-RMT-13** `authorize --expires YYYY-MM-DD` MAY add `expiry-time="YYYYMMDDZ"` to both
  lines. Test: unit (`test_authkeys.py`).

### Server check and host setup

- **REQ-RMT-14** `hal remote server-check` MUST be read-only, MUST NOT call `sudo` unless
  `--sshd-t` is given (and then only `sudo -n`), and MUST print each check's id, severity,
  result and fix; it MUST exit 1 when any `critical` check fails. Test: unit
  (`test_servercheck.py` with fixture trees and `FakeRunner`).
- **REQ-RMT-15** `server-check` MUST implement checks SC01 to SC22 in "Server check". Test:
  unit (`test_servercheck.py`, one case per check, pass and fail).
- **REQ-RMT-16** The sshd_config reader MUST apply first-value-wins, expand `Include` globs in
  lexical order (relative to `/etc/ssh`), match keywords case-insensitively, honour quoted
  arguments, and report any `Match` block as a warning that marks the keys it sets as
  "unknown". Test: unit (`test_sshdconf.py`).
- **REQ-RMT-17** SC14 MUST launch the forced command the way sshd does: the user's login
  shell with `-c`, `SSH_CLIENT` and `SSH_CONNECTION` set, `PATH=/usr/bin:/bin`, stdin a
  socket, and MUST pass only if the first stdout line is the JSON of the `check` verb with
  `"ok": true`. Test: unit (`test_servercheck.py` with a fake shell) and sshd
  (`test_sshd_serve.py`).
- **REQ-RMT-18** No `hal remote` command MUST print private keys or token values, except
  `env`/`connect` with `--show-token`; public keys MUST be summarised by fingerprint in
  check output. Test: unit (`test_output_secrets.py` greps every command's output in a
  `TempHalHome` for the fake token and key material).
- **REQ-RMT-19** `scripts/host-setup.sh --ssh` MUST write
  `/etc/ssh/sshd_config.d/10-hal-hardening.conf` containing at least
  `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin no`,
  `AllowUsers <user>`, `X11Forwarding no` and `PermitUserEnvironment no`; it MUST add
  `AllowTcpForwarding local` and `AllowStreamLocalForwarding no` only when the user also
  passes `--ssh-forwarding-limits`, and MUST run `sshd -t` before reloading the service.
  Test: unit (`tests/test_host_setup.py` runs the script with `--dry-run`, with and without
  the flag, and checks the printed file) and manual (fresh install).

### Forced command

- **REQ-RMT-20** `hal-remote serve` MUST accept exactly the verbs `mcp-stdio`, `hello` and
  `check` from `SSH_ORIGINAL_COMMAND` (empty means `mcp-stdio`), MUST answer any other value
  with one JSON error line and exit 64, and MUST NOT pass `SSH_ORIGINAL_COMMAND` to a shell
  or to `hal-hub`. Test: unit (`test_serve.py`) and sshd (`test_sshd_serve.py` sends
  `rm -rf x` and checks the error line).
- **REQ-RMT-21** `serve` MUST write only protocol bytes to stdout; logs MUST go to
  `state_dir()/remote/serve.log` and fatal errors to stderr. Test: unit (`test_serve.py`
  captures stdout for each verb).
- **REQ-RMT-22** `serve` MUST NOT depend on `PATH`, shell rc files or a desktop session
  environment: it MUST use the absolute `--hub` path, set `HAL_REMOTE_CLIENT`, and resolve
  the runtime directory only through `halkit.paths.runtime_dir()` (the rule is defined in
  00-overview.md, section `halkit.paths`); when that raises, `check` MUST report the error
  and `mcp-stdio` MUST exit with a JSON error instead of guessing a path. Test: unit
  (`test_serve.py` with an empty environment) and sshd (`test_sshd_serve.py`).
- **REQ-RMT-23** `mcp-stdio` MUST exec `hal-hub mcp --transport stdio --channel remote
  --origin remote:<client>` with the SSH client address in `HAL_REMOTE_ADDR`, so every
  audited call carries origin `remote:<client>` and `remote_addr` (flags defined in
  04-hub.md, section "Entry points and options"; audit fields defined in 01-security.md,
  section "Audit log"). Test: unit (`test_serve.py` checks the exec argv and env) and sshd
  (`tests/e2e/test_flow_remote.py` reads the audit record).
- **REQ-RMT-24** `serve` SHOULD cap concurrent `mcp-stdio` sessions per client at
  `remote.server.max_sessions` and answer the next one with a JSON error and exit 75. Test:
  unit (`test_serve.py`).

### Remote MCP sessions (requirements on 04-hub and 01-security)

- **REQ-RMT-25** In an MCP session with channel `remote`, a policy `ask` MUST be resolved the
  way 01-security defines for host harnesses using `hal-hub mcp` (01-security.md, sections
  "Approval binding" and "Approval surfaces and protocol"; RESOLUTIONS B1, B2, B4): by MCP
  elicitation when the laptop client declares it, else out of band through `hal approve` on
  the desktop; bound to 01's approval key and `apr_` request id; scopes limited to those the
  `remote` profile offers (never "always"); expiring after 01's `approval.timeout_s`. A
  decline, a timeout or a key mismatch MUST be a deny audited with 01's reason. This spec
  adds no approval mechanism of its own. Test: unit (04-hub's MCP approval tests run with
  `--channel remote`, cases supplied by this spec) and sshd (`tests/e2e/test_flow_remote.py`).
- **REQ-RMT-26** The `remote` channel profile MUST deny tier `system` and pack `desktop`
  (08's pack name), and MUST NOT loosen the selected profile in any rule; a user widens it
  only with rules in `policy.toml` (01-security owns the profile and its syntax). Test: unit
  (01-security `test_policy_profiles.py`, cases supplied by this spec).

### Tunnel

- **REQ-RMT-27** The tunnel MUST be `ssh -F <profile ssh_config> -N <alias>` with
  `ExitOnForwardFailure yes`, `ServerAliveInterval`, `BatchMode yes`, and only TCP
  `LocalForward`s bound to `127.0.0.1` on the laptop. Test: unit (`test_sshconfig.py` renders
  and runs `ssh -G` when `ssh` is present) and sshd (`test_sshd_tunnel.py`).
- **REQ-RMT-28** The supervisor MUST reconnect with exponential backoff with jitter from
  `backoff_initial_s` to `backoff_max_s`, MUST reset the backoff after a connection that
  lived `stable_after_s`, and MUST stop with exit code 3 and a reason in the status file on a
  permanent error (auth refused, host key mismatch, local port in use, bad config). Test:
  unit (`test_supervisor.py` with `FakeSpawner` and `FakeClock`).
- **REQ-RMT-29** The supervisor MUST probe `GET /v1/models` through the tunnel every
  `health_interval_s` and MUST restart ssh after two consecutive connection-level failures
  while ssh is still running. Test: unit (`test_supervisor.py` with `FakeProber`).
- **REQ-RMT-30** The supervisor MUST keep `status.json` (schema in "Tunnel supervisor")
  current by atomic writes, and `hal remote status` MUST report state, last error, gateway
  health and the commands to fix a permanent error; `--json` prints the file plus a live
  probe. Test: unit (`test_status.py`).
- **REQ-RMT-31** On Linux with a running systemd user manager, `up` and `down` MUST start and
  stop `hal-remote-tunnel@<profile>.service`; without one they MUST run a detached supervisor
  with a pid file. `up --enable` MUST enable the unit at login. Test: unit (`test_linux.py`
  with `FakeRunner`) and manual.
- **REQ-RMT-32** On Windows, `up` MUST start the supervisor without a console window, and
  `down` MUST stop it with `taskkill /T` only after checking that the pid's image name is the
  expected one; `up --install-task` MUST register a logon start (scheduled task, else a
  Startup-folder launcher). Test: windows (`test_windows.py` with `FakeRunner`) and manual.
- **REQ-RMT-33** `up` and `down` MUST be idempotent, and `down` MUST NOT signal a process
  whose identity it cannot verify. Test: unit (`test_linux.py`, `test_windows.py`).
- **REQ-RMT-34** No client command MUST ever fall back to a direct LAN connection, an
  unauthenticated request, or a desktop port other than those in the profile. Test: unit
  (`test_sshconfig.py` asserts the only network argv is `ssh`; `test_env.py` asserts every
  URL is `127.0.0.1`).

### Harness settings

- **REQ-RMT-35** `hal remote env HARNESS` MUST print settings for `claude`, `qwen`,
  `opencode`, `codex`, `crush`, `goose`, `cline`, `openai` and `mcp`, in `sh`, `powershell`
  or `cmd` syntax, using the same renderers as `hal connect` (`halkit.harness`, defined in
  00-overview.md, section "halkit API"), including the "unsandboxed" first line and
  `HAL_HARNESS=1` that `hal connect` prints (06-core-cli.md, section "Harness front-ends").
  Test: unit (`test_env.py` goldens per harness and shell).
- **REQ-RMT-36** `env` MUST reference the token file instead of printing the token unless
  `--show-token` is given. Test: unit (`test_env.py`, `test_output_secrets.py`).
- **REQ-RMT-37** The MCP entry MUST be stdio over the absolute path of `ssh` with `-F <abs
  ssh_config> -T <mcp alias>`. Test: unit (`test_env.py`).
- **REQ-RMT-38** `env` SHOULD fill model names from `/v1/models` through the tunnel and fall
  back to the role aliases with a comment when the tunnel is down. Test: unit (`test_env.py`
  with `FakeModelServer`).
- **REQ-RMT-39** `hal remote connect HARNESS [-- ARGS]` MUST check the tunnel, then exec the
  harness with the env settings; for a harness whose settings need a config file it MUST
  exit 2 and point to `env`. Test: unit (`test_env.py`).

### Gateway and desktop exposure

- **REQ-RMT-40** A request through the tunnel without the client's token MUST get 401 from
  the gateway, and one with a foreign `Host` MUST get 403 (05-gateway, RESOLUTIONS D4).
  Test: sshd (`test_sshd_tunnel.py` against `FakeModelServer` behind a token check) and unit
  (server-check SC17).
- **REQ-RMT-41** `server-check` MUST fail when any Hal port in 00-overview's ports table
  (8740, 8741, 8742, 8080, 9880, 8737) or Ollama's 11434 listens on a non-loopback address.
  Test: unit (`test_servercheck.py` with fixture `ss` output).
- **REQ-RMT-46** `authorize` MUST turn on the desktop gateway's loopback TCP listener the way
  05-gateway defines (`[gateway].tcp`, off by default; RESOLUTIONS D4) when it is off, MUST
  say so in its output, and MUST NOT change `tcp_host` or `tcp_port`. Test: unit
  (`test_authorize.py` with a fake `hal`).
- **REQ-RMT-47** The laptop end of the gateway forward MUST use the same port number as the
  desktop's `[gateway].tcp_port`, because the gateway accepts only its own
  `127.0.0.1:<port>` as `Host`; `setup` MUST refuse a profile whose local and remote ports
  differ. Test: unit (`test_setup.py`, `test_sshconfig.py`).

### Platform and packaging

- **REQ-RMT-42** `halremote` MUST import only the standard library and `halkit`, MUST import
  no POSIX-only module at import time, and its client commands MUST work on Windows 10 1809
  or later with Python 3.11+ and the built-in OpenSSH client. Test: unit
  (`tests/test_layering.py`), windows (CI job runs the client tests) and manual.
- **REQ-RMT-43** `hal-remote` MUST install console scripts `hal-remote` and a GUI script
  `hal-remote-tunnelw`, and `hal remote ...` MUST call `halremote.cli.main` with the same
  arguments. Test: unit (`test_cli.py`; 06-core-cli `test_cli_aliases.py` for delegation).

### hal code on a laptop

- **REQ-RMT-44** After `setup`, a Linux laptop's `hal code` MUST reach models only through the
  tunnel endpoint with the client token, through 05-gateway's client mode (`[gateway].url`
  plus the client token file; the forwarder targets a TCP URL, RESOLUTIONS D5/D6). Test:
  docker (`tests/e2e/test_flow_remote.py::test_laptop_hal_code_uses_tunnel`, marked `docker`
  and `slow`).
- **REQ-RMT-45** `setup` SHOULD offer the desktop's tools to laptop sessions by writing a
  `[mcp.servers.hal-<profile>]` entry with `host = true` (03-agent owns the table), which the
  laptop broker runs as an upstream over `ssh -T <mcp alias>` (02-sandbox, "Broker
  upstreams"; RESOLUTIONS E6). The desktop's policy stays authoritative and its approval
  requests are shown by the laptop's host UI, never the box. Test: docker
  (`test_flow_remote.py::test_desktop_tool_through_laptop_broker`).

## Design

### What runs where

| Piece | Desktop (server) | Laptop (client) |
| --- | --- | --- |
| llama-server, Ollama, GPU | yes, loopback only | no |
| hal-gateway | yes, unix socket + `127.0.0.1:8741` (TCP listener off by default, turned on by `authorize`) | no; reached at `127.0.0.1:8741` through the tunnel |
| hal-hub tools (packs) | yes, run by the hub runner | only packs the user installs there (none by default) |
| sshd | yes (`scripts/host-setup.sh --ssh`) | no |
| `hal-remote serve` (forced command) | yes | no |
| ssh client, tunnel supervisor | no | yes |
| Third-party harnesses (`hal remote env`) | optional (`hal connect`) | yes, unsandboxed, labelled |
| `hal code` box, broker, model forwarder | for code on the desktop | for code on a Linux laptop with docker or bubblewrap |
| Code being edited | desktop repos | laptop repos |

**Decision: the sandbox runs where the code lives.** On a Linux laptop the box, broker and
forwarder run on the laptop and only model calls cross the tunnel; for code on the desktop,
the user opens an ordinary SSH session to the desktop and runs `hal code` there. Reasons:

- A coding turn makes many small tool calls (read, grep, edit, run) and few model calls. Tool
  calls need the files and the user's editor sees the same files; keeping both local avoids
  a sync layer, which is out of scope.
- Model calls tolerate the network well. A request with 100k tokens of context is about
  400 KB, about 65 ms at 50 Mbit/s, and a stream at 100 tokens/s is under 20 KB/s; both are
  small next to generation time [unverified: estimate; measured by the manual check M6].
- The laptop box has the same properties as on the desktop: no network, no secrets, and the
  model token stays on the host side of the forwarder (REQ-SYS-12). Backend and runtime
  choice on the laptop follow 02-sandbox unchanged (for example, `runsc` is used only when
  its probe passes, which needs the runtime registered with `--host-uds=open` to reach the
  bind-mounted sockets).

Windows laptops get the tunnel, `env`, `connect` and MCP over SSH; for `hal code` they use
WSL2 (where the Linux rules apply) or an SSH session to the desktop. `hal code` on native
Windows exits with that explanation (06-core-cli owns the message).

### Components and file layout

```text
packages/hal-remote/
  pyproject.toml           name = "hal-remote"; deps: hal-kit; scripts below
  src/halremote/
    __init__.py            version
    __main__.py            python -m halremote -> cli.main
    cli.py                 argparse tree, printing, exit codes (the only printing module)
    model.py               dataclasses and JSON schemas (Profile, Forward, ClientRecord, ...)
    keys.py                ssh-keygen wrapper, public key parsing, fingerprints
    authkeys.py            render, parse, merge and remove authorized_keys lines
    netrange.py            from= derivation from a client address
    sshconf.py             client ssh_config rendering; ssh argv builders; ssh discovery
    sshdconf.py            read-only sshd_config reader (Include, first wins, Match)
    servercheck.py         SC01 to SC22
    authorize.py           desktop side of pairing: authorize, revoke, clients, registry
    pairing.py             laptop side of pairing: setup (bootstrap and manual), hello
    serve.py               forced command: verbs mcp-stdio, hello, check
    supervisor.py          tunnel loop, backoff, health probe, status file
    platform_linux.py      systemd user unit, detached spawn, pid checks
    platform_windows.py    windowless spawn, taskkill, schtasks, Startup folder
    envout.py              env and connect, built on halkit.harness
    units/hal-remote-tunnel@.service   package data (template)
  tests/
    test_keys.py test_authkeys.py test_netrange.py test_sshconfig.py test_sshdconf.py
    test_servercheck.py test_authorize.py test_setup.py test_serve.py test_supervisor.py
    test_status.py test_linux.py test_windows.py test_env.py test_cli.py
    test_output_secrets.py
    sshd/ conftest.py test_sshd_pairing.py test_sshd_restrictions.py test_sshd_serve.py
          test_sshd_tunnel.py
    fixtures/sshd/<case>/etc/ssh/...    sshd_config trees for the reader
```

Console scripts: `hal-remote = "halremote.cli:main"`; GUI script (no console on Windows):
`hal-remote-tunnelw = "halremote.cli:tunnel_main"`. `scripts/install.sh` (06-core-cli) MUST
put `hal`, `hal-remote` and `hal-hub` in `~/.local/bin` on the desktop, because the forced
command names them by absolute path and `authorize` runs `hal` without `PATH`.

Files on the laptop (profile `desktop` shown; `C` = `config_dir()`, `S` = `state_dir()`):

```text
C/remote/desktop/            0700
  profile.toml               0600  profile settings (schema below)
  ssh_config                 0600  generated, used with ssh -F
  known_hosts                0600  pinned desktop host keys under HostKeyAlias hal-desktop
  tunnel_ed25519[.pub]       0600  tunnel key
  mcp_ed25519[.pub]          0600  mcp key
  gateway-token              0600  client token for the desktop gateway
S/remote/desktop/
  status.json                      supervisor state (schema below)
  supervisor.pid.json              pid, start time, mode
  tunnel.log                       supervisor log, rotated at 1 MiB, 3 kept
```

On Windows, `C` and `S` are whatever `halkit.paths.config_dir()` and `state_dir()` return
there; 00-overview.md (section `halkit.paths`, RESOLUTIONS F6) owns the Windows defaults
(runtime dir `%LOCALAPPDATA%\hal\run`, TCP only). The client never needs `runtime_dir()`.
This spec asks 00 for home-based XDG-style defaults (`%USERPROFILE%\.config\hal`,
`%USERPROFILE%\.local\state\hal`) so the layout and the docs are the same on both OSes
(Decision D14). Keys are not placed in `%USERPROFILE%\.ssh`, so the user's own SSH setup is
untouched; `-F` also stops the user's `%USERPROFILE%\.ssh\config` from changing the
connection.

Files on the desktop:

```text
~/.ssh/authorized_keys                 0600  two lines per client, tagged in the comment
S/remote/clients.toml                  0600  client registry (schema below)
S/remote/delivery/<client>.json        0600  pending token delivery, deleted on hello or TTL
S/remote/sessions/<client>.<n>.lock          session slot locks (REQ-RMT-24)
S/remote/serve.log                           forced-command log, rotated at 1 MiB, 3 kept
~/.config/systemd/user/                      nothing: the desktop needs no remote unit
```

### Pairing flow

Two flows reach the same end state. Both end with the laptop calling `hello` over the new mcp
key, which proves the lines work and delivers the token.

**Bootstrap flow** (default; the laptop can already log in to the desktop with the user's own
key):

```text
laptop: hal remote setup user@desk.lan
  1. ssh-keygen -t ed25519 -N "" -C hal-remote:<client>:tunnel -f C/remote/desktop/tunnel_ed25519
     ssh-keygen -t ed25519 -N "" -C hal-remote:<client>:mcp    -f C/remote/desktop/mcp_ed25519
  2. ssh -o BatchMode=no user@desk.lan -- '~/.local/bin/hal-remote' authorize --stdin --json
       stdin:  AuthorizeRequest JSON (public keys only)
       stdout: AuthorizeResponse JSON (host public keys, ports, from, paths; no token)
     The user's own ssh config and known_hosts verify the host here (normal SSH trust).
  3. write known_hosts from AuthorizeResponse.host_keys under HostKeyAlias hal-desktop
  4. write profile.toml and ssh_config
  5. ssh -F ssh_config -T hal-desktop-mcp hello   -> HelloResponse with the token, once
  6. write gateway-token; append the [gateway] client-mode keys to config.toml if absent;
     on Linux with [remote].desktop_tools, add [mcp.servers.hal-desktop] (host = true)
  7. print next steps: hal remote up; hal remote env <harness>
```

`~/.local/bin/hal-remote` is passed quoted so the remote login shell expands `~` but no `PATH`
lookup happens (a non-interactive bash returns early from `~/.bashrc` before `~/.local/bin`
is added to `PATH` on this desktop [verified: `~/.bashrc` lines 5 to 9 and 136 to 149]).
`--remote-hal PATH` overrides it.

**Manual flow** (`--manual`; needed when password login is already off and the laptop has no
other key, which is the state `host-setup.sh --ssh` leaves):

```text
laptop:  hal remote setup desk.lan --manual
         -> creates keys, prints one command to run on the desktop:
            hal remote authorize --client laptop --tunnel-key 'ssh-ed25519 AAAA... ' \
              --mcp-key 'ssh-ed25519 AAAA... ' --from 192.168.1.0/24
desktop: (user runs it) -> writes the lines, prints the host key fingerprints:
            ED25519 SHA256:abc...  (compare this on the laptop)
laptop:  (setup is waiting) user presses Enter
         -> ssh -F ssh_config -o StrictHostKeyChecking=ask ... hello
            shows the fingerprint, requires "yes" or --host-key-fingerprint SHA256:abc...
         -> pins the key, stores the token, prints next steps
```

In the manual flow the laptop cannot see its own address as the desktop sees it, so `--from`
is required on `authorize` unless the command runs inside an SSH session (then
`SSH_CONNECTION` gives it). The printed command includes the laptop's best guess (the
address of the interface that routes to the desktop, with the default prefix).

`setup --print-lines --serve-path /abs/hal-remote --hub-path /abs/hal-hub --from PATTERN`
prints the two raw lines for users who manage `authorized_keys` by hand; such clients have no
registry entry and get the token with `hal remote authorize --client NAME --token-only`.

### authorized_keys lines

Rendered by `authkeys.render_line`. Example for client `laptop` (the gateway is the only
forward in v1):

```text
restrict,port-forwarding,from="192.168.1.0/24",permitopen="127.0.0.1:8741",permitlisten="127.0.0.1:1",command="/bin/false" ssh-ed25519 AAAAC3Nza... hal-remote:laptop:tunnel
restrict,from="192.168.1.0/24",command="'~/.local/bin/hal-remote' serve --client laptop --hub '~/.local/bin/hal-hub'" ssh-ed25519 AAAAC3Nza... hal-remote:laptop:mcp
```

Why each option. Behaviour was verified against OpenSSH 10.2p1, the version Ubuntu 26.04
ships, by running an unprivileged `sshd` on loopback in a temporary directory: on 2026-10-23
from the downloaded `openssh-server` package ("the sshd experiment" below), and on 2026-10-24
with the installed `/usr/sbin/sshd` (OpenSSH_10.2p1 Ubuntu-2ubuntu3.8) for the
`permitlisten` and `AllowStreamLocalForwarding` cases ("the 2026-10-24 experiment"):

- `restrict` turns off port, agent and X11 forwarding, PTY and `~/.ssh/rc`, and any future
  restriction (sshd(8)). With the mcp key, `ssh -tt` got "PTY allocation request failed" and
  `-L` to an open port got nothing [verified: sshd experiment].
- `port-forwarding` then re-enables forwarding for the tunnel key only.
- `permitopen` limits `-L` targets. A non-listed port was refused ("the request was denied")
  [verified: sshd experiment]. It also blocks `-L` to unix sockets: with `permitopen` set, a
  forward to a socket path was refused, while a key with `port-forwarding` and no
  `permitopen` made sshd try to connect to the path [verified: sshd experiment]. That matters
  here: the desktop user is in the `docker` group and `/var/run/docker.sock` is group
  `docker` [verified: `id -nG`, `ls -la`], so an unrestricted socket forward would be root on
  the desktop.
- `permitopen` does not limit `-R` (remote listens). With only `restrict`,
  `port-forwarding` and `permitopen`, `-R 127.0.0.1:29104:...` opened a listener on the
  server, and `-R /path:...` made sshd try to bind a unix socket [verified: sshd experiment].
  `permitlisten` cannot express "none" (it takes `[host:]port` with a port > 0 or `*`, per
  `handle_permit` in OpenSSH 10.2 `auth-options.c` [verified: source read]), so the tunnel
  key carries `permitlisten="127.0.0.1:1"`: any other `-R` port is refused ("the request was
  denied"), and port 1 itself fails with "bind [127.0.0.1]:1: Permission denied" because a
  normal user cannot bind it [verified: the 2026-10-24 experiment]. That closes TCP port
  squatting (a stolen key listening on, for example, 8737 while hal-employment is stopped,
  to collect local clients' bearer tokens) without touching sshd for the user's other keys.
- `permitlisten` does not cover unix-socket listens. With the full tunnel-key options above
  and the server defaults, `-R $XDG_RUNTIME_DIR/<name>.sock:...` created the socket (mode
  0600, owned by the user) on the server; with `AllowStreamLocalForwarding local` the same
  request failed ("remote port forwarding failed for listen path") [verified: the 2026-10-24
  experiment]. `AllowStreamLocalForwarding no` turns off unix-socket forwarding in both
  directions (sshd_config(5)), so it also refuses that request [unverified: the experiment
  ran `local`, not `no`; the sshd test in the Test plan covers `no`]. Per RESOLUTIONS,
  `AllowTcpForwarding local` and `AllowStreamLocalForwarding no` are recommended by
  `server-check` with a warning, not required (SC07, SC08), because they change sshd for the
  user's other keys. The remaining risk (a stolen tunnel key, used from inside the `from=`
  range, can bind a unix socket at a free path the user can write, such as a Hal socket
  while its service is stopped) is listed in the threat model and in Decision D7.
- `command="/bin/false"`: any session (shell, exec, subsystem) on the tunnel key runs
  `/bin/false`; `ssh ... id` with the tunnel key exited 1 [verified: sshd experiment]. `-N`
  opens no session, so the tunnel is unaffected.
- `from=` accepts patterns and CIDR (sshd(8)). A key used from outside the range is refused
  with "Permission denied (publickey)" and the server logs "not from a permitted host"
  [verified: sshd experiment].
- The mcp key's forced command ignores the client's command: `ssh ... 'rm -rf x'` ran the
  forced command with `SSH_ORIGINAL_COMMAND=rm -rf x` [verified: sshd experiment].
- sshd runs the forced command as `<login shell> -c "<command>"`. On Ubuntu, bash sources
  `~/.bashrc` for `-c` when `SSH_CLIENT` is set [verified: `bash -c` with a test `HOME` whose
  `.bashrc` echoes, 2026-10-23], so the paths are absolute and `serve` sets up its own
  environment; SC14 catches an rc file that prints before its interactive check. The
  experiment's forced command saw `PATH=/usr/local/sbin:...:/bin` without `~/.local/bin`
  and printed clean JSON as the first stdout bytes [verified: sshd experiment, with this
  user's real `~/.bashrc`].
- Paths are wrapped in single quotes (POSIX shell quoting, `shlex.quote`) inside the double
  quotes of the option; a path with `"`, `\`, newline or NUL is refused (REQ-RMT-09), which
  keeps the option parseable by sshd (which unescapes only `\"`).
- The comment `hal-remote:<client>:<role>` is the tag `merge` and `revoke` use. sshd ignores
  comments.

`authkeys.merge(existing: str, client: str, lines: list[str]) -> str` drops lines whose
comment field starts with `hal-remote:<client>:` and appends the new ones; all other bytes,
including comments, blank lines and the final newline state, are kept.

### from= derivation

`netrange.derive(addr: str, *, v4_prefix: int, v6_prefix: int) -> str` from the first field of
`SSH_CONNECTION` (bootstrap flow) or the `--from` flag:

| Client address | Result | Reason |
| --- | --- | --- |
| IPv4 in 10/8, 172.16/12, 192.168/16 | `/v4_prefix` network (default /24) | DHCP changes the host part, not the home subnet |
| IPv4 in 100.64.0.0/10 (Tailscale, CGNAT) | the exact address `/32` | Tailscale addresses are stable per device |
| IPv4 or IPv6 loopback | the exact address | tests and local use |
| IPv6 ULA fc00::/7 or global unicast | `/v6_prefix` network (default /64) | SLAAC and privacy addresses change inside the /64 |
| IPv6 link-local fe80::/10 | refused | scope ids do not survive in `from=` |
| any other IPv4 (public) | refused unless `--from` | a public source means the desktop is exposed beyond the LAN; the user must state the range |
| IPv4-mapped IPv6 | treated as the IPv4 address | sshd reports mapped addresses on dual-stack sockets |

`--from` takes a comma-separated list of CIDRs or sshd patterns, validated with `ipaddress`
for CIDR items and against `[0-9a-fA-F.:*?]+` for patterns; `any` omits the option.

### Client ssh_config

`sshconf.render(profile, paths) -> str`. Example for profile `desktop`:

```text
# Generated by hal-remote setup on 2026-10-23T18:00:00Z. Rerun setup instead of editing.
Host hal-desktop-tunnel hal-desktop-mcp
  HostName desk.lan
  User user
  Port 22
  HostKeyAlias hal-desktop
  UserKnownHostsFile "/home/me/.config/hal/remote/desktop/known_hosts"
  GlobalKnownHostsFile none
  StrictHostKeyChecking yes
  UpdateHostKeys no
  CheckHostIP no
  IdentitiesOnly yes
  IdentityAgent none
  BatchMode yes
  ConnectTimeout 10
  ServerAliveInterval 15
  ServerAliveCountMax 3
  ForwardAgent no
  ForwardX11 no
  ControlMaster no
  ControlPath none
  PermitLocalCommand no
  LogLevel ERROR
Host hal-desktop-tunnel
  IdentityFile "/home/me/.config/hal/remote/desktop/tunnel_ed25519"
  SessionType none
  ExitOnForwardFailure yes
  LocalForward 127.0.0.1:8741 127.0.0.1:8741
Host hal-desktop-mcp
  IdentityFile "/home/me/.config/hal/remote/desktop/mcp_ed25519"
  RequestTTY no
  ClearAllForwardings yes
```

`ssh -G -F <file> hal-desktop-tunnel` resolved this to the intended `identityfile`,
`localforward [127.0.0.1]:8741 [127.0.0.1]:8741`, `sessiontype none`, `identityagent none`,
`globalknownhostsfile none` and a quoted known_hosts path with a space; the mcp alias
resolved to `clearallforwardings yes`, `requesttty false` [verified: `ssh -G` with OpenSSH
10.2p1, 2026-10-23]. Host key pinning by alias accepted the right key and failed with
"REMOTE HOST IDENTIFICATION HAS CHANGED" for a wrong one [verified: sshd experiment].

Choices:

- `HostKeyAlias` + `CheckHostIP no`: the pin survives a DHCP address change.
- `IdentityAgent none` + `IdentitiesOnly yes`: only the profile key is offered, so an agent
  full of keys cannot hit `MaxAuthTries`, and a laptop agent is never used.
- `BatchMode yes`: never prompts (a prompt would hang a harness's MCP start).
- `LogLevel ERROR`: harnesses show ssh's stderr; warnings would be noise.
- Windows paths are written with forward slashes inside double quotes
  (`"C:/Users/me/.config/hal/remote/desktop/mcp_ed25519"`) [unverified: Windows OpenSSH
  parsing of quoted forward-slash paths; check M3].
- `ssh` discovery: `[remote].ssh` if set; else on Windows
  `%SystemRoot%\System32\OpenSSH\ssh.exe` if it exists, else `shutil.which("ssh")`; the
  absolute result is written into harness configs.

### Forwarded ports

`AuthorizeResponse.forwards` lists what the desktop permits. v1 has one forward:

| Name | Desktop port | Laptop port | When |
| --- | --- | --- | --- |
| `gateway` | `[gateway].tcp_port`, 8741 | the same number (REQ-RMT-47) | always |

The laptop uses the same port number for two reasons: the gateway answers 403 to any `Host`
other than its own `127.0.0.1:<port>` (05-gateway, RESOLUTIONS D4), and `hal connect` output
written for the desktop then works unchanged on the laptop. A laptop that already has a
listener on that port gets a permanent tunnel error from `ExitOnForwardFailure` with a clear
message. Tools go over MCP on SSH stdio only; the hub's HTTP port 8742 is not forwarded
(Decision D13).

### Forced command protocol (`hal-remote serve`)

Invocation (fixed by the `command=` option): `hal-remote serve --client NAME --hub PATH`.
The verb comes from `SSH_ORIGINAL_COMMAND`, split with `shlex.split`; the first word is the
verb and no other words are accepted.

| Verb | Output on stdout | Exit |
| --- | --- | --- |
| `mcp-stdio` or empty | the MCP stdio stream of `hal-hub` (newline-delimited JSON-RPC) | hub's exit code |
| `hello` | one JSON line, `HelloResponse` | 0 |
| `check` | one JSON line, `CheckResponse` | 0 if ok, 1 otherwise |
| anything else | one JSON line, `ErrorResponse` with `"error": "unknown_verb"` | 64 |
| session cap reached | `ErrorResponse` with `"error": "busy"` | 75 |

`mcp-stdio` does `os.execve(hub, [hub, "mcp", "--transport", "stdio", "--channel", "remote",
"--origin", "remote:" + client], env)` where `env` is a minimal environment: `HOME`, `USER`,
`LANG`, `PATH=/usr/bin:/bin`, `XDG_RUNTIME_DIR` when set, `HAL_REMOTE_CLIENT`,
`HAL_REMOTE_ADDR` (first field of `SSH_CONNECTION`), and `HAL_CONFIG`/`HAL_POLICY`/
`HAL_STATE_DIR`/`HAL_RUNTIME_DIR` when set. Before the exec, `serve` calls
`halkit.paths.runtime_dir()` once, so a missing runtime directory (00's rule:
`$XDG_RUNTIME_DIR/hal`, else `/run/user/<uid>/hal` if it exists, else an error) fails here
with a JSON error rather than inside the hub. Exec keeps the layering rule (`halremote`
imports only `halkit`) and leaves no extra process.

Messages (all `ensure_ascii=True`, one line, `\n`-terminated):

```json
{"schema": "hal.remote.hello/1", "client": "laptop", "time": "2026-10-23T18:00:05Z",
 "server": {"hostname": "desk", "user": "user", "hal_version": "0.1.0"},
 "forwards": [{"name": "gateway", "port": 8741, "base_path": "/v1"}],
 "tokens": {"gateway": "<client token in 05-gateway's format>"}}
```

`tokens` is `null` once delivered or expired; the laptop then tells the user to run
`hal remote authorize --client laptop --rotate-token` on the desktop.

```json
{"schema": "hal.remote.check/1", "ok": true, "time": "2026-10-23T18:00:05Z",
 "client": "laptop", "python": "3.13.11", "hal_version": "0.1.0",
 "hub": {"path": "~/.local/bin/hal-hub", "executable": true},
 "runtime_dir": "/run/user/1000/hal", "problems": []}
```

```json
{"schema": "hal.remote.error/1", "error": "unknown_verb",
 "message": "verb must be one of mcp-stdio, hello, check"}
```

### Authorize request and response

`AuthorizeRequest` (laptop to desktop on stdin, bootstrap flow):

```json
{"schema": "hal.remote.authorize-request/1",
 "client": "laptop",
 "tunnel_key": "ssh-ed25519 AAAAC3Nza... hal-remote:laptop:tunnel",
 "mcp_key": "ssh-ed25519 AAAAC3Nza... hal-remote:laptop:mcp",
 "from": null,
 "expires": null,
 "rotate": false}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | str | must equal `hal.remote.authorize-request/1` |
| `client` | str | `^[a-z0-9][a-z0-9-]{0,31}$`; the name the desktop knows the laptop by |
| `tunnel_key`, `mcp_key` | str | one OpenSSH public key line each, type `ssh-ed25519`, distinct |
| `from` | str or null | null derives from `SSH_CONNECTION`; `"any"` disables the option |
| `expires` | str or null | `YYYY-MM-DD` for `expiry-time` |
| `rotate` | bool | must be true to replace an existing client's keys |

`AuthorizeResponse` (desktop stdout):

```json
{"schema": "hal.remote.authorize/1",
 "client": "laptop",
 "time": "2026-10-23T18:00:03Z",
 "server": {"hostname": "desk", "user": "user", "hal_version": "0.1.0"},
 "host_keys": ["ssh-ed25519 AAAAC3Nza..."],
 "from": "192.168.1.0/24",
 "forwards": [{"name": "gateway", "port": 8741, "base_path": "/v1"}],
 "checks": {"ok": true, "accepted_risks": [], "warnings": ["SC07", "SC08"]},
 "gateway_tcp_enabled": true,
 "token_pending": true}
```

`host_keys` are read from `/etc/ssh/ssh_host_*_key.pub` (world-readable). The response never
carries the token; `hello` over the new key does, which also proves the lines work.
`gateway_tcp_enabled` is true when this `authorize` turned the listener on (REQ-RMT-46);
the laptop prints it as part of the next steps.

Client registry `S/remote/clients.toml` on the desktop:

```toml
schema = 1

[clients.laptop]
created = "2026-10-23T18:00:03Z"
updated = "2026-10-23T18:00:03Z"
from = "192.168.1.0/24"
expires = ""                      # empty: no expiry-time
tunnel_fingerprint = "SHA256:hxu0JN..."
mcp_fingerprint = "SHA256:5gHxRW..."
forwards = [8741]
gateway_token_name = "remote-laptop"
accepted_risks = []               # check ids passed with --accept-risk
```

Laptop `profile.toml`:

```toml
schema = 1
profile = "desktop"
client = "laptop"
host = "desk.lan"
user = "user"
port = 22
created = "2026-10-23T18:00:05Z"

[forward.gateway]
local = 8741
remote = 8741

[tunnel]
server_alive_interval_s = 15
server_alive_count_max = 3
connect_timeout_s = 10
backoff_initial_s = 2
backoff_max_s = 60
stable_after_s = 60
health_interval_s = 30
```

### Tunnel supervisor

`supervisor.Supervisor.run()` is the same code on every OS. States:

```text
starting -> connecting -> up -> (ssh exits) -> backoff -> connecting ...
                       \-> failed (permanent; exit 3)
any state -> stopped (signal or down; exit 0)
```

Loop:

1. Spawn `ssh -F <ssh_config> -N hal-<profile>-tunnel` with `LANG=C` and `LC_ALL=C` (stable
   stderr text), stderr piped (last 4 KiB kept), stdin `/dev/null`, `CREATE_NO_WINDOW` on
   Windows.
2. Readiness: poll a TCP connect to `127.0.0.1:<local gateway port>` every 250 ms until it
   succeeds or `connect_timeout_s + 5` s pass. ssh opens local listeners after authentication,
   and `ExitOnForwardFailure` makes a failed bind fatal, so a successful connect means the
   forward is ours.
3. `up`: every `health_interval_s`, `GET /v1/models` with the token, timeout 5 s. 200 is
   healthy; 401 is "token rejected" (state stays `up`, `gateway.ok = false`, hint: rotate the
   token); 502 or 503 is "gateway up, model backend down"; connection refused, reset or
   timeout counts as a connection-level failure. Two in a row while ssh runs: terminate ssh
   and reconnect (laptop resume from sleep is the usual cause).
4. On ssh exit, classify with `classify_exit(code, stderr_tail, lifetime_s)`:

   | Stderr contains | Kind |
   | --- | --- |
   | `Permission denied` | permanent: key not authorised, revoked, expired or `from=` mismatch |
   | `Host key verification failed`, `REMOTE HOST IDENTIFICATION HAS CHANGED` | permanent |
   | `Address already in use`, `cannot listen to port`, `Could not request local forwarding` | permanent: laptop port taken |
   | `Bad configuration option`, `Load key`, `no such identity`, `bad permissions` | permanent |
   | anything else, including `Connection refused`, `timed out`, `Network is unreachable`, `Could not resolve hostname` | transient |

5. Transient: wait `next_delay(attempt)`, where
   `delay = min(backoff_max_s, backoff_initial_s * 2 ** (attempt - 1)) * uniform(0.8, 1.2)`,
   and `attempt` resets to 1 when the previous connection lived `stable_after_s`.
   Permanent: write the reason and fix to `status.json`, exit 3. Stopping on permanent errors
   avoids OpenSSH 9.8+ `PerSourcePenalties` (default on; `authfail` 5 s) locking the laptop
   out [verified: `sshd_config(5)` of 10.2p1].
6. On SIGTERM, SIGINT or (Windows) the stop file `S/remote/<profile>/stop`: terminate ssh,
   wait 5 s, kill, write `stopped`, remove the pid file, exit 0.

`status.json`:

```json
{"schema": "hal.remote.status/1",
 "profile": "desktop",
 "state": "up",
 "mode": "systemd",
 "supervisor_pid": 41231,
 "ssh_pid": 41240,
 "since": "2026-10-23T18:01:00Z",
 "attempt": 1,
 "next_retry_at": null,
 "last_error": null,
 "fix": null,
 "forwards": [{"name": "gateway", "local": "127.0.0.1:8741", "remote": "127.0.0.1:8741"}],
 "gateway": {"ok": true, "checked_at": "2026-10-23T18:05:30Z", "status": 200,
             "detail": "3 models"}}
```

`state` is one of `starting`, `connecting`, `up`, `backoff`, `failed`, `stopped`; `mode` is
`systemd`, `detached` or `foreground`; `fix` is a command the user can run (for example
`hal remote setup desk.lan --rotate` for a revoked key).

`hal remote status` prints, for example:

```text
desktop  up since 18:01Z (systemd)   gateway ok (3 models)   127.0.0.1:8741 -> desk.lan:8741
```

and exits 0 when `up` and healthy, 4 when not running or failing.

#### Linux

`hal-remote-tunnel@.service`, installed into `~/.config/systemd/user/` by `up`:

```ini
[Unit]
Description=Hal SSH tunnel (profile %i)
Documentation=https://github.com/NotEoin/Hal/blob/main/docs/howto/use-hal-from-a-laptop.md
StartLimitIntervalSec=0

[Service]
Type=exec
ExecStart=/home/me/.local/bin/hal-remote tunnel-run --profile %i
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=3
NoNewPrivileges=yes

[Install]
WantedBy=default.target
```

- The supervisor does its own backoff; systemd only restarts a crashed supervisor, and
  `RestartPreventExitStatus=3` stops it from retrying a permanent error.
- No ordering on `network-online.target`: it is a system target the user manager cannot
  order against, and the backoff covers a late network.
- `up` = `systemctl --user daemon-reload` (when the file changed) then `start`; `--enable`
  adds `enable`; `down` = `stop` (`--disable` adds `disable`). Each `systemctl` call has a
  30 s timeout.
- Detection: `systemctl --user is-system-running` with a 5 s timeout; output `running` or
  `degraded` means usable. Otherwise (no systemd, WSL without systemd, a container) `up`
  spawns `hal-remote tunnel-run` with `start_new_session=True` and writes
  `supervisor.pid.json` (`pid`, `started` from `/proc/<pid>/stat` start time, `mode`).
  `down` checks `/proc/<pid>/cmdline` contains `tunnel-run` and the start time matches
  before sending SIGTERM.

#### Windows

- `up` starts `hal-remote-tunnelw.exe tunnel-run --profile P` (the GUI script, so no console)
  with `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, and records the pid. The
  supervisor starts `ssh.exe` with `CREATE_NO_WINDOW`.
- `down` runs `tasklist /FI "PID eq <pid>" /FO CSV /NH` (10 s timeout), requires the image
  name `hal-remote-tunnelw.exe` or `python*.exe`, then `taskkill /PID <pid> /T /F` (15 s
  timeout), which also ends `ssh.exe`. Before that it creates the stop file and waits 3 s so
  the supervisor can record `stopped`.
- `up --install-task` runs
  `schtasks /Create /F /TN "Hal\remote-<profile>" /SC ONLOGON /RL LIMITED /TR "\"<exe>\" tunnel-run --profile <profile>"`
  (30 s timeout). If it fails (for example access denied), it writes
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Hal remote tunnel (<profile>).cmd`
  containing `@start "" "<exe>" tunnel-run --profile <profile>`. `down --disable` removes
  both. [unverified: whether `/SC ONLOGON` needs an administrator for a task that runs as the
  same user; check M4.]
- The OpenSSH client (`ssh.exe`, `ssh-keygen.exe`) is a built-in optional feature of
  Windows 10 1809 and later, installed by default on Windows 11 [unverified on a Windows
  machine in this session; check M3]. `setup` exits 3 with the "Add an optional feature"
  instruction when `ssh.exe` is missing.
- `--foreground` (both OSes) runs the supervisor in the terminal; Ctrl-C stops it.
- Forwards are TCP to `127.0.0.1` only. Windows OpenSSH has no unix-socket forwarding to rely
  on, and the desktop refuses socket forwards anyway; using TCP on Linux too keeps one code
  path.
- `ssh-keygen.exe` gives new private keys an owner-only ACL, which Windows `ssh.exe` requires
  [unverified: Win32-OpenSSH behaviour; check M3]. `setup` never writes key files itself.

### env and connect

`envout` builds a `halkit.harness` endpoint description and calls the shared renderer that
`hal connect` uses, so the two commands never drift. 00-overview.md (section "halkit API",
module `halkit.harness`, RESOLUTIONS C6) owns the types and signatures; `envout` uses them
as defined there and adds nothing to them. What this spec needs the endpoint to carry: the
base URL (`http://127.0.0.1:8741`), the token file path (never the token itself), the model
names per role alias, an MCP stdio entry (absolute `ssh` path plus
`("-F", "<abs ssh_config>", "-T", "hal-<profile>-mcp")`), a label such as "Hal on desk.lan
via SSH tunnel (profile desktop)", and a way to ask whether a harness can be configured by
environment variables alone or needs a config file.

Example, `hal remote env claude` in sh (the first line and `HAL_HARNESS` come from the shared
renderer, as in `hal connect`):

```sh
# UNSANDBOXED: claude and every tool it runs work on this laptop as you. Hal on desk.lan via SSH tunnel (profile desktop). Needs: hal remote up
export ANTHROPIC_BASE_URL="http://127.0.0.1:8741"
export ANTHROPIC_AUTH_TOKEN="$(cat '/home/me/.config/hal/remote/desktop/gateway-token')"
export ANTHROPIC_MODEL="hal-code"
export HAL_HARNESS=1
# Desktop tools over SSH (stdio MCP):
claude mcp add hal-desktop --scope user -- /usr/bin/ssh -F '/home/me/.config/hal/remote/desktop/ssh_config' -T hal-desktop-mcp
```

The same in PowerShell:

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8741"
$env:ANTHROPIC_AUTH_TOKEN = (Get-Content -Raw "C:\Users\me\.config\hal\remote\desktop\gateway-token").Trim()
$env:ANTHROPIC_MODEL = "hal-code"
$env:HAL_HARNESS = "1"
claude mcp add hal-desktop --scope user -- "C:\Windows\System32\OpenSSH\ssh.exe" -F "C:/Users/me/.config/hal/remote/desktop/ssh_config" -T hal-desktop-mcp
```

`hal remote env codex` (Responses only) prints a `config.toml` snippet:

```toml
[model_providers.hal]
name = "Hal (desk.lan via SSH)"
base_url = "http://127.0.0.1:8741/v1"
env_key = "HAL_GATEWAY_TOKEN"
wire_api = "responses"

[mcp_servers.hal-desktop]
command = "/usr/bin/ssh"
args = ["-F", "/home/me/.config/hal/remote/desktop/ssh_config", "-T", "hal-desktop-mcp"]
```

plus the line that sets `HAL_GATEWAY_TOKEN` from the token file. `env mcp` prints only the
`mcpServers` JSON entry; `env openai` prints `OPENAI_BASE_URL` and `OPENAI_API_KEY`. The
per-harness formats (Qwen `modelProviders`, opencode `provider` with
`@ai-sdk/openai-compatible`, Crush `providers.<id>.type = "openai-compat"`, Goose, Cline)
are owned by the shared renderer; each renderer states the harness version it was checked
against.

`ANTHROPIC_BASE_URL` is the gateway root URL, as for `hal claude` (RESOLUTIONS F7); `hal-code`
is one of the role aliases the gateway resolves from `[hal].*_model` (RESOLUTIONS D1).

`hal remote connect claude -- --resume` checks `status` (exit 4 if the tunnel is down),
builds `env_vars`, adds `--mcp-config '<json>'` for Claude Code (the flag accepts JSON
strings [verified: `claude --help`, version 2.1.293]), and execs the harness. Harnesses that
need a config file (Codex, Qwen, opencode, Crush) exit 2 with the matching `env` command.

### Server check

Severity `critical` blocks `authorize`; `warn` and `info` do not. Each check returns
`CheckResult(id, title, severity, status, detail, fix)` with `status` in `pass`, `fail`,
`skip`, `unknown`.

| Id | Check | Severity | How (read-only) |
| --- | --- | --- | --- |
| SC01 | `sshd` installed | critical | `/usr/sbin/sshd` exists and is executable (it does on this desktop: openssh-server is active, key-only [verified: brief host facts, 2026-10-23]) |
| SC02 | ssh service active | critical | `systemctl is-active ssh.socket ssh.service` (10 s); Ubuntu 26.04 ships socket activation (`ssh.socket`, `ListenStream=0.0.0.0:22`) [verified: package contents of openssh-server 1:10.2p1-2ubuntu3.8] |
| SC03 | `PasswordAuthentication no` | critical | reader; the default is `yes` and Ubuntu's main file does not set it [verified: `/usr/share/openssh/sshd_config` in the package] |
| SC04 | `KbdInteractiveAuthentication no` | critical | reader (Ubuntu's main file sets `no`) |
| SC05 | `PermitRootLogin no` or `prohibit-password` | warn | reader |
| SC06 | `AllowUsers` set and contains the current user | warn | reader |
| SC07 | `AllowTcpForwarding local` (or `no` with no Hal clients) | warn | reader; recommended, not required: the tunnel key's `permitlisten="127.0.0.1:1"` already refuses TCP `-R`, and this line would also change the user's other keys (RESOLUTIONS) |
| SC08 | `AllowStreamLocalForwarding no` (recommended; `local` also passes and keeps `-L` to sockets for the user's other keys) | warn | reader; recommended, not required (RESOLUTIONS); without it a stolen tunnel key can bind unix sockets with `-R` [verified: the 2026-10-24 experiment]; the fix text names `host-setup.sh --ssh --ssh-forwarding-limits` |
| SC09 | `PermitUserEnvironment no` | critical | reader; `environment=` or `~/.ssh/environment` could inject `LD_PRELOAD` into the forced command |
| SC10 | `AuthorizedKeysFile` includes `.ssh/authorized_keys` | critical | reader |
| SC11 | home not group or world writable; `~/.ssh` 0700; `authorized_keys` 0600, owned by the user | critical | `os.stat` (StrictModes would reject the keys otherwise) |
| SC12 | every `hal-remote:` line has `restrict`, `from=` (or a registry `from = "any"`), and the exact options of REQ-RMT-04 (including `permitlisten`) or 05 | critical | `authkeys.parse` |
| SC13 | forced-command paths exist and are executable | critical | `os.access` |
| SC14 | forced command starts cleanly non-interactively | critical | REQ-RMT-17 emulation, 20 s timeout |
| SC15 | `/etc/ssh/sshrc` absent | warn | it runs for every session and could print to stdout |
| SC16 | Hal ports and 11434 bound to loopback only | critical | parse `ss -ltnH` (10 s) for 8740, 8741, 8742, 8080, 11434, 9880, 8737 (00-overview ports table) |
| SC17 | gateway TCP listener on and rejecting a request without a token | critical | `GET http://127.0.0.1:8741/v1/models`, no auth, timeout 5 s, expect 401; connection refused fails with the fix "run `hal remote authorize` again or turn on `[gateway].tcp`" |
| SC18 | `Match` blocks present | warn | reader marks affected keys `unknown`; suggests `--sshd-t` |
| SC19 | registry and `authorized_keys` agree | warn | clients without lines, tagged lines without a client |
| SC20 | unrestricted keys in `authorized_keys` | info | count of lines without `restrict` or `command=` (by fingerprint only) |
| SC21 | firewall | info | `ufw status` if installed (10 s); suggests limiting 22 to the LAN |
| SC22 | expired or undelivered tokens | info | delivery files past TTL are deleted (the only write, and only with `--fix`) |

`--sshd-t` runs `sudo -n /usr/sbin/sshd -T -C user=<user>,host=<laptop>,addr=<ip>` (15 s
timeout) and checks the same keys on the effective config; it is optional because `sshd -T`
as a normal user fails ("no hostkeys available -- exiting") [verified: extracted sshd binary,
2026-10-23]. Without it, SC03 to SC10 come from the static reader.

On this desktop today the static reader would report SC03 to SC06 and SC09 pass (SC09 through
OpenSSH's default `PermitUserEnvironment no`) and SC07 and SC08 warn (defaults `yes`): the
live `10-hal-hardening.conf` holds `PasswordAuthentication no`,
`KbdInteractiveAuthentication no`, `PermitRootLogin no`, `AllowUsers user` and
`X11Forwarding no` only [verified: `cat /etc/ssh/sshd_config.d/*.conf`, 2026-10-24; the
expected check results are a reading of these lines, not a run]. The new
`host-setup.sh --ssh` writes `PermitUserEnvironment no` explicitly so a later edit of the
main file cannot turn it on.

Reader details (`sshdconf.read(root: Path = Path("/etc/ssh")) -> SshdConfig`):

- Starts at `sshd_config`, follows `Include` (globs sorted lexically; relative paths are
  relative to `/etc/ssh`); Ubuntu's main file begins with `Include
  /etc/ssh/sshd_config.d/*.conf` [verified: package file], so drop-ins win over it.
- First value wins for single-value keys; `AllowUsers` and `DenyUsers` accumulate.
- Keywords are case-insensitive; arguments may be double-quoted.
- A `Match` line starts a conditional block; keys inside are recorded as conditional and
  the affected global values are reported `unknown` when they differ.
- Unreadable files make the dependent checks `unknown` (shown as failures for critical
  checks).

### Host setup

`scripts/host-setup.sh --ssh` (06-core-cli owns the script and its flags; this spec owns the
content) writes `/etc/ssh/sshd_config.d/10-hal-hardening.conf`, the file name the current
script already uses on this desktop:

```text
# Written by Hal scripts/host-setup.sh --ssh. Key-only SSH for hal-remote.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers <user>
X11Forwarding no
PermitUserEnvironment no
```

With `--ssh-forwarding-limits` it appends:

```text
AllowTcpForwarding local
AllowStreamLocalForwarding no
```

- The name sorts before `50-cloud-init.conf`, which some Ubuntu images ship with
  `PasswordAuthentication yes` [unverified: not present on this desktop]; first value wins,
  so `10-` is required.
- The current `repos/hal-host-setup.sh` writes the same file with the first five lines
  [verified: read of the script and of `/etc/ssh/sshd_config.d/10-hal-hardening.conf`,
  2026-10-24]; the port keeps the name and adds `PermitUserEnvironment no`. openssh-server
  is already installed and active here, so on this desktop the script only rewrites the
  drop-in, runs `sshd -t` and reloads `ssh`.
- The forwarding lines are opt-in (RESOLUTIONS): they also stop `ssh -R` for the user's own
  unrestricted keys, and hal-employment's own remote feature may use `-R` [unverified: its
  launcher was not read, per the no-touch rule]. The script writes
  `AllowStreamLocalForwarding no`, the value RESOLUTIONS and 00-overview (Flow 6) name; it
  also stops `-L` to unix sockets for the user's own keys. A user who needs those can set
  `local` by hand, which SC08 also accepts and which blocks only remote socket listens
  [verified: the 2026-10-24 experiment refused `-R <path>` with `local`].
- The script runs `sshd -t` before `systemctl enable --now ssh` (fresh install) or
  `systemctl reload ssh` (existing install), as today.

### Remote MCP sessions: policy and approvals

The forced command execs `hal-hub mcp --transport stdio --channel remote --origin
remote:<client>`. 04-hub owns the server, the MCP wire format of approvals and the
`--origin`/`HAL_REMOTE_ADDR` handling (04-hub.md, sections "Entry points and options" and
"Approvals over MCP"); 01-security owns the `remote` channel profile, the approval data
model, surfaces and audit (01-security.md, sections "`policy.toml` schema", "Approval
binding", "Approval surfaces and protocol" and "Audit log"). Because the guard and reviewer
live in `halhub` (RESOLUTIONS A4), `hal-hub mcp` applies them to remote sessions exactly as
to local ones. This spec states only what remote use needs from them (REQ-RMT-23, 25, 26):

| Tier | Selected profile `default` | With channel `remote` | Reason |
| --- | --- | --- | --- |
| `read` | allow | allow | |
| `write` | allow, logged | allow, logged | Hal's own stores |
| `act` | ask | ask | the user is at the laptop, so the request must reach the laptop |
| `send` | ask | ask | |
| `system` | ask (shell: deny) | deny | power, services and process control on a machine the user is not in front of |
| pack `desktop` | as tier | deny | screen, clipboard and media of a desktop someone else may be using |

How an `ask` is answered (RESOLUTIONS B4):

- When the laptop client declares MCP elicitation, the hub sends the approval request through
  it. The request is rendered by the hub from the canonical arguments and carries 01's
  `apr_` id and approval key; the client's answer is checked against that key, and a retry
  with different arguments is a new request. Scopes are those the `remote` profile offers
  (`once`, and `session` if 01 allows it there); there is no "always".
- When it does not, or the request cannot be shown in full (01's `shows_all_args` rule for
  calls with content parameters), the request waits on the hub's control socket on the
  desktop and is answered with `hal approve ID` there, for example from an ordinary SSH login
  with the user's own key. The mcp key itself never gives a shell.
- An unanswered request expires after 01's `approval.timeout_s` (the profile may raise it)
  and is a deny. Any `_confirmed`-style argument is stripped and logged (01-security).

How far an elicitation answer can be trusted: it comes from the laptop client application,
not from the model, but an unsandboxed laptop harness (or anything else running as the user
on the laptop) can answer its own elicitations. Approvals in remote sessions are therefore
advisory against a compromised laptop, as for every host harness (01-security residual risk
R-new-4; threat model below). The deny of `system` and pack `desktop` does not depend on
approvals and holds regardless.

### hal code on a laptop

After `setup`, the laptop's `config.toml` gets (appended only when no `[gateway]` table
exists; otherwise `setup` prints the lines to add):

```toml
[gateway]
url = "http://127.0.0.1:8741"
token_file = "/home/me/.config/hal/remote/desktop/gateway-token"
```

- These are 05-gateway's client-mode keys (`url`, `token_file`), defined in 05-gateway.md,
  sections "Client tokens and client mode" and "Configuration" (RESOLUTIONS D5/D6). In
  client mode `token_file` names the client token, not an owner token.
- The laptop's model forwarder (05-gateway `SessionForwarder` targeting a TCP URL with the
  client token, started by halcore as 02-sandbox describes) and `halkit.llm` send model calls
  to `url` with the token; the box never sees the token (REQ-SYS-12).
- With `[remote].desktop_tools = true` (default true when a profile exists), `setup` also
  writes a user MCP server entry (03-agent owns `[mcp.servers.<name>]`; RESOLUTIONS E6):

  ```toml
  [mcp.servers.hal-desktop]
  host = true
  command = "/usr/bin/ssh"
  args = ["-F", "/home/me/.config/hal/remote/desktop/ssh_config", "-T", "hal-desktop-mcp"]
  ```

  The laptop broker runs it as an upstream (02-sandbox, "Broker upstreams"). Its tools reach
  the box as `hal-desktop__<tool>` with pack `mcp-hal-desktop` and tier `act` unless the
  laptop's policy says otherwise. So by default a desktop tool call is asked once on the
  laptop, and an `act`/`send` tool may be asked again by the desktop's own policy; both
  prompts appear in the laptop's host UI (the TUI) and the model in the box can answer
  neither. `setup` prints, as an optional next step, the `hal policy allow` command that lets
  the desktop's policy alone decide for pack `mcp-hal-desktop`; it never writes that rule
  itself (durable allows come only from the user, RESOLUTIONS B2).

### Python interfaces

```python
# model.py
Role = Literal["tunnel", "mcp"]

@dataclass(frozen=True)
class Forward:
    name: Literal["gateway"]         # the only forward in v1 (Decision D13)
    local_port: int
    remote_port: int

@dataclass(frozen=True)
class Profile:
    name: str
    client: str
    host: str
    user: str
    port: int
    forwards: tuple[Forward, ...]
    tunnel: TunnelSettings

@dataclass(frozen=True)
class ProfilePaths:
    root: Path
    ssh_config: Path
    known_hosts: Path
    tunnel_key: Path
    mcp_key: Path
    token: Path
    state: Path

def profile_paths(name: str) -> ProfilePaths: ...
def load_profile(name: str | None = None) -> Profile: ...     # None: [remote].default_profile
def save_profile(profile: Profile) -> None: ...

# keys.py
@dataclass(frozen=True)
class PublicKey:
    type: str             # "ssh-ed25519" only in v1
    blob: str             # base64
    comment: str
    def fingerprint(self) -> str: ...   # "SHA256:..." computed in Python (sha256 of the blob)

def parse_public_key(line: str) -> PublicKey: ...            # raises KeyFormatError
def generate(path: Path, *, comment: str, ssh_keygen: Path, timeout_s: float = 30) -> PublicKey: ...

# authkeys.py
@dataclass(frozen=True)
class HalKeyLine:
    client: str
    role: Role
    options: tuple[str, ...]
    key: PublicKey

def render_line(role: Role, *, client: str, key: PublicKey, from_pattern: str | None,
                forwards: Sequence[Forward], serve_path: Path, hub_path: Path,
                expires: date | None = None) -> str: ...
def parse(text: str) -> tuple[list[HalKeyLine], int]: ...    # hal lines, count of other lines
def merge(existing: str, client: str, lines: Sequence[str]) -> str: ...
def remove(existing: str, client: str) -> tuple[str, int]: ...
def write(path: Path, text: str, *, lock_timeout_s: float = 10) -> None: ...

# netrange.py
def derive(addr: str, *, v4_prefix: int = 24, v6_prefix: int = 64) -> str: ...   # FromPatternError
def validate(pattern: str) -> str: ...

# sshconf.py
def find_ssh(override: str = "") -> Path: ...
def find_ssh_keygen(ssh: Path) -> Path: ...
def render(profile: Profile, paths: ProfilePaths) -> str: ...
def tunnel_argv(profile: Profile, paths: ProfilePaths, ssh: Path) -> list[str]: ...
def mcp_argv(profile: Profile, paths: ProfilePaths, ssh: Path) -> list[str]: ...

# sshdconf.py
@dataclass(frozen=True)
class SshdConfig:
    values: Mapping[str, tuple[str, ...]]     # lower-case keyword -> first value's args
    allow_users: tuple[str, ...]
    conditional: frozenset[str]               # keywords set inside Match blocks
    files: tuple[Path, ...]
    unreadable: tuple[Path, ...]
def read(root: Path = Path("/etc/ssh")) -> SshdConfig: ...
def effective(cfg: SshdConfig, key: str, default: str) -> str | None: ...  # None: unknown

# servercheck.py
Severity = Literal["critical", "warn", "info"]
@dataclass(frozen=True)
class CheckResult:
    id: str
    title: str
    severity: Severity
    status: Literal["pass", "fail", "skip", "unknown"]
    detail: str
    fix: str
def run_checks(*, home: Path, etc_ssh: Path = Path("/etc/ssh"), runner: Runner,
               sshd_t: bool = False, fix: bool = False) -> list[CheckResult]: ...

# authorize.py
def authorize(req: AuthorizeRequest, *, client_addr: str | None, runner: Runner,
              accept_risks: frozenset[str], authorized_keys: Path) -> AuthorizeResponse: ...
def revoke(client: str, *, runner: Runner, authorized_keys: Path) -> list[str]: ...  # problems
def clients() -> list[ClientRecord]: ...

# serve.py
def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int: ...

# supervisor.py
class Spawner(Protocol):
    def spawn(self, argv: Sequence[str], env: Mapping[str, str]) -> Child: ...
class Prober(Protocol):
    def models(self, port: int, token: str, timeout_s: float) -> ProbeResult: ...
class Supervisor:
    def __init__(self, profile: Profile, paths: ProfilePaths, *, ssh: Path,
                 clock: Clock, spawner: Spawner, prober: Prober, rng: random.Random) -> None: ...
    def run(self, stop: threading.Event) -> int: ...      # 0 stopped, 3 permanent
def next_delay(attempt: int, *, initial: float, maximum: float, rng: random.Random) -> float: ...
def classify_exit(code: int, stderr_tail: str, lifetime_s: float) -> Literal["permanent", "transient"]: ...
```

`Runner` is `halkit.proc.run`-compatible (argv, timeout) so tests inject `FakeRunner`. Every
subprocess has a timeout: `ssh-keygen` 30 s, bootstrap `ssh` 120 s (the user may type a
password), `systemctl` 30 s, `schtasks` 30 s, `tasklist` 10 s, `taskkill` 15 s, `icacls`
10 s, `ss` 10 s, `ufw` 10 s, SC14 emulation 20 s, `hal gateway token` and the gateway TCP
switch 15 s each.

### CLI synopsis

`hal remote ...` and `hal-remote ...` are the same. Laptop commands:

```text
hal remote setup HOST [--profile NAME] [--client NAME] [--user USER] [--port N]
                      [--manual] [--from PATTERN|any] [--remote-hal PATH]
                      [--host-key-fingerprint SHA256:...] [--expires YYYY-MM-DD]
                      [--rotate] [--print-lines --serve-path PATH --hub-path PATH]
hal remote up [PROFILE] [--foreground] [--enable] [--install-task]
hal remote down [PROFILE] [--disable]
hal remote status [PROFILE] [--json] [--deep]          # --deep also runs the check verb
hal remote env HARNESS [PROFILE] [--shell sh|powershell|cmd] [--show-token] [--json]
hal remote connect HARNESS [PROFILE] [-- HARNESS_ARGS...]
hal remote profiles [--json]
```

Desktop commands:

```text
hal remote authorize --client NAME (--stdin | --tunnel-key KEY --mcp-key KEY)
                     [--from PATTERN|any] [--expires YYYY-MM-DD] [--rotate]
                     [--accept-risk ID]... [--json] [--authorized-keys PATH]
hal remote authorize --client NAME --from PATTERN       # update from= only
hal remote authorize --client NAME --rotate-token       # new token, pending delivery
hal remote authorize --client NAME --token-only         # token for a --print-lines client
hal remote revoke NAME
hal remote clients [--json]
hal remote server-check [--json] [--sshd-t] [--fix]
hal-remote serve --client NAME --hub PATH                # forced command only
hal-remote tunnel-run --profile NAME                     # supervisor; used by units and tasks
```

HARNESS is one of `claude`, `qwen`, `opencode`, `codex`, `crush`, `goose`, `cline`,
`openai`, `mcp`. Exit codes: 0 ok; 1 check failed or operation failed; 2 usage or config
error (including `ConfigError`); 3 not set up / permanent tunnel error; 4 tunnel not up;
64 and 75 for `serve` as above.

### Configuration keys

`[remote]` in `config.toml` (registered as a `halkit.config.Table`, owner `hal-remote`):

| Key | Type | Default | Reason |
| --- | --- | --- | --- |
| `default_profile` | str | `""` (the only profile, else error naming the choices) | Most users pair one desktop |
| `ssh` | str | `""` (discover) | Override for a non-standard OpenSSH install |
| `desktop_tools` | bool | `true` | Laptop `hal code` gets the desktop's tools by default; the desktop policy still decides |

`[remote.server]` (desktop):

| Key | Type | Default | Reason |
| --- | --- | --- | --- |
| `max_sessions` | int | `4` | One or two harnesses per laptop is normal; the cap bounds process count if a client loops |
| `delivery_ttl_s` | int | `86400` | A day covers the manual flow across two machines; then the secret stops sitting on disk |
| `from_prefix_v4` | int | `24` | Home LANs are /24; DHCP changes only the host part |
| `from_prefix_v6` | int | `64` | SLAAC and privacy addresses stay inside the /64 |
| `serve_path` | str | `""` (resolved `hal-remote` beside the running one, else `~/.local/bin/hal-remote`) | Absolute path is mandatory in `command=` |
| `hub_path` | str | `""` (resolved `hal-hub` in the same directory, else `~/.local/bin/hal-hub`) | Same |
| `hal_path` | str | `""` (resolved `hal` in the same directory, else `~/.local/bin/hal`) | `authorize` runs `hal gateway token ...` without relying on `PATH` |

The approval timeout and the denied packs of remote sessions are not `[remote.server]` keys:
they belong to 01's `remote` channel profile (`approval.timeout_s`, pack rules in
`policy.toml`), so there is one place to change them.

Per-profile `[tunnel]` keys are in `profile.toml` (above):

| Key | Default | Reason |
| --- | --- | --- |
| `server_alive_interval_s` | 15 | With count 3, a dead link is detected in about 45 s |
| `server_alive_count_max` | 3 | OpenSSH default; tolerates brief Wi-Fi loss |
| `connect_timeout_s` | 10 | A LAN host answers in well under 1 s; 10 s allows a slow wake |
| `backoff_initial_s` | 2 | Fast first retry after a blip, above `PerSourcePenalties` thresholds |
| `backoff_max_s` | 60 | Reconnects within a minute after the desktop or network returns |
| `stable_after_s` | 60 | A connection that held a minute was not a flapping failure |
| `health_interval_s` | 30 | Catches a dead tunnel after resume faster than keepalives alone |

### Interfaces required from other specs

`halremote` imports only `halkit` (00-overview layering), so every other dependency is a
process interface. These are the points the owners must provide; until they do, the
matching implementation step uses a fake.

| Owner | Interface | Used for |
| --- | --- | --- |
| 00 (`halkit`) | `halkit.harness`, the renderer shared with `hal connect` (RESOLUTIONS C6; 00-overview.md, section "halkit API") | `env`, `connect` |
| 00 (`halkit`) | `halkit.paths` on Windows (no `os.getuid` at import; `ensure_private_dir` skips POSIX mode checks; config and state defaults, see Decision D14) and the one runtime-dir rule (RESOLUTIONS F6) | Windows client, `serve` |
| 05-gateway | named client tokens: `hal gateway token issue`, `revoke` and `list`, persisted hashed, surviving restarts; requests carry the name for audit and gpu-light ownership (RESOLUTIONS D5/D6). This spec calls `issue` with a name and reads the token from its JSON output, `revoke` with the name, `list` for SC19 | per-client tokens (REQ-RMT-10, 11) |
| 05-gateway | `[gateway].tcp` switch (off by default, RESOLUTIONS D4), 401 without a token and 403 for a foreign `Host` | REQ-RMT-40, 46, 47 |
| 05-gateway | client mode: `[gateway].url` plus a client token file, honoured by `halkit.llm` and by a `SessionForwarder` that targets a TCP URL (RESOLUTIONS D5/D6) | laptop `hal code` (REQ-RMT-44) |
| 04-hub | `hal-hub mcp --transport stdio --channel remote --origin remote:<client>`; reads `HAL_REMOTE_ADDR` into the audit record; MCP elicitation and out-of-band approvals for `ask` (RESOLUTIONS F5, B4) | forced command |
| 01-security | `remote` channel profile meeting REQ-RMT-26; approval key, ids, scopes, surfaces and timeout (RESOLUTIONS B1 to B4); audit fields `remote_addr` and origin `remote:<client>` (B8) | remote policy and approvals |
| 03-agent | `[mcp.servers.<name>]` with `host = true` (RESOLUTIONS E6) | desktop tools on a laptop (REQ-RMT-45) |
| 02-sandbox | broker upstreams for `host = true` servers, approval requests from the upstream shown through the session's approver | desktop tools on a laptop (REQ-RMT-45) |
| 06-core-cli | `hal remote` delegation; `hal policy allow`; `scripts/install.sh` links `hal`, `hal-remote` and `hal-hub` into `~/.local/bin`; `host-setup.sh --ssh` (and `--ssh-forwarding-limits`) with the content from "Host setup"; `hal code` message on Windows | install and CLI |

### Threat model

Assets: the desktop account (and through the `docker` group, root), the secrets on the
desktop, the user's files on both machines, the gateway and its GPU, the laptop's two keys
and token.

| Threat | Entry | Contained by | Residual |
| --- | --- | --- | --- |
| LAN attacker sniffs or tampers | network | SSH encryption; host key pinned by alias with `StrictHostKeyChecking yes` (REQ-RMT-12) | none known beyond SSH itself |
| LAN attacker guesses logins | sshd on port 22 | key-only (SC03, SC04), `AllowUsers`, `PerSourcePenalties` default, optional ufw limit to the LAN | an sshd vulnerability; keep the package updated |
| Stolen tunnel key | laptop disk | `from=`, `permitopen` to the gateway port only, no session (`/bin/false`), no TCP `-R` (`permitlisten="127.0.0.1:1"`, verified), no `-L` to sockets (verified); gateway token still needed | an attacker inside the `from=` range with key and token can use the models: revoke. Without SC08's line it can also bind unix sockets with `-R` (next rows) |
| Stolen mcp key | laptop disk | `from=`, forced command with three verbs, `remote` profile (no `system`, no pack `desktop`), `ask` needs an approval from a surface | holder gets `read`/`write` tools and can answer its own elicitations: revoke; the key never gives a shell |
| Malicious process on the laptop (same user) | laptop | nothing beyond the stolen-key rows: it can read the keys | documented: the laptop account is trusted like the desktop account; keys have no passphrase (Decision D8) |
| Other local users or browser pages on the laptop | `127.0.0.1:8741` on the laptop | gateway token, Host and Origin checks (REQ-SYS-10) | none known |
| Rogue model on the desktop | model output streamed to the laptop | the model only emits tokens; laptop `hal code` runs tools in the laptop box | unsandboxed harnesses on the laptop (`env`, `connect`) run model-chosen commands with laptop privileges, as `hal connect` does on the desktop; labelled "unsandboxed" |
| Prompt injection through desktop tool output | MCP results | same as local: 01's untrusted-content envelope and, when enabled, the guard and reviewer, which `hal-hub mcp` applies (RESOLUTIONS A4, B11) | as local; with the guard off the defence is `ask` on `act`/`send` plus the domain allowlist (RESOLUTIONS F5) |
| Compromised desktop attacks the laptop | SSH connection back | no agent forwarding, no X11, `ClearAllForwardings`, no `-R` requested by the client, `PermitLocalCommand no` | MCP and model results are untrusted input to the laptop harness |
| Desktop TCP port squatting via `-R` | tunnel key | `permitlisten="127.0.0.1:1"` (verified); optionally `AllowTcpForwarding local` (SC07) | none known |
| Desktop unix socket squatting via `-R` (for example a Hal socket path while its service is stopped, to collect tokens or prompts from clients that connect to it) | tunnel key, from inside the `from=` range | `AllowStreamLocalForwarding no` when the user opts in (SC08 warns otherwise; `local` verified to block it, `no` covered by the sshd test); `from=`; revoke | without the opt-in line: open, accepted by RESOLUTIONS as a warning; see Decision D7 |
| Unix socket access via `-L` (docker.sock, session bus, broker sockets) | tunnel key | `permitopen` refuses socket targets (verified) | none known |
| Forced command bypass through env or rc files | sshd session | `restrict` (no `~/.ssh/rc`), `PermitUserEnvironment no`, SC15 for `/etc/ssh/sshrc`, minimal env in `serve` | a user rc file that misbehaves for non-interactive shells: SC14 |
| Replay or swap of an approval | MCP retry | 01's approval key over pack, tool and canonical arguments, `apr_` ids, scopes and timeout (REQ-RMT-25) | an unsandboxed laptop harness or a malicious laptop client can approve its own requests (advisory approvals, 01 residual risk R-new-4) |
| GPU denial of service from the laptop | gateway | named token gives gpu-light an owner for fair share (05-gateway) | the laptop can still use its share |
| Token disclosure in scrollback or logs | CLI output | token never printed by default, files 0600, redaction in logs (REQ-RMT-18, 36) | `--show-token` output |

### SSH over Tailscale (documented alternative)

`docs/howto/use-hal-away-from-home.md` describes, without code in `hal-remote`, running the
same pairing over a Tailscale tailnet:

1. Install Tailscale on both machines (not installed on this desktop today [verified:
   understand/critic.md V6]).
2. Pair with the desktop's tailnet name or address: `hal remote setup user@desk.<tailnet>`.
   The `from=` derivation gives the laptop's tailnet address as `/32` (100.64.0.0/10 row of
   "from= derivation"); everything else (restricted keys, forced command, tunnel, tokens)
   is unchanged.
3. Tailnet ACLs should allow only the user's laptop to reach the desktop's port 22.
4. Do not use Tailscale SSH for the mcp key: it does not apply `authorized_keys` options, so
   it is not a substitute for the forced command [unverified: Tailscale SSH documentation].
5. Do not put Tailscale Serve or Funnel in front of the gateway (Decision D17).

Trade-off: Tailscale works off the home network without opening port 22 to the internet;
plain SSH on the LAN needs no third-party daemon or account. Both keep every Hal listener on
loopback.

## Failure modes

Stance per layer: **closed** means the action does not happen.

| Layer | Failure | Behaviour | Stance |
| --- | --- | --- | --- |
| sshd | not installed or not running | `server-check` SC01/SC02 fail; `setup` reports "Connection refused" with the host-setup command | Closed |
| sshd | password login still on | SC03 critical; `authorize` refuses unless `--accept-risk SC03` | Closed |
| sshd | `AllowTcpForwarding` or `AllowStreamLocalForwarding` not limited | SC07/SC08 warn; `authorize` proceeds and records the warnings; TCP `-R` is still refused by `permitlisten`, socket `-R` is not (threat model) | Open (warned; RESOLUTIONS) |
| sshd | config not readable or `Match` blocks | checks `unknown`; critical ones block `authorize`; `--sshd-t` resolves | Closed |
| Pairing | bootstrap login fails | `setup` exits 3 and suggests `--manual` | Closed |
| Pairing | host key fingerprint not confirmed | nothing pinned, keys kept, exit 3 | Closed |
| Pairing | token already delivered or expired | `hello` returns `tokens: null`; laptop asks for `--rotate-token` | Closed |
| Pairing | `authorized_keys` lock not acquired in 10 s | exit 1, file untouched | Closed |
| Gateway | `hal gateway token issue` fails | `authorize` exits 1 after removing the lines it added | Closed |
| Gateway | TCP listener cannot be turned on (gateway down or config error) | `authorize` exits 1 after removing the lines and revoking the token it added; SC17 names the fix | Closed |
| Forced command | `hal-hub` missing or moved (reinstall) | `check` reports it; `mcp-stdio` exits 127 with a JSON error; SC13 fails | Closed |
| Forced command | unknown verb | JSON error, exit 64 | Closed |
| Forced command | session cap reached | JSON `busy`, exit 75 | Closed |
| MCP | client has no elicitation | request waits for `hal approve` on the desktop (01, RESOLUTIONS B4); unanswered within 01's timeout: deny, audited with 01's reason | Closed |
| MCP | client declines, times out, or retries with other arguments | deny, audited with 01's reason; changed arguments are a new request | Closed |
| MCP | policy file malformed | hub does not start the session (04-hub); harness sees the server exit with the error on stderr | Closed |
| Tunnel | network down, desktop asleep | transient: backoff 2 to 60 s, state `backoff`; clients get connection refused | Closed |
| Tunnel | key refused, revoked, expired, `from=` mismatch | permanent: exit 3, `status` names the fix | Closed |
| Tunnel | host key changed | permanent: exit 3; never re-pins automatically | Closed |
| Tunnel | laptop port in use | permanent: exit 3; fix: stop the laptop process that holds the port (the port must equal the desktop's, REQ-RMT-47) | Closed |
| Tunnel | tunnel up, gateway or model down | state `up`, `gateway.ok = false` with 503 detail; requests fail at the gateway | Closed |
| Tunnel | supervisor crash | systemd restarts it after 10 s; detached mode: `status` reports "not running" | Closed |
| Windows | scheduled task creation denied | Startup-folder launcher instead, with a message | Degraded (documented) |
| Windows | `icacls` fails | warning; token stays in the profile directory under the user's home | Degraded (documented) |
| Laptop | `[gateway]` table already present | `setup` prints the lines to add instead of editing | Open (user action) |

## Migration

There is no remote code in Hal today: `grep -ri "ssh\|remote"` over `Hal.next` finds only
download-size checks in `setup.sh:264-274` [verified: grep on 2026-10-23]. What this spec
replaces or touches:

| Today | Becomes |
| --- | --- |
| `repos/hal-host-setup.sh --ssh` writing `/etc/ssh/sshd_config.d/10-hal-hardening.conf` (five lines, live on this desktop) | `scripts/host-setup.sh --ssh` writing the same file plus `PermitUserEnvironment no`; forwarding limits only with `--ssh-forwarding-limits` |
| hal-hub's documented use `"hal-hub": {"command": "hal-hub", "args": ["mcp"]}` and the idea `ssh desktop hal-hub mcp` (understand/hal-hub-eco.md section 5), which needs `PATH` and offers `_confirmed` to the model | forced command `hal-remote serve` -> `hal-hub mcp --transport stdio --channel remote --origin remote:<client>`; approvals per 01 (elicitation, else `hal approve`); `_confirmed` stripped |
| nothing (planned item 4) | `packages/hal-remote` |

Existing users and config:

- No existing user has `hal remote`, so there is no config to migrate. `config.toml` gains an
  optional `[remote]` table; its absence means defaults.
- hal-employment's own SSH feature (its `docs/howto/use-hal-from-another-computer.md`) keeps
  working: it uses the user's own unrestricted key and keeps its server on `127.0.0.1:8737`.
  Its how-to has it write `sshd_config.d/hal-employment.conf` (`PasswordAuthentication no`);
  that file is not present on this desktop [verified: `ls /etc/ssh/sshd_config.d/`,
  2026-10-24], and if added it does not conflict with Hal's drop-in. Hal changes nothing
  that feature relies on by default: the forwarding limits that could affect a `-R` launcher
  are opt-in (`--ssh-forwarding-limits`) [unverified: whether its launcher uses `-R`; its
  code was not read, per the no-touch rule].
- Removed: nothing.

## Test plan

### Hermetic unit tests (default run, Linux and Windows CI)

| File | Covers |
| --- | --- |
| `test_keys.py` | `generate` argv (`-t ed25519 -N "" -C ... -f ...`), refuses existing files, fingerprint matches `ssh-keygen -lf` output for fixture keys |
| `test_authkeys.py` | goldens for both roles (tunnel line with `permitlisten="127.0.0.1:1"`), with and without `from=` and `expires`; quoting of paths with spaces; refusal of `"`/`\`/newline; `merge` keeps foreign lines byte for byte (fixture with comments, CRLF line, no final newline); `remove`; `parse` round trip |
| `test_netrange.py` | every row of the derivation table, mapped IPv6, invalid input |
| `test_sshconfig.py` | rendered file golden (POSIX and Windows paths); `ssh -G` resolution when `ssh` is on PATH (skip otherwise); argv builders contain only `ssh` |
| `test_sshdconf.py` | fixture trees: Ubuntu default plus drop-in, cloud-init `50-` file overriding order, `Include` glob order, quoted args, `Match` blocks, unreadable file |
| `test_servercheck.py` | one pass and one fail case per SC id with `FakeRunner` outputs (`systemctl`, `ss`, `ufw`), fake home with modes, fake shell for SC14 |
| `test_authorize.py` | request validation, registry writes, refusal on critical checks, SC07/SC08 warnings recorded but not blocking, `--accept-risk` recording, token issue and gateway TCP switch via a fake `hal`, rollback on failure (lines removed, token revoked), `revoke`, `--rotate-token`, `--from` update |
| `test_setup.py` | bootstrap flow with a fake `ssh` script that runs `authorize` locally against a `TempHalHome`; manual flow with fingerprint confirmation; `--rotate`; `[gateway]` append only when absent; `[mcp.servers.hal-<profile>]` written with `host = true`; refusal of differing local and remote ports |
| `test_serve.py` | each verb's stdout; unknown verb; empty env; exec argv `mcp --transport stdio --channel remote --origin remote:<client>` and env (exec patched); `runtime_dir()` error reported as JSON; session cap; delivery once and TTL |
| `test_supervisor.py` | state machine with `FakeSpawner` (scripted exit code, stderr, lifetime), `FakeClock`, `FakeProber`; backoff sequence and reset; each permanent classification; health-triggered restart; clean stop |
| `test_status.py` | status file schema, rendering, exit codes |
| `test_linux.py` | unit rendering golden; systemd detection; detached spawn and pid verification |
| `test_windows.py` | creation flags, `tasklist` parsing, `taskkill` argv, `schtasks` argv and Startup fallback, path rendering with `PureWindowsPath` (runs on Linux too; also on the Windows job) |
| `test_env.py` | goldens per harness and shell; token never inlined without `--show-token`; model names from `FakeModelServer`; `connect` exec argv and exit 2 for file-based harnesses |
| `test_cli.py` | argparse tree, exit codes, `hal remote` delegation |
| `test_output_secrets.py` | runs every command in a `TempHalHome` with a known fake token and fake keys, greps all output |
| `tests/test_host_setup.py` (root) | `host-setup.sh --ssh --dry-run` prints `10-hal-hardening.conf` with every required line, and the two forwarding lines only with `--ssh-forwarding-limits` |

Fakes needed: `FakeRunner` (records argv and timeout, returns scripted results),
`FakeSpawner`/`FakeChild`, `FakeProber`, a fake `hal` script (answers `gateway token issue`,
`revoke`, `list` and the TCP switch, issuing `test-token-<n>` tokens into a temp file), a fake `hal-hub` script (echoes its argv and env as JSON, then
serves a two-tool MCP stdio stub), a fake `ssh` script for bootstrap tests, plus
`halkit.testing` `TempHalHome`, `FakeClock`, `FakeModelServer`.

### sshd tests (marked `slow`, skipped without an sshd binary)

An unprivileged sshd on `127.0.0.1:<ephemeral>` with its own host key and config, no root,
no system changes. The fixture finds `sshd` from `HAL_TEST_SSHD`, else `/usr/sbin/sshd`
(installed on this desktop: OpenSSH_10.2p1 Ubuntu-2ubuntu3.8 [verified: `ssh -V` and the
2026-10-24 experiment]). Developer machines without openssh-server can use the package
without installing it:

```sh
apt-get download openssh-server                       # downloads the .deb, no root
dpkg-deb -x openssh-server_*.deb sshd-root            # extracts into ./sshd-root
export HAL_TEST_SSHD=$PWD/sshd-root/usr/sbin/sshd     # tests use this binary
```

Fixture config (what made the experiment work [verified: sshd experiment]):
`ListenAddress 127.0.0.1`, `Port <n>`, `HostKey <tmp>/hostkey`, `AuthorizedKeysFile
<tmp>/authorized_keys`, `PidFile <tmp>/sshd.pid`, `UsePAM no`, `PasswordAuthentication no`,
`StrictModes no` (temp directories have sticky world-writable parents), `SshdSessionPath`
and `SshdAuthPath` pointing next to the binary when it is an extracted package (OpenSSH 10
splits these out; the installed binary finds them in `/usr/lib/openssh`), plus the
hardened lines under test; started with `-D -e` in a subprocess (daemon mode reset every
connection in the experiment, cause not investigated). Each test has a 60 s timeout.

| File | Covers |
| --- | --- |
| `test_sshd_restrictions.py` | tunnel key: permitted port works, other port denied, socket `-L` denied, `-R` TCP denied by `permitlisten` with server defaults, `-R` socket allowed with server defaults (documents the risk) and denied with `AllowStreamLocalForwarding no` and with `local`, exec gets exit 1; mcp key: client command ignored, `-L` denied, PTY denied; `from=` mismatch refused |
| `test_sshd_pairing.py` | full `setup` bootstrap against the fixture (bootstrap login with a separate unrestricted test key), `hello` delivers once, host key change refused |
| `test_sshd_serve.py` | forced command with the real login shell and a `HOME` whose `.bashrc` prints before its interactive check (SC14 fails) and after it (passes) |
| `test_sshd_tunnel.py` | supervisor against the fixture and `FakeModelServer` behind a token check: up, health ok, 401 without token, kill sshd -> backoff -> reconnect, revoke -> permanent |
| `tests/e2e/test_flow_remote.py` | Flow 6 end to end (REQ-SYS-22): tunnel, `env openai` settings used by a small client against `FakeModelServer`, MCP over the mcp key to the fake hub, elicitation approve and deny, a client without elicitation answered by `hal approve`, audit record with origin `remote:<client>` and `remote_addr`; the two `docker` cases of REQ-RMT-44 and 45 |

### Windows CI job

`windows-latest`, Python 3.13: installs `hal-kit` and `hal-remote` only, runs the unit tests
(the POSIX-only ones skip with a reason), and one `slow` test that generates a key with the
runner's `ssh-keygen.exe` and runs `ssh.exe -G -F` on the rendered config [unverified: the
runner image ships OpenSSH client; check when adding the job].

### Manual checks (`docs/dev/remote.md`)

- M1 Desktop: `host-setup.sh --ssh` on a fresh install and on this desktop (existing
  `10-hal-hardening.conf`), `server-check` all critical pass, SC07/SC08 warn without
  `--ssh-forwarding-limits` and pass with it.
- M2 Linux laptop: bootstrap and manual pairing; `up --enable`; reboot; suspend and resume;
  `status` within 60 s of resume.
- M3 Windows 11 laptop (the user's own machine, not the hal-vm): install with `uv tool
  install` from the git tag, manual pairing, key ACLs accepted by `ssh.exe`,
  quoted forward-slash paths, `up`, `down`, Claude Code with `env claude` and the MCP entry.
- M4 Windows: `up --install-task` as a non-admin user; logon start.
- M5 Revoke from the desktop while the tunnel runs: laptop state `failed` with the fix.
- M6 Latency: 20 prompts from the laptop vs on the desktop, same model; report TTFT p50 and
  tokens/s difference.

## Implementation plan

Each step is one commit, green on its own, in this order. Subjects follow the brief
(`<Verb> <object>`, at most 60 characters). Steps 1 to 15 need no other spec's code beyond
`halkit` (the gateway and hub are faked); steps 16 to 21 wait for the interface named in each
step.

1. `Add hal-remote package with CLI skeleton` - pyproject (console and GUI scripts),
   module headers, `cli.main` with subcommands that exit 2 "not implemented", `[remote]`
   table registration, `test_cli.py`.
2. `Add public key parsing and ed25519 generation` - `keys.py`, `test_keys.py`.
3. `Add authorized_keys rendering and merging` - `authkeys.py`, goldens.
4. `Add from= range derivation` - `netrange.py`, tests.
5. `Add client ssh_config rendering` - `sshconf.py` incl. ssh discovery, tests with `ssh -G`.
6. `Add read-only sshd_config reader` - `sshdconf.py`, fixture trees.
7. `Add hal remote server-check` - `servercheck.py` SC01 to SC22 except SC14, tests.
8. `Add hal-remote serve forced command` - `serve.py` verbs, minimal env, exec, session cap,
   SC14 in `servercheck.py`, tests with the fake hub.
9. `Add sshd test fixture for restricted keys` - `tests/sshd/conftest.py`,
   `test_sshd_restrictions.py`, `test_sshd_serve.py`, marker wiring, dev doc snippet.
10. `Add remote client authorize, revoke and registry` - `authorize.py` (named client token,
    gateway TCP switch, rollback), fake `hal`, tests. Runs against the fake until 05-gateway's
    named tokens land; the fake follows 05's CLI as published.
11. `Add hal remote setup with bootstrap and manual pairing` - `pairing.py`, known_hosts
    pinning, `hello`, `[gateway]` client-mode append, `test_setup.py`,
    `test_sshd_pairing.py`.
12. `Add tunnel supervisor with backoff and health probe` - `supervisor.py`, status file,
    `test_supervisor.py`, `test_status.py`.
13. `Add tunnel up, down and status on Linux` - `platform_linux.py`, unit template,
    `test_linux.py`, `test_sshd_tunnel.py`.
14. `Add Windows tunnel control and logon start` - `platform_windows.py`, `test_windows.py`.
15. `Add Windows CI job for hal-remote` - workflow job, skip reasons.
16. `Add hal remote env and connect` - `envout.py` on `halkit.harness` (lands after the
    00-overview renderer commit; until then the step is blocked, not faked, so the two
    commands share one renderer from the start), goldens.
17. `Add secret leak test for hal-remote output` - `test_output_secrets.py`.
18. `Update host setup SSH drop-in` - `scripts/host-setup.sh` content of
    `10-hal-hardening.conf` (adds `PermitUserEnvironment no`), opt-in
    `--ssh-forwarding-limits`, `tests/test_host_setup.py`.
19. `Add remote channel cases to policy tests` - the REQ-RMT-26 cases in 01-security's test
    file (after its policy engine lands).
20. `Add remote flow end-to-end test` - `tests/e2e/test_flow_remote.py` (after 04-hub's
    `--origin`, `--channel remote` and 01's approvals land).
21. `Add desktop tools as laptop MCP server` - `setup` writes
    `[mcp.servers.hal-<profile>]` with `host = true`; docker-marked e2e case (after
    03-agent's host servers and 02-sandbox upstreams land).
22. `Add how-to for using Hal from a Linux laptop` - `docs/howto/use-hal-from-a-laptop.md`.
23. `Add how-to for using Hal from a Windows laptop` -
    `docs/howto/use-hal-from-a-windows-laptop.md` (install with `uv tool install` from the
    git tag, pairing, task).
24. `Add how-to for using Hal away from home` - `docs/howto/use-hal-away-from-home.md` (SSH
    over Tailscale).
25. `Add remote design and threat model to dev docs` - `docs/dev/remote.md` (protocol,
    manual checklist M1 to M6), remote section of `docs/dev/threat-model.md`, docs index
    rows, CHANGELOG entry.

## Decisions

- **D1 The sandbox runs where the code lives.** See "What runs where". The desktop-side
  option needs nothing new: an ordinary SSH session and `hal code`.
- **D2 Windows is client-only; `hal code` there means WSL2 or SSH to the desktop.** Reason:
  the box relies on Linux namespaces and unix-socket bind mounts (02-sandbox), and
  00-overview lists a Windows sandbox as a non-goal.
- **D3 One forced command with three verbs, exec into `hal-hub`.** Reason: one key can then
  serve MCP, the health check and token delivery, while `SSH_ORIGINAL_COMMAND` never reaches
  a shell; exec keeps `halremote` importing only `halkit`. The forced command is
  `hal-remote serve`, not `hal-hub` directly, because token delivery and the check belong to
  this package.
- **D4 Tokens are delivered once over the new mcp key, never in the bootstrap response.**
  Reason: the first use of the restricted key proves the lines work, and the secret crosses
  only the restricted channel; one-time delivery keeps no long-lived plaintext copy on the
  desktop beyond the gateway's own store.
- **D5 Two pairing flows.** Reason: after `host-setup.sh --ssh` password login is off, so the
  one-command bootstrap only works for users who already have a key on the desktop; the
  manual flow covers the rest with two short copy steps.
- **D6 Own `ssh_config` and `known_hosts` per profile, used with `-F`.** Reason: the user's SSH
  configuration cannot change Hal's connection, and Hal never edits the user's `~/.ssh`.
- **D7 The tunnel key carries `permitlisten="127.0.0.1:1"`; the server-wide forwarding
  limits are recommended, not required.** Reason (RESOLUTIONS, open questions settled): the
  key option refuses every usable TCP `-R` listener without changing sshd for the user's
  other keys (hal-employment's remote feature may use `-R`). SC07 and SC08 warn, and
  `host-setup.sh --ssh --ssh-forwarding-limits` writes the lines. The 2026-10-24 experiment
  showed `permitlisten` does not stop unix-socket `-R`. Residual risk: without the opt-in
  line, a stolen tunnel key used from inside the `from=` range can bind a unix socket at a
  free path the user can write (for example a Hal socket while its service is stopped).
  This is accepted as a warning (SC08) per RESOLUTIONS; the threat model lists it, and
  `revoke` plus `from=` bound it.
- **D8 Keys have no passphrase.** Reason: the tunnel and MCP start unattended (logon task,
  harness spawning ssh); a passphrase would need an agent, which `IdentityAgent none` keeps
  out of the picture. Damage from a stolen key is bounded by the restrictions; `revoke` and
  `--expires` exist.
- **D9 Remote approvals are 01's host-harness approvals, nothing more.** A remote `ask` goes
  to MCP elicitation when the client supports it, else waits for `hal approve` on the
  desktop, with 01's key, ids, scopes and timeout (RESOLUTIONS B1 to B4). Reason: one
  approval implementation; the brief allows approvals only from a host surface, never from
  the model. They are advisory against a compromised laptop (R-new-4).
- **D10 The `remote` profile denies `system` and pack `desktop`.** Reason: the user is not in
  front of the desktop; power and session actions there are the most surprising remote
  effects. 01 owns the profile; a user can allow named tools in `policy.toml`. There is no
  `[remote.server]` copy of these settings.
- **D11 The supervisor does its own backoff; systemd and the scheduled task only start it.**
  Reason: one implementation on both OSes, and permanent errors stop retries
  (`RestartPreventExitStatus=3`).
- **D12 TCP-only forwards, same port numbers on the laptop.** Reason: Windows has no socket
  forwarding to rely on, the desktop refuses socket forwards, the gateway accepts only its
  own `127.0.0.1:<port>` as `Host` (403 otherwise), and same ports keep `hal connect` output
  valid on the laptop.
- **D13 The hub HTTP port 8742 is not forwarded.** Reason: it uses one shared bearer token
  (`hub-token`) with no per-client revocation, and a request through it would not carry the
  `remote` channel or a `remote:<client>` origin; stdio over SSH covers every listed harness
  and gets both. SC16 still checks 8742 is loopback-only.
- **D14 Windows config and state paths are XDG-style under the home directory.** Reason: one
  layout on both OSes is easier to document. 00-overview owns the final Windows defaults
  (RESOLUTIONS F6 fixes only the runtime dir, which the client does not use); if 00 picks
  others, this spec follows.
- **D15 sshd integration tests use an unprivileged sshd, marked `slow`.** Reason: no root,
  no container image download, and the same binary Ubuntu ships; the marker list is fixed by
  the brief.
- **D16 No console, gpu-light, TTS or hal-employment forwards in v1.** Reason: each widens
  the tunnel key's reach; the console has its own CSRF history.
- **D17 Away from home means SSH over Tailscale, not Tailscale Serve.** Reason: Serve would
  put a non-loopback path in front of the gateway, whose `Host` check accepts only
  `127.0.0.1:<port>`, and would bypass the restricted keys; running the same pairing over
  the tailnet keeps one transport and one threat model.
- **D18 Install from git tags.** Reason: RESOLUTIONS (no PyPI in this release); the Windows
  how-to uses `uv tool install` with a `git+https://...@v0.1.0#subdirectory=packages/hal-remote`
  source, with `hal-kit` pinned `>=0.1,<0.2` (RESOLUTIONS C3).
- **D19 Manual Windows checks run on the user's own Windows machine, not the hal-vm.**
  Reason: RESOLUTIONS ("It does not use the hal-vm"); the VM belongs to the hal-employment
  session.

## Open questions

1. Which laptops will you use (Linux, Windows, both), and is the laptop always on the same
   home network as the desktop? This sets the default `from=` range and whether the
   away-from-home (Tailscale) how-to should be the primary path for you.
