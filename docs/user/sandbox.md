# The sandbox

Every coding session runs hal-agent in a box. The box has no network, a read-only root, no
capabilities and your uid. Its only ways out are two unix sockets: the broker (tools, through
`hal-hub`) and the model socket (the model API only). This page says what you can set and what
the box can see. Design and requirement ids: [design/02-sandbox.md](../design/02-sandbox.md).

## Lockdown levels

`[sandbox].lockdown` is changed by you, in the config file or with `hal config set`. A tool
call cannot change it.

| Level | Backends | Project in the box | Other rules |
| --- | --- | --- | --- |
| `strict` | docker with runsc only; refuses to start without it | isolated copy only | pids, memory and CPU limits; host MCP upstreams only through `hal-hub`; classifier paranoia floor `high` |
| `standard` (default) | docker, runsc when installed, else runc with a warning | isolated copy by default | limits on |
| `relaxed` | also bubblewrap | `--direct` allowed: the real project mounted read-write, checkpoints on | dependency-prep egress box allowed |
| `off` | also `none` (no isolation) | as `relaxed` | one-time yes per project, typed on a terminal; red banner at every start |

Backend `none` runs hal-agent as a host process with no `run` tool and with file access confined
to the project. It is never chosen automatically.

## What the box sees

| Path | What it is |
| --- | --- |
| `/work` | An isolated copy of the project (default for `hal code`), or the real project with `--direct`. Files that match the mask list (`.env`, keys) are empty and read-only in the box. |
| `/scratch` | A per-project directory that persists between sessions (`hal scratch clean` empties it). Chat and voice sessions, and jobs without a project, get only this: there is no `/work`, and the agent starts in `/scratch`. |
| `.git` | Mounted read-only with `--direct`; the isolated copy has none. |
| `/run/hal` | The sockets, read-only. |

## Getting changes out

The agent never writes to your repository. In the default mode its work stays in the copy until
you run `hal code accept SID` (shows the stat and risk flags, takes a checkpoint, applies the
patch all or nothing) or `hal code export SID FILE` (patch, or `--bundle` for a git bundle; it refuses an existing FILE unless `--force`).
`hal code diff SID` shows the patch and the flags without applying anything. A high-risk flag
(CI files, `.envrc`, git metadata, a link out of the project, a private key) stops `accept`
until you pass `--allow-flags`.

## Checkpoints

A session takes checkpoint 0 at start and one before each turn. They are git commits under
`refs/hal/checkpoints/<session>/<n>` in your repository, or in a shadow repository under the
state dir for a folder without git. They never touch your index or branches. A session started
without checkpoints cannot undo or diff.

## Running an agent CLI

`hal box -- CLI [ARGS]` runs claude, qwen, opencode, codex, crush or goose in the same box, on an
isolated copy of the project, with no network. The CLI talks to the model through the relay on
`127.0.0.1:8741` and to Hal tools through `hal-mcp-bridge`. `--no-mcp` leaves the bridge out of the CLI's config, so it gets no Hal tools (verified by `test_no_mcp_leaves_the_broker_bridge_out_of_every_cli_config`). `--ask` keeps the CLI's own prompts;
`--direct` mounts the real project (lockdown `relaxed` or lower). Docker is the only backend.

```sh
hal box image build         # the base image; then `hal box image build crush`
hal box harnesses           # which CLIs are built
hal box doctor              # lockdown, backend, runtime and image sizes (exit 1 on a failed check)
hal box ls                  # running boxes
hal box gc [--dry-run]      # remove orphaned boxes and empty session dirs
hal box forget [DIR]        # drop saved per-project choices
hal box -- crush --version  # extra arguments after -- reach the CLI unchanged
```

A box image missing: the error names `hal box image build`. The image bundles Hal's own agent
code, so after you update a Hal checkout `hal doctor` warns (`image freshness`) until you build
it again. `hal box deps` installs project
dependencies in the egress-limited prep box after you approve each lockfile.

## Status

- Verified by tests with fakes: argv for docker and bubblewrap (golden files), the box entry,
  relay and MCP bridge, the session lifecycle. A bubblewrap run (`pytest -m bwrap`) passed on the
  development host on 2026-10-30.
- Verified against the real docker daemon on 2026-10-30, under runc and runsc (`--host-uds=open`):
  `hal code` end to end with a fake model server, the containment suite, and `hal box` starting
  crush and goose (`--version`). Check with `pytest -m docker` once the images are built.
- Not verified: a model round trip through each CLI (claude, qwen, opencode, codex) and the
  other four box images.
