# Sandbox developer notes

How `packages/hal-sandbox` fits together and how to change it. User view: [../user/sandbox.md](../user/sandbox.md).
Requirements: [../design/02-sandbox.md](../design/02-sandbox.md).

## Modules

| Module | Job |
| --- | --- |
| `session.py` | `Session.start` prepares backend, work tree, masks, runtime dir, checkpoint 0, broker and lifeline; `close` tears down. It does not spawn the box. |
| `backends/docker.py` | `docker_argv(LaunchSpec)`: pure, golden-tested (`tests/golden/docker_code.txt`). |
| `backends/bwrap.py` | `bwrap_launch(LaunchSpec)`: namespaces, optional `systemd-run --user --scope` limits. |
| `backends/none.py` | `none_launch`: host argv with box paths mapped to host paths. |
| `boxside/` | Stdlib-only `hal-box-entry`, `hal-relay`, `hal-mcp-bridge`; each also runs as a lone file in the image. |
| `detect.py`, `lockdown.py` | Backend choice, probe cache, lockdown gates. |
| `upstreams.py`, `hubchild.py` | Host MCP servers (`[mcp.servers.<name>]` with `host = true`) as hub providers. The broker runs each hub child as `python -m halsandbox.hubchild`, which adds them through `halhub.cli.HUB_HOOKS`. Each connection starts its own upstream processes. Strict lockdown offers none. |
| `egress.py` | `ConnectProxy`: CONNECT-only unix-socket proxy for the dependency-prep box; listed registries on 443, addresses checked with `halkit.netguard.resolve_checked`. Not wired to a session yet (needs `deps.py`). |
| `labels.py` | Container labels and the orphan sweep (`sweep`, `sweep_if_due`): dead own boxes, dead runtime dirs, empty isolated copies older than 7 days. |
| `broker.py`, `transport.py`, `lifeline.py` | Line-framed unix socket server with peer rules, broker over it, box shutdown socket. |
| `checkpoints.py`, `shadow.py`, `diffreport.py`, `gitlayout.py` | Checkpoints, diff report and flags, read-only git mounts. |
| `isolated.py`, `code.py` | The isolated copy with accept/export/reject; `run_code` ties Session and the model forwarder together. |

## Adding a backend

1. Write `backends/<name>.py` with a pure function `LaunchSpec -> Launch`.
2. Register it in `session.ARGV_BUILDERS`.
3. Add a golden file under `tests/golden/` and a test that pins forbidden flags.

## Tests

```bash
uv run --offline pytest -q packages/hal-sandbox   # hermetic: fake docker scripts, injected probes
uv run --offline pytest -q -m bwrap packages/hal-sandbox   # needs unprivileged bubblewrap
uv run --offline pytest -q -m docker packages/hal-sandbox  # needs a daemon and the image
```

Regenerate a golden file only after reading the diff: it is the reviewed list of flags.

## What is not built

- `Session` accepts an `approver` and does not use it; approvals still come from the `halhub mcp` child.
- Sanitised git config (REQ-BOX-15), the terminal filter, host MCP upstreams, labels and orphan sweep,
  images and `hal box` are later steps of the plan.
- The runsc probe still connects with `python3` in the image, not `hal-box-entry --probe`.
