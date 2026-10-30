# hal-sandbox

Hal sandbox: broker, container backends, images, hal box and checkpoints. Import name: `halsandbox`.

Status: skeleton. The package installs and carries its version; behaviour lands in later
phases of the implementation plan in
[docs/design/00-overview.md](../../docs/design/00-overview.md).

Run the tests from the repository root:

```sh
uv sync --all-packages
uv run pytest packages/hal-sandbox
```

## Usage: `hal code`

`hal code [DIR]` runs `hal-agent` in a docker container on DIR (default: current directory).

- Container: `--network none`, read-only root, `--cap-drop ALL`, `no-new-privileges`, host uid,
  runtime `runsc` when docker has it, else `runc` with a warning (`[sandbox].lockdown = "strict"` refuses `runc`). By default `/work` (rw) is an isolated copy of DIR (see Isolated sessions) and the real project is never mounted; `--direct` mounts DIR itself and needs lockdown `relaxed` or lower (then `/work/.git` is read-only).
- Sockets: a private directory under `$XDG_RUNTIME_DIR/hal/` is mounted read-only at `/run/hal`.
  `model.sock` forwards only `POST /v1/chat/completions` and `GET /v1/models` to the local model
  server (`--upstream URL`, env `HAL_CODE_UPSTREAM`, default `http://127.0.0.1:8080`).
  `hub.sock` serves `hal-hub mcp` on the host, so pack tools use the host policy, approvals and audit.
- Approvals wait on the host: answer with `hal-hub approve ID` in a host terminal.
- `hal code --build` builds the `hal-sandbox:full-<hash>` image from `images/` (pinned base digest, hash-pinned wheels, `--selftest` after the build) and tags it `hal-agent:local`. It is the only step that uses the network.
- Extra agent arguments go after `--`: `hal code . -- -p "fix the tests"`.
- Docker tests: `pytest -m docker packages/hal-sandbox`.

`hal claude` starts Claude Code as before. It is not private and not sandboxed.

## Limits and scratchpad

- Limits (`[sandbox.limits]` memory, cpus, pids, tmp_size): memory equals swap, default `min(8 GiB, RAM/4)`,
  cpus `min(4, nproc/2)`, pids 1024. `tmp_size` must stay below `memory`.
- Every box gets `/scratch` (rw), backed by `$STATE/scratch/<project>-<hash>/`, kept between sessions.
  A session refuses to start when it is over `scratch_max_bytes` (2 GiB); the cap is checked at start
  because a bind mount cannot enforce one.

## Lockdown

`[sandbox].lockdown` is `strict`, `standard` (default), `relaxed` or `off`. Only the user sets it, in the
config file on the host; there is no flag or environment variable for it.

| Level | Runtime | `--direct` and egress prep | Backend none | Host MCP upstreams |
| --- | --- | --- | --- | --- |
| strict | runsc required | no | no | `hal-hub` only |
| standard | runsc if installed, else runc plus a warning | no | no | per policy |
| relaxed | as standard | yes | no | per policy |
| off | as standard | yes | yes, after per-project consent | per policy |

## Isolated sessions

`halsandbox.isolated` copies the project to `$STATE/isolated/<sid>/work` (no `.git`, no symlinks, no
secret files such as `.env*` and `*.pem`, no `node_modules` or `.venv`) and keeps a base snapshot in a
shadow repository. The box mounts only that copy. `accept` shows the changed paths with risk flags,
refuses high-risk paths (CI files, git config, hooks, symlinks, private keys) unless allowed, takes a
checkpoint of the project in `$STATE/checkpoints/<project>`, then runs `git apply` all or nothing.
`export` writes a patch or a git bundle and never touches the project.

Review with `hal code ls`, `hal code accept SID [--allow-flags]`, `hal code export SID FILE [--bundle]`
and `hal code reject SID`. `hal scratch clean [DIR]` empties scratchpads.

## Broker limits

The hub socket accepts only the broker's own uid (`SO_PEERCRED`), newline-framed messages of at most
4 MiB, a partial message for at most 30 s, and 8 connections at a time. Anything else is dropped and
logged; an unterminated last message is never forwarded.

The model socket has the same guards: own uid only, 8 concurrent requests, a client that stalls for
30 s is cut off, and only the two allowed routes reach the upstream.
