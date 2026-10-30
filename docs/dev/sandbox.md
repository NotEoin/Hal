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
uv run --offline hal code --build                          # builds hal-sandbox:full-<hash>, tags hal-agent:local
uv run --offline pytest -q -m docker tests/e2e             # containment suite and `hal code` end to end
uv run --offline pytest -q -m "docker and network" packages/hal-sandbox/tests/test_deps_live.py
```

The `tests/e2e` cases run under `runc` and under `runsc` when docker has it registered (with
`"runtimeArgs": ["--host-uds=open"]`, see docs/howto/install-gvisor.md). Verified 2026-10-30 on
this machine: 36 docker cases pass under both runtimes. Not yet covered (RESOLUTIONS I6): audit
checks on each broker attempt, forged approvals over the approval surfaces, self-raised lockdown or
paranoia through a tool call, and smuggled instructions in tool results.

Regenerate a golden file only after reading the diff: it is the reviewed list of flags.

## What is not built

- `Session` accepts an `approver` and does not use it; approvals still come from the `halhub mcp` child.
- Sanitised git config (REQ-BOX-15), the terminal filter, box images for agent CLIs and `hal box`
  are later steps of the plan.
- Dependency prep: `deps.prepare(work, approve=...)` works and is tested against docker (runc and runsc,
  `test_deps_live.py`, needs network). `approve` is a callable, not `halhub`'s `Approver`; halcore builds
  the `sandbox_deps_prep` request. There is no `hal code --deps` flag or `hal-sandbox deps` command yet,
  and the "done" record is `deps.json`, not a key of `projects.json`. Only `uv` is proven live; `npm`
  needs `[sandbox].node = true` and `cargo` needs it in `extra_apt_packages`, both unverified.
- Images: `images.build` makes `sandbox` and `sandbox-slim` (optional node); `hal code --build` calls it.
  There is no `hal box image build` command yet (step 32), no per-CLI image and no custom-image check.
  `images/requirements-box.txt` is `uv export --no-dev --package hal-agent --no-emit-workspace`; regenerate
  it when `uv.lock` changes. Measured 2026-10-30 with `docker images`: slim 511 MB and full 869 MB, over
  the 450 and 850 MB of REQ-BOX-26, so `SIZE_BUDGET_MB` holds 520 and 880 until the image is trimmed.
- Upstream elicitations are not mapped to host approvals (REQ-BOX-77); an upstream that asks fails.
- Each broker connection starts its own upstream processes, and a failed start drops that upstream
  for the connection. `sweep_if_due` is not called at `hal` start yet.
- Upstream names must match `^[a-z][a-z0-9]{0,31}$` (the hub's rule), so `hal-desktop` is refused.
- The runsc probe still connects with `python3` in the image, not `hal-box-entry --probe`.

## Box images for agent CLIs

`boximages.py` builds `hal-box-<cli>:<hash>` on the local `hal-sandbox` image. npm CLIs install with
`npm ci` from `images/box/<cli>/package-lock.json` (integrity hashes, generated with
`npm install --package-lock-only`); goose is a tarball with a pinned sha256. Versions live in
`images/pins.toml` under `[cli.*]` and must be 14 days old (REQ-BOX-24). The Dockerfile names the base
by tag because buildkit cannot `FROM` a bare image ID; the tag hash covers the base image ID.
Measured 2026-10-30 [verified: `docker images`]: crush 1.25 GB, goose 1.45 GB (budget 1.5 GB). The
other CLIs were not built here. Tested by `test_boximages.py` and the live tests in `test_images_live.py`.

## What remains (2026-10-30)

Plan steps 29 to 33 are done; `hal box` and `hal-sandbox` exist with `run`, `harnesses`, `image`
and `deps`. Not built yet:

- `hal-sandbox shell`, `undo` and `diff` (step 32; `doctor`, `ls`, `gc` and `forget` exist), so the undo
  how-to uses `hal code` and git refs.
- The startup benchmark `scripts/bench-sandbox.py` (step 34), and steps 35 to 38: sanitized git
  config, the terminal filter, upstream elicitations, stripped command output.
- Box image sizes: the base images are above their budgets (see `images.SIZE_BUDGET_MB`); only
  crush and goose were built and run. The claude, qwen, opencode and codex images are unbuilt, and no
  CLI has done a model round trip (REQ-BOX-61).
- The open RESOLUTIONS I6 containment cases listed above.
