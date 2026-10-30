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
| `/scratch` | A per-project directory that persists between sessions (`hal scratch clean` empties it). |
| `.git` | Mounted read-only with `--direct`; the isolated copy has none. |
| `/run/hal` | The sockets, read-only. |

## Getting changes out

The agent never writes to your repository. In the default mode its work stays in the copy until
you run `hal code accept SID` (shows the stat and risk flags, takes a checkpoint, applies the
patch all or nothing) or `hal code export SID FILE` (patch, or `--bundle` for a git bundle).
`hal code diff SID` shows the patch and the flags without applying anything. A high-risk flag
(CI files, `.envrc`, git metadata, a link out of the project, a private key) stops `accept`
until you pass `--allow-flags`.

## Checkpoints

A session takes checkpoint 0 at start and one before each turn. They are git commits under
`refs/hal/checkpoints/<session>/<n>` in your repository, or in a shadow repository under the
state dir for a folder without git. They never touch your index or branches. A session started
without checkpoints cannot undo or diff.

## Status

- Verified by tests with fakes: argv for docker and bubblewrap (golden files), the box entry,
  relay and MCP bridge, the session lifecycle. A bubblewrap run (`pytest -m bwrap`) passed on the
  development host on 2026-10-30.
- Not verified: a real docker run with the shipped image, runsc with `--host-uds=open`, the
  lifeline in a real box. Check with `pytest -m docker` once the image is built.
