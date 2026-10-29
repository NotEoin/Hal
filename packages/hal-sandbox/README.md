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
  runtime `runsc` when docker has it else `runc`. DIR is `/work` (rw) with `/work/.git` read-only.
- Sockets: a private directory under `$XDG_RUNTIME_DIR/hal/` is mounted read-only at `/run/hal`.
  `model.sock` forwards only `POST /v1/chat/completions` and `GET /v1/models` to the local model
  server (`--upstream URL`, env `HAL_CODE_UPSTREAM`, default `http://127.0.0.1:8080`).
  `hub.sock` serves `hal-hub mcp` on the host, so pack tools use the host policy, approvals and audit.
- Approvals wait on the host: answer with `hal-hub approve ID` in a host terminal.
- `hal code --build` builds the image `hal-agent:local` (needs network for pip once).
- Extra agent arguments go after `--`: `hal code . -- -p "fix the tests"`.
- Docker tests: `pytest -m docker packages/hal-sandbox`.

`hal claude` starts Claude Code as before. It is not private and not sandboxed.
