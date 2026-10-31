# hal-remote developer notes

How the SSH add-on is built and tested. The design is [07-remote.md](../design/07-remote.md).
Steps 18 to 21 of the plan (host setup script, policy cases, end-to-end flow, desktop tools as a
laptop MCP server) and `env`/`connect` (steps 16 and 17) wait for code in other packages; the
sections below describe what is built.

## Modules

| Module | Job |
| --- | --- |
| `keys`, `authkeys`, `netrange` | Key parsing and generation, `authorized_keys` lines, `from=` ranges |
| `sshconf`, `sshdconf` | The client `ssh_config`, ssh discovery, a read-only `sshd_config` reader |
| `servercheck` | `server-check`, checks SC01 to SC22 |
| `serve` | The forced command with the verbs `mcp-stdio`, `hello` and `check` |
| `authorize` | Desktop side of pairing: keys, registry, token delivery |
| `pairing` | Laptop side: `setup`, profile files, host key pinning, `hello`, `[gateway]` lines |
| `supervisor`, `tunnelrun` | The tunnel loop with backoff and health probe, and its entry point |
| `platform_linux`, `platform_windows` | `up` and `down`: systemd unit or detached process; detached process and logon task |

## Protocol

1. The laptop makes a tunnel key and an mcp key (`ssh-keygen`, no passphrase, Decision D8).
2. Bootstrap: `ssh USER@HOST hal-remote authorize --client NAME --stdin --json` with the public
   keys on stdin (schema `hal.remote.authorize-request/1`). The reply
   (`hal.remote.authorize/1`) carries the host keys, never a token. Manual: the user runs
   `hal remote authorize` on the desktop and compares the host key fingerprint on the laptop.
3. The laptop pins the host keys in its own `known_hosts` under the alias `hal-<profile>` and
   writes `profile.toml` and `ssh_config`.
4. The laptop calls `hello` over the mcp key. The reply (`hal.remote.hello/1`) holds the gateway
   token once; the desktop deletes the pending delivery as it reads it.
5. `up` keeps `ssh -N` running through `supervisor`; `status.json` records the state and
   `status` exits 4 unless it is `up` and the gateway answers.

The wire formats and the `authorized_keys` options are specified in
[07-remote.md](../design/07-remote.md); this page does not repeat them. The threat model is in
[threat-model.md](threat-model.md), section "Remote (SSH add-on)".

## Manual checks

These need real machines. Record the date and the result in the pull request.

- M1 Desktop: `host-setup.sh --ssh` on a fresh install and on an existing one; `server-check`
  passes every critical check.
- M2 Linux laptop: bootstrap and manual pairing; `up --enable`; reboot; suspend and resume;
  `status` within 60 s of resume.
- M3 Windows 11 laptop: install with `uv tool install` from the git tag, manual pairing, key
  ACLs accepted by `ssh.exe`, `up`, `down`.
- M4 Windows: `up --install-task` as a non-admin user; logon start.
- M5 Revoke from the desktop while the tunnel runs: the laptop state becomes `failed` with a fix.
- M6 Latency: 20 prompts from the laptop and on the desktop with the same model; report the
  first-token time p50 and tokens per second.

## sshd tests

The tests in `packages/hal-remote/tests/sshd/` start an unprivileged `sshd` on
`127.0.0.1:<free port>` with its own host key, config and `authorized_keys` in a temp
directory. They need no root and change nothing on the host. They carry the `slow` marker, so
the default run skips them:

```sh
uv run pytest -m slow packages/hal-remote/tests/sshd   # needs an sshd binary
```

The fixture uses `$HAL_TEST_SSHD`, else `/usr/sbin/sshd`, and skips when neither exists. A
machine without `openssh-server` can use the package without installing it:

```sh
apt-get download openssh-server                       # downloads the .deb, no root
dpkg-deb -x openssh-server_*.deb sshd-root            # extracts into ./sshd-root
export HAL_TEST_SSHD=$PWD/sshd-root/usr/sbin/sshd     # tests use this binary
```

| Symptom | Fix |
| --- | --- |
| Every sshd test is skipped | Install `openssh-server` or set `HAL_TEST_SSHD`. |
| `sshd did not start` | Read the log it prints; an extracted package needs `SshdSessionPath` and `SshdAuthPath` next to the binary (the fixture adds them). |
| A forced-command test prints nothing | Check `serve.log` under the test's temp `state` directory. |
