# Hal system overview

This spec describes Hal as a whole: what it is for, how its repos and packages fit together,
how a request travels through it end to end, and where configuration, ports and sockets live.
It also defines the shared `halkit` library, the cross-cutting requirements every package must
meet, the release rules, and the index of the component specs `01` to `08`.

Status: draft 2026-10-24 (revised to match the binding resolutions of 2026-10-23,
the design resolutions of 2026-10-23; ids such as A1, B4, F6, R-new-9 below
refer to that file and its reviews; "architect F3" means the security-architect finding, a
bare F-id the CLI section, and "resolution D4" a gateway resolution rather than this spec's
own Decisions D1 to D17)

## Scope and non-goals

This spec owns:

- the product definition and the audiences it serves;
- the repo map (Hal, hal-tools, hal-memory, hal-voice, gpu-light, and the hal-employment
  contract) and how the repos combine at install and at run time;
- the package map of the monorepo, its dependency graph, the import rules for packs, and the
  test that enforces the graph;
- the `halkit` public API (config, paths, atomic writes, time, logging, subprocess, secrets,
  redaction, canonical JSON, terminal-safe text, beneath-root file access, egress checks,
  dates, notifications, harness config rendering, gateway client, test fakes);
- the end-to-end flows, as sequences across components, including where the assistant agent
  runs (A1); each component spec owns the inside of its own box;
- configuration files, environment variables and precedence, and the shared `[hal]` and
  `[compat]` tables;
- the runtime-directory rule (F6) and the ports and sockets table;
- the fail-open or fail-closed stance of each layer, as a summary (01-security is
  authoritative where they differ);
- the release rules (REQ-SYS-24);
- the requirements index and the topic ownership of the other specs.

Non-goals for this release (`0.1.0`):

- Multi-user or server deployments. Hal is one user on one desktop, with an optional laptop.
- A sandboxed agent on Windows or macOS hosts. Windows and Linux laptops are supported only as
  `hal remote` clients (07-remote).
- Agent-to-agent protocols (A2A). MCP, ACP and an OpenAI-compatible model API cover every
  flow below.
- A cloud safety classifier, or any network call made by Hal itself without the user opting in.
- Protection against a compromised host kernel, GPU driver, Docker daemon or user account.
  These are documented as residual risks (01-security).
- Changes to hal-employment. Hal keeps the contract in section "Compatibility contract".
- Publishing any private repo, any vault, voice data or model weights.
- Changing an existing `hal-ecosystem` install: `hal migrate ecosystem` is a dry run until the user applies it.
- Publishing to PyPI. Hal, hal-kit, hal-hub and the packs install from git tags in this
  release.

## Requirements

REQ-SYS requirements are cross-cutting: every package must meet them. Each names its test.

- **REQ-SYS-01** The repo MUST be a uv workspace whose root `pyproject.toml` lists
  `packages/*` as members, and `uv sync --all-packages` followed by `uv run pytest` from the
  root MUST pass on a clean checkout. Test: integration (CI job).
- **REQ-SYS-02** Every package MUST declare `requires-python = ">=3.11"` and CI MUST run the
  tests on Python 3.11, 3.12 and 3.13. Test: unit (`tests/test_dependencies.py` reads each
  `pyproject.toml`) and CI matrix.
- **REQ-SYS-03** Each package's runtime dependencies MUST be a subset of the "Third-party
  dependencies" table; `hal-kit` MUST have none. Test: unit (`tests/test_dependencies.py`).
- **REQ-SYS-04** A module in one package MUST import another Hal package only along an edge
  marked "yes" in "Allowed imports", and `halkit.testing` MUST be imported only from test
  files. Nothing in the monorepo MAY import `gpu_light` or any pack. Test: unit
  (`tests/test_layering.py`, AST scan of every `src/` file including function-level imports).
- **REQ-SYS-05** The package import graph MUST be acyclic. Test: unit (`tests/test_layering.py`
  runs a cycle check on the observed edges, so a violation shows the cycle).
- **REQ-SYS-06** `halkit.config.load` MUST raise `ConfigError` naming file and line for
  malformed TOML, and file, table and key for an unknown key or a bad value. Test: unit
  (`packages/hal-kit/tests/test_config.py`).
- **REQ-SYS-07** Settings MUST resolve in the order flag, environment variable, file, default.
  Test: unit (`test_config.py`, one case per level).
- **REQ-SYS-08** `halkit.paths` MUST honour `HAL_CONFIG`, `HAL_POLICY`, `HAL_STATE_DIR`,
  `HAL_DATA_DIR`, `HAL_RUNTIME_DIR` and XDG variables, MUST accept `HAL_STATE` and `HAL_DATA`
  with one warning, MUST resolve the runtime directory by the rule in "`halkit.paths`" (no
  `/tmp` fallback), MUST raise `UnsafePathError` for a runtime directory owned by another
  user and `RuntimeDirMissing` when no rule applies, and MUST NOT call `os.getuid` at import
  time. Test: unit (`packages/hal-kit/tests/test_paths.py`, including a Windows case with
  `os.name` patched).
- **REQ-SYS-09** Hal components MUST NOT bind a non-loopback address; config that names one MUST
  raise `ConfigError`. Test: unit (config checks) and integration (`tests/e2e/test_ports.py`
  starts gateway, hub HTTP and console, then compares `ss -ltn` output with the allowed list;
  marked `slow`).
- **REQ-SYS-10** Every Hal TCP listener on the host MUST require a token compared in constant
  time, and every HTTP listener MUST reject a foreign `Host` or browser `Origin` with 403. Two
  exceptions, both stated in the ports table: the in-box `hal-relay` (box loopback, no token
  inside the box by design) and the gateway's `GET /health` (no body beyond liveness). Test:
  integration per listener (05, 06 and 04 own the cases; `tests/e2e/test_ports.py` checks
  that a request with `Origin: http://evil.example` or `Host: evil.example` gets 403 from
  each).
- **REQ-SYS-11** Every unix socket MUST be created in a directory with mode 0700 owned by the
  user, with socket mode 0600, and its path MUST be at most 107 bytes. Test: unit
  (`test_paths.py`), integration (02 checks session sockets).
- **REQ-SYS-12** A box MUST NOT contain any secret file, gateway token or session token: not
  in its mounts, its environment, or its tmpfs config files. Test: docker-marked integration
  (`tests/e2e/test_box_secrets.py` greps the container's environment and mounted files for the
  token values).
- **REQ-SYS-13** Hal MUST keep the compatibility contract items 1 to 9. Test: unit
  (`packages/hal-core/tests/test_compat.py` for the `hal.conf` writer and verifier, the off
  flag and unit rendering) and manual (hal-employment add-on check after an upgrade).
- **REQ-SYS-14** Machine-readable timestamps MUST be UTC ISO 8601 with `Z`, produced only by
  `halkit.timeutil`. Test: unit (`test_timeutil.py`) and a lint test that greps `src/` for
  `datetime.now()` without a timezone and `time.strftime` (`tests/test_style.py`).
- **REQ-SYS-15** Library modules MUST NOT print; only `__main__.py` and `cli.py` modules MAY.
  Every `subprocess` call MUST pass a timeout. Test: unit (ruff `T20` with per-file ignores;
  `tests/test_style.py` AST check that `subprocess.run`, `check_output` and `call` have a
  `timeout` keyword or go through `halkit.proc.run`).
- **REQ-SYS-16** In Flow 3, the assistant box SHOULD add at most 150 ms p50 to
  time-to-first-token compared with a direct gateway request, over 20 short prompts. Test:
  docker-marked and `slow` benchmark (`tests/e2e/test_voice_overhead.py` with
  `FakeModelServer`, which isolates the overhead from the model).
- **REQ-SYS-17** `scripts/install.sh` MUST be idempotent, MUST support `--dry-run`, MUST NOT
  pipe a download into a shell, and MUST install packs from a pinned tag. Test: integration
  (`tests/e2e/test_install.py` runs `--dry-run` twice in a `TempHalHome` and diffs the plan;
  marked `slow`) and manual (fresh user account).
- **REQ-SYS-18** All packages MUST carry the same version (first release `0.1.0`), and
  `hal --version` MUST print the version of every installed `hal-*` distribution. Test: unit
  (`tests/test_versions.py`).
- **REQ-SYS-19** Every requirement ID in `docs/design/*.md` MUST match
  `REQ-[A-Z]+-[0-9]{2}`, MUST be unique across the Hal specs, and MUST name a test. Test: unit
  (`tests/test_docs.py`).
- **REQ-SYS-20** Every file under `packages/`, `scripts/`, `docs/`, `eval/` and the root
  MUST be ASCII, except public README emoji headings and badges. Test: CI
  (`scripts/check-ascii.sh`) and unit (`tests/test_style.py` calls it).
- **REQ-SYS-21** CI MUST run ruff, the ASCII gate, the tests on the Python matrix and a
  gitleaks scan, and MUST deselect the markers `gpu`, `docker`, `network`, `slow`, `bwrap`,
  `systemd` and `live` by default (`live` means the test needs a real model server). Test:
  integration (the workflow itself); unit (`tests/test_markers.py` checks the markers are
  declared in the root `pyproject.toml`).
- **REQ-SYS-22** Each flow in "End-to-end flows" MUST have one end-to-end test that runs
  against `FakeModelServer`. Test: docker-marked integration (`tests/e2e/test_flow_code.py`,
  `test_flow_chat.py`, `test_flow_voice_acp.py`, `test_flow_box.py`; Telegram is covered in
  hal-tools and remote in 07-remote).
- **REQ-SYS-23** `hal` MUST accept the old command names `here`, `mode`, `serve`, `stop`, `ext`
  and print one line naming the new command. Test: unit (06 owns `test_cli_aliases.py`).
- **REQ-SYS-24** The default branch MUST only advance by a merged PR, and a release MUST NOT
  force-push or rewrite a pushed commit. Test: manual (release checklist: tests and lint green,
  `gitleaks` clean, no `hal.conf`, vault content, audio or weights in any commit).
- **REQ-SYS-25** `docs/README.md` MUST index every file in `docs/` in a
  `| Document | What it is |` table. Test: unit (`tests/test_docs.py`).
- **REQ-SYS-26** An assistant channel (`hal chat`, `hal acp --channel <name> --toolset
  assistant`) MUST run the agent in a box (docker or bubblewrap backend) with a scratch `/work`
  that is not the vault; with backend `none` it MUST run the agent in the `hal`
  process with the same broker and policy, and every surface MUST show an "unboxed" notice.
  Test: docker-marked integration (`tests/e2e/test_flow_chat.py` asserts the agent pid is in
  a container and `/work` is the channel scratch dir) and unit
  (`packages/hal-core/tests/test_assistant_unboxed.py`, backend forced to `none`, asserts the
  notice and that the broker is the one tool path).
- **REQ-SYS-27** Hal components MUST reach models only through the gateway. `halkit.llm` MAY
  use a direct OpenAI-compatible URL only when no `hal-gateway` distribution is installed
  (a pack running without Hal); with Hal installed and the gateway down it MUST raise
  `ModelUnavailable`. Test: unit (`packages/hal-kit/tests/test_llm.py`, distribution lookup
  injected).

### Requirements index

Each topic has one owner. When two specs disagree, the owner's text wins and the other spec is
fixed in the same commit. The design resolutions override any spec text that disagrees with it.
Requirement prefixes are fixed here so IDs stay unique inside the Hal repo; 08 and GL live in
other repos with their own prefixes.

| Spec | Prefix | Owns |
| --- | --- | --- |
| [00-overview.md](00-overview.md) | REQ-SYS | Product, repo and package maps, layering and pack import rules, `halkit`, flows (including where the assistant runs), config files and precedence, `[hal]` and `[compat]`, runtime-dir rule, ports table, compatibility contract, release rules |
| [01-security.md](01-security.md) | REQ-SEC | Threat model and residual risks, tool tiers and per-tool flags, `policy.toml` and profiles (`default`, `strict`, `trusted`, `standalone`, channel profiles), the approvals module `halhub.approvals` (key, ids, scopes, `Approver`, `Decision`, control socket, surfaces and `shows_all_args`, vectors file), routine grants, argument stripping, taint and `Broker.on_human_message`, guard and reviewer (`halhub.guard`, `halhub.reviewer`), the untrusted-content envelope, audit log format and `EventSink`, redaction rules, forwarder route allowlist, model file pinning and the no-pickle rule, behaviour rules of `halkit.canon`, `termsafe`, `fsafe`, `netguard` |
| [02-sandbox.md](02-sandbox.md) | REQ-BOX | Backends, detection and fallback, flags, images, mounts and `[sandbox].mask`, session runtime directory and socket lifecycle, broker process and MCP transport, broker upstreams, `result._meta.hal`, model forwarder wiring, checkpoints (including the shadow git repo), `--isolated`, dependency prep (`hal code --deps`), `hal box` templates per CLI, `hal-relay`, `hal-mcp-bridge`, no-backend fallback |
| [03-agent.md](03-agent.md) | REQ-AGT | Providers, loop, tool-call repair, coding and assistant toolsets (`--toolset`, `halagent.acp.serve`), modes `auto` and `review`, context and compaction, budgets and watchdog, session record schema and transcript layout, AGENTS.md and skills, MCP client and `[mcp.servers.<name>]`, ACP agent server, headless output event schema, `model-profiles.toml`, eval library and task formats, `[brain]` to `[agent]` mapping |
| [04-hub.md](04-hub.md) | REQ-HUB | Manifest v2 schema, entry-point discovery (`hal.packs`), registry, search, runner and timeouts, MCP server (stdio and HTTP on 8742), `halhub.client.call`, `halhub.pack.main`, `hal-hub` CLI (including `--origin`), unit declarations in manifests |
| [05-gateway.md](05-gateway.md) | REQ-GW | Gateway API surface and auth, session tokens, named client tokens, client mode (`[gateway].url`), upstreams, role aliases, presets, router policies, managed llama-server, model manifest `models.toml` and `halgateway.models`, gpu-light integration (`halgateway.gpu`), `hal gateway release-gpu`, `hal-gateway.service`, optional GPU container for inference |
| [06-core-cli.md](06-core-cli.md) | REQ-CLI | `hal` command tree (including `hal policy`, `hal approvals`, `hal approve`, `hal deny`, `hal audit`, `hal guard`, `hal secrets`), TUI, headless `hal -p` flags and exit codes, launch of code and assistant sessions (including the unboxed fallback), `agent.json` and run files, `_hal/record` handling, `hal acp` proxy, console, on/off/status/kill, unit rendering and desktop files, pack installs and environments (`packs.json`), long-job tool `core_long_task`, `hal eval` front end, configure and migrate, install scripts |
| [07-remote.md](07-remote.md) | REQ-RMT | SSH setup and key restrictions, `hal-remote up|down|env|connect`, MCP over SSH, Windows client, `remote` profile |
| `08-packs-and-flagships.md` (hal-tools) | REQ-PACK, REQ-MEM, REQ-VOICE | The pack list (`organiser`, `life`, `comms`, `online`, `desktop`, `system`, `work`, `jobs`), the per-tool table, hal-memory and hal-voice designs, the Telegram bridge |
| `gpu-light/docs/design-v2.md` (GL, gpu-light repo) | its own | gpu-light v2: scheduler, JSON-RPC socket API, claims, reclaimers, recipes for the gateway and hal-voice |

Cross-spec decisions this overview fixes, which the owners must not contradict:

- Container paths `/work`, `/run/hal/broker.sock`, `/run/hal/model.sock`, `/run/hal/agent.json`.
  Host session directory `$R/s/<sid>/`; its `run/` subdirectory is bind-mounted read-only at
  `/run/hal` (E10).
- One session id per session: 12 lowercase hex characters, used for the runtime directory,
  checkpoint refs, the ACP `sessionId` and the transcript directory (E2).
- `hal-relay` on `127.0.0.1:8741` inside the box.
- Entry-point group `hal.packs` (C2).
- Model role aliases `hal-chat`, `hal-fast`, `hal-code`, `hal-writer`, `hal-embed`,
  `hal-vision`, resolved by the gateway from the `[hal]` model keys (resolution D1).
- Broker approvals reach ACP clients as host-originated `session/request_permission` with
  `_meta` `hal/origin = "broker"`.
- Assistant channels use `hal acp --channel <name> --toolset assistant` and run in a box (A1).
  No package outside Hal imports `halcore`, `halagent`, `halsandbox` or `halgateway`. Packs
  may import `halkit` and `halhub` (only `halhub.client` and `halhub.pack`) (A3).
- The runtime-directory rule in "`halkit.paths`" (F6).
- First release `0.1.0` for every repo, git tags `v0.1.0`, dependents pin `>=0.1,<0.2` (C3).

## Design

The sections below describe the product, the repo and package maps, the shared `halkit` API,
the end-to-end flows, configuration, ports and sockets, the hal-employment compatibility
contract.

### Product

Hal is a local framework for getting the most out of local language models. It gives
you the common functions of an AI CLI (chat, a coding agent, tools, sessions, skills) on models
you run yourself, through open standards (OpenAI and Anthropic model APIs, MCP, ACP, AGENTS.md,
Agent Skills). Its defining property is the security model: the local model is treated as
untrusted, it runs with no network and no access to your home directory, and its only contact
with the outside is a small set of tool calls checked by a broker on the host.

#### Audiences

| Audience | What they want | What Hal gives them |
| --- | --- | --- |
| Researcher | Compare models and prompts on the same tasks; run long batch jobs on the same GPU; inspect every step | `hal eval` (assistant scenarios and coding tasks), JSONL session transcripts, presets with measured rationale, one gateway that any harness can target, gpu-light to share the GPU with batch jobs |
| Hobbyist | A good coding and chat agent on one consumer GPU, safe by default, easy to install | `scripts/install.sh`, Ollama or llama.cpp out of the box, `hal code` in a sandbox with no approval prompts inside the box, `hal box` to run Claude Code, Qwen Code, opencode and others in the same sandbox |
| Daily assistant user | Voice, Telegram, calendar, tasks and memory that act on their behalf without approve-everything fatigue | Tool tiers with deterministic policy (reads and Hal's own stores run freely, actions and sends ask), approvals bound to exact arguments, an audit log, `hal off` to hand the GPU back |

#### What Hal replaces

| Today (path) | Problem | Replacement |
| --- | --- | --- |
| `hal` bash launcher that ends in `exec claude` with env redirection (`hal:123-263`) | Coding depends on Claude Code; the model runs as you with full access | `hal code`: Hal's own agent (03-agent) in a sandbox (02-sandbox); Claude Code becomes optional through `hal box -- claude` |
| Proxy env "no network" block (`hal:234-255`) | Advisory only: DNS, raw sockets, ssh and any program that unsets the env still reach the network; `NO_PROXY=localhost` exposes every local service | Container with `--network none`; the model reaches only two sockets |
| `halcore/brain.py` | Ollama native API only, 8K context, no sessions, English regex guards | `halagent`: providers, tool-call repair, compaction, sessions; the `Brain` class is removed, not kept as a shim (A1) |
| hal-hub in-process tools and the `_confirmed` argument | No timeout for in-process tools; over MCP the model confirms itself; one "always allow" on `hal_call` grants all 236 tools | `halhub` runner out of process with enforced timeouts; approvals bound to the key defined in 01-security "Approval binding" (pack, tool, canonical arguments), given only by host surfaces, scopes `once` and `session` only |
| `deep_work` running cloud `claude -p` with acceptEdits on every turn | Injected mail or web text can start a cloud job with your login | Long jobs are the hub tool `core_long_task` (tier `act`), a sandboxed `hal -p` run; cloud only with `[agent].cloud_jobs = true` (off by default) |
| `hal-serve`, `hal-route.py`, `modes/*.ini` | Bash and a script on fixed ports with no auth | `halgateway`: one authenticated endpoint, presets and router policies |
| 32 extension repos cloned unpinned by `hal ext add` | Supply chain: unpinned clones, unchecked `install` argv | Eight hal-tools packs installed from a pinned tag through uv |
| `halcore/console.py` on 127.0.0.1:8740 | No auth, no Origin check: likely CSRF from any web page | Token and Origin check (06-core-cli) |

### Repo map

Hal ships as four public repos plus one separate tool, and keeps a contract with one repo it
does not touch. Each stands on its own and states its own install and test commands.

| Repo | Final path | Role | Depends on at install | Talks to at run time |
| --- | --- | --- | --- | --- |
| Hal (headline) | `~/src/Hal` (branch `next`) | The monorepo: `hal` command, agent, sandbox, broker, hub, gateway, remote | `mcp`, `rich`, `prompt_toolkit` | llama-server or Ollama, gpu-light (optional), packs through the hub |
| hal-tools | `~/src/hal-tools` (new) | The small extensions as eight packs: `organiser`, `life`, `comms`, `online`, `desktop`, `system`, `work`, `jobs` (08), history imported | `hal-kit`, `hal-hub` (`>=0.1,<0.2`, git tag) | Hal's hub (discovery), gateway through `halkit.llm` |
| hal-memory | `~/src/hal-memory` | Vault memory: index, search, capture, brief; MCP server | `hal-kit`, `hal-hub` (`>=0.1,<0.2`, git tag) | Hal's hub, gateway |
| hal-voice | `~/src/hal-voice` | Voice loop (wake word or push-to-talk, STT, TTS on :9880); film pipeline published with a disclaimer | none from Hal (own venv: torch, GPT-SoVITS) | `hal acp --channel voice --toolset assistant` for the agent, its own MCP server for `speak` and `listen`, gpu-light claims (GL recipe) |
| gpu-light | `~/src/gpu-light` | GPU scheduler (v2), not Hal-branded; commit style `<area>: <what>` | none | Nothing in Hal imports it; `halgateway.gpu` speaks its socket API with a stdlib client |
| hal-employment | not touched | Job search app on 127.0.0.1:8737 | n/a | Reads Hal's config path, off flag and `hal.conf` lines (see "Compatibility contract") |

How they combine:

- **At install.** `scripts/install.sh` (06-core-cli) creates Hal's environment at
  `data_dir()/venv` and installs the Hal packages into it from a pinned tag. Packs go into one
  shared packs environment at `data_dir()/envs/packs`; a pack that declares `env = "own"` gets
  its own environment, and the `desktop` pack gets a `system-site-packages` environment for
  the desktop bindings. The installer records each module's interpreter in
  `state_dir()/packs.json`, which the hub reads (C8, 06 owns the details). hal-voice keeps its
  own environment because its torch and CUDA dependencies weigh several GB; the uv
  documentation advises separate environments for members with heavy or conflicting
  requirements (measured during development, 2026-10). gpu-light installs with
  its own `scripts/install.sh`.
- **At discovery.** Packs register with the hub through the Python entry-point group
  `hal.packs`; each entry point names a pack manifest (manifest v2, 04-hub). A pack with heavy
  dependencies (hal-voice) registers an `exec` manifest that runs its own interpreter
  (`uv run --project <root>`), so its environment never mixes with Hal's.
- **At run time.** Packs may import `halkit` and `halhub`, and from `halhub` only
  `halhub.client` and `halhub.pack` (so a pack's standalone `mcp` uses the same validator and
  policy engine as Hal's hub). They never import `halcore`, `halagent`, `halsandbox` or
  `halgateway`. hal-voice imports nothing from Hal. Hal never imports a pack: the hub calls a
  pack only through its out-of-process runner. hal-voice and the Telegram bridge (`comms`
  pack) reach the agent only as ACP clients of `hal acp`. Hal components reach models only
  through the gateway; a pack running without Hal may use a direct OpenAI-compatible URL
  (REQ-SYS-27).

Dependency direction between repos (an arrow means "imports or installs"):

```text
hal-tools packs ----> hal-kit, hal-hub (from Hal, git tag, subdirectories packages/hal-kit, packages/hal-hub)
hal-memory      ----> hal-kit, hal-hub
hal-voice       ----> (nothing from Hal; spawns `hal acp`, serves MCP through an exec manifest)
Hal             ----> (nothing from the other repos; discovers packs by entry point)
gpu-light       ----> (nothing); Hal -> gpu-light by CLI or unix socket only
```

### Package map

The monorepo is a uv workspace with seven members under `packages/`. Distribution names use a
hyphen, import names do not.

| Distribution | Import | Responsibility | Spec |
| --- | --- | --- | --- |
| `hal-kit` | `halkit` | Stdlib only: config loading, XDG paths, atomic writes, UTC timestamps, logging setup, subprocess with mandatory timeout, secret files, redaction, canonical JSON, terminal-safe text, beneath-root file access, egress checks, dates, notifications, harness config rendering, gateway client, test fakes, version | this spec |
| `hal-hub` | `halhub` | Tool registry (manifest v2, entry points), policy engine, approvals (`halhub.approvals`), guard client and reviewer (`halhub.guard`, `halhub.reviewer`), audit, out-of-process runner with enforced timeouts, MCP server (`mcp>=2.3,<3`), pack client and pack CLI, `hal-hub` CLI | 04-hub; policy, approvals, guard, reviewer and audit rules in 01-security |
| `hal-agent` | `halagent` | Agent loop, providers, tool-call repair, coding and assistant toolsets, sessions, compaction, AGENTS.md, skills, MCP client, ACP agent server, eval library. No UI, no host side effects | 03-agent |
| `hal-sandbox` | `halsandbox` | Broker (wires policy, approvals and audit from `halhub`), container backends (docker runc or runsc, bubblewrap), images, `hal box`, dependency prep, in-container relay and MCP bridge, checkpoints | 02-sandbox, broker security in 01-security |
| `hal-gateway` | `halgateway` | Model gateway, session forwarder, presets, router policies, managed llama-server, model manifest and pulls, gpu-light claims | 05-gateway |
| `hal-remote` | `halremote` | SSH add-on for Linux and Windows clients | 07-remote |
| `hal-core` | `halcore` | The `hal` command: TUI, headless mode, `hal code`, `hal chat`, `hal box`, `hal connect`, `hal acp`, on/off/status/kill, packs, units, eval, console, configure, built-in module with `core_long_task` | 06-core-cli |

#### Allowed imports

Each row may import only the packages marked in its row. This table is the single source of
truth; `tests/test_layering.py` encodes it (REQ-SYS-04).

| Importer \ imported | halkit | halhub | halagent | halsandbox | halgateway | halremote | halcore |
| --- | --- | --- | --- | --- | --- | --- | --- |
| halkit | - | no | no | no | no | no | no |
| halhub | yes | - | no | no | no | no | no |
| halagent | yes | no | - | no | no | no | no |
| halsandbox | yes | yes | no | - | no | no | no |
| halgateway | yes | no | no | no | - | no | no |
| halremote | yes | no | no | no | no | - | no |
| halcore | yes | yes | yes | yes | yes | yes | - |

The same graph as an edge list. It is a DAG with `halkit` at the bottom and `halcore` at the
top:

```text
halcore    -> halsandbox, halagent, halgateway, halremote, halhub, halkit
halsandbox -> halhub, halkit
halhub     -> halkit
halagent   -> halkit
halgateway -> halkit
halremote  -> halkit
halkit     -> (stdlib only)
```

Reasons for the cuts:

- `halagent` does not import `halhub` or `halsandbox`. It reaches host tools only through MCP
  on `/run/hal/broker.sock` (or the pipe given in its settings when unboxed), so the same code
  runs boxed and unboxed and the box image needs only `halkit` and `halagent`. There is no
  in-process hub fallback (A2); eval runs through an in-process broker that halcore owns.
- `halsandbox` does not import `halagent`. It builds the image from the `halagent` wheel;
  halcore spawns `hal-agent --acp` from the `agent_argv` that 02 returns (A7). halsandbox
  never calls agent code.
- `halsandbox` does not import `halgateway`. halcore starts the session forwarder
  (`halgateway`) and hands its socket to the session; the gateway client is for halcore only
  (A5).
- `halhub` does not import `halsandbox`. The guard client and the reviewer live in `halhub`
  so `hal-hub mcp` applies them too (A4).
- `halgateway` does not import `halhub`: models and tools are separate trust paths. It does
  not import `gpu_light` either; `halgateway.gpu` has its own stdlib socket client (A6).
- `halremote` imports only `halkit`, so a Windows laptop can install `hal-kit` and
  `hal-remote` without the Linux-only sandbox.
- `halkit.testing` (fakes) may be imported only from test files. The layering test enforces
  this as well.

#### Third-party dependencies

| Package | Runtime dependencies | Reason |
| --- | --- | --- |
| hal-kit | none | Every pack depends on it; it must install anywhere |
| hal-hub | `mcp>=2.3,<3` | Official MCP SDK for the server (spec revision handling, framing) |
| hal-agent | `mcp>=2.3,<3` | MCP client for the broker and user servers |
| hal-sandbox | `mcp>=2.3,<3` | Broker speaks MCP on the session socket |
| hal-gateway | none | A stdlib HTTP proxy |
| hal-remote | none | Drives the system `ssh` client |
| hal-core | `rich`, `prompt_toolkit` | TUI only |

Any other runtime dependency needs a stated reason in its spec and an entry in
`tests/test_dependencies.py` (REQ-SYS-03). ACP is implemented in stdlib JSON-RPC inside
`halagent` and `halcore` rather than through the `agent-client-protocol` package, which the
brief does not allow (`agent-client-protocol` 0.12.1, 2026-08-16 [verified: PyPI JSON
2026-10-23]). Development dependencies (pytest, ruff) live in the root `pyproject.toml`
`[dependency-groups] dev`.

Version facts used here [verified: PyPI JSON API on 2026-10-23]: `mcp` 2.3.0 (2026-10-02,
requires Python >=3.10), `rich` 15.0.0, `prompt_toolkit` 3.0.53.

#### File layout

```text
Hal/
  pyproject.toml                 workspace root (virtual, not a package): members, ruff, pytest, dev group
  uv.lock
  packages/
    hal-kit/      pyproject.toml  src/halkit/      tests/
    hal-hub/      pyproject.toml  src/halhub/      tests/
    hal-agent/    pyproject.toml  src/halagent/    tests/
    hal-sandbox/  pyproject.toml  src/halsandbox/  tests/   images/
    hal-gateway/  pyproject.toml  src/halgateway/  tests/   (presets and hal-gateway.service as package data)
    hal-remote/   pyproject.toml  src/halremote/   tests/
    hal-core/     pyproject.toml  src/halcore/     tests/   (unit templates, desktop files as package data)
  tests/                         cross-package tests: layering, dependencies, versions, docs, e2e
    data/approval_vectors.json   approval key vectors (01 owns; read by the 01, 04 and 06 tests)
  eval/                          formats owned by 03-agent
    assistant/scenarios.toml     assistant scenarios
    coding/tasks/<id>/           coding tasks
  docs/
    README.md                    index table: Document | What it is
    user/                        how to use Hal
    howto/                       one task per page, ends with a symptom -> fix table
    dev/                         architecture.md, threat-model.md, protocols.md
    design/                      00-overview.md (this file) to 07-remote.md
  scripts/
    install.sh                   whole ecosystem, user level, no sudo
    host-setup.sh                one-time sudo setup (from repos/hal-host-setup.sh)
    check-ascii.sh               ASCII gate used by CI
  AGENTS.md  CLAUDE.md  README.md  LICENSE  CHANGELOG.md  SECURITY.md  CONTRIBUTING.md
  .github/workflows/ci.yml
```

Every member uses the `src/` layout so tests import the installed package, not the working
tree. Every member has its own version, kept equal across members (REQ-SYS-18).

#### Console scripts

| Script | Package | Runs where |
| --- | --- | --- |
| `hal` | hal-core | host |
| `hal-hub` | hal-hub | host (also over SSH as a forced command, 07-remote) |
| `hal-gateway` | hal-gateway | host (systemd user unit `hal-gateway.service`, shipped by 05, rendered by 06) |
| `hal-remote` | hal-remote | laptop (Linux or Windows); `hal remote` delegates to it |
| `hal-agent` | hal-agent | inside the box (`hal-agent --acp`); on the host for backend `none` (02-sandbox) |
| `hal-mcp-bridge` | hal-sandbox | inside the box (stdio MCP to `/run/hal/broker.sock`) |
| `hal-relay` | hal-sandbox | inside the box (serves `/run/hal/model.sock` on `127.0.0.1:8741`) |

### halkit API

`halkit` is the only package every repo may depend on, so its API is defined here and other
specs use it rather than redefine it. It is stdlib only and supports Python 3.11 to 3.13.
Where another spec owns the rules a module applies (01-security for `canon`, `termsafe`,
`fsafe`, `netguard` and the `redact` rule set), this section fixes the signatures and that
spec fixes the behaviour. There is no `halkit.gpu`: packs do not claim the GPU, the gateway
does (A6).

#### `halkit.paths`

```python
class UnsafePathError(Exception): ...
class RuntimeDirMissing(Exception): ...
class SocketPathTooLong(ValueError): ...
class PathNotAllowed(ValueError): ...

def config_file() -> Path: ...          # HAL_CONFIG, else $XDG_CONFIG_HOME/hal/config.toml
def config_dir() -> Path: ...           # parent of config_file()
def policy_file() -> Path: ...          # HAL_POLICY, else config_dir()/policy.toml
def state_dir() -> Path: ...            # HAL_STATE_DIR, else HAL_STATE (deprecated), else $XDG_STATE_HOME/hal
def data_dir() -> Path: ...             # HAL_DATA_DIR, else HAL_DATA (deprecated), else $XDG_DATA_HOME/hal
def runtime_dir() -> Path: ...          # rule below
def session_dir(session_id: str) -> Path: ...   # runtime_dir()/"s"/session_id
def ensure_private_dir(path: Path) -> Path: ... # mkdir 0700; raises UnsafePathError on wrong owner or mode
def check_socket_path(path: Path) -> Path: ...  # raises SocketPathTooLong above 107 bytes
def confine(path: str, roots: Sequence[Path], deny: Sequence[Path]) -> Path: ...
    # expanduser, resolve symlinks, raise PathNotAllowed outside every root or inside any deny entry
```

Runtime directory rule (F6), one rule for every package:

1. `HAL_RUNTIME_DIR` when set.
2. Linux: `$XDG_RUNTIME_DIR/hal` when `XDG_RUNTIME_DIR` is set and non-empty.
3. Linux: `/run/user/<uid>/hal` when `/run/user/<uid>` exists (a login without the variable,
   such as some SSH forced commands).
4. Windows: `%LOCALAPPDATA%\hal\run`. Hal on Windows uses TCP only (07-remote), so this holds
   state files, never sockets.
5. Otherwise `RuntimeDirMissing`, naming `HAL_RUNTIME_DIR` as the fix. There is no `/tmp`
   fallback.

Behaviour:

- XDG defaults follow the XDG Base Directory spec: `~/.config`, `~/.local/state`,
  `~/.local/share` when the variable is unset or empty. Windows uses the same home-relative
  defaults, matching the paths 07-remote prints.
- `uid` is read on first use, never at import, so `halkit.paths` imports on Windows.
- An existing runtime directory owned by another user, or with group or other permission
  bits, raises `UnsafePathError`.
- A deprecated variable (`HAL_STATE`, `HAL_DATA`) is honoured when the new one is unset, and
  logs one warning per process naming the replacement.
- `HAL_STATE_DIR` and `HAL_DATA_DIR` are the generic port of the per-install state directory
  from an earlier private fork: two installs with different values and different
  ports in their config do not share state.
- `check_socket_path`: a `sockaddr_un` path holds at most 107 bytes plus the terminator. A
  bind on a 125-byte path failed with `OSError: AF_UNIX path too long` [verified: bind
  experiment on 2026-10-23; the scratch directory it ran in has since been wiped, and
  `test_socket_path_over_107_bytes_is_refused` re-checks it]. Session ids are 12 hex
  characters, so `/run/user/1000/hal/s/0123456789ab/run/broker.sock` is 50 bytes.
- `confine` is the one path-root check packs use (C10, C11); the roots and deny list come from
  01-security's `policy.toml` `[paths]`, and a pack may narrow them, never widen them. Opening
  a file safely inside a root is `halkit.fsafe.open_beneath`.

#### `halkit.config`

```python
@dataclass(frozen=True)
class Key:
    name: str
    type: type | tuple[type, ...]          # str, int, float, bool, list, dict
    default: object
    env: str | None = None                 # env var that overrides the file value
    choices: tuple[object, ...] | None = None
    check: Callable[[object], str | None] | None = None   # returns an error message or None
    doc: str = ""                          # one line; `hal configure --show` prints it
    deprecated_alias: str | None = None    # old key name accepted with a warning

@dataclass(frozen=True)
class Table:
    name: str                              # dotted path, e.g. "gateway" or "agent.budgets"
    keys: tuple[Key, ...]
    owner: str                             # distribution name, for error messages
    open: bool = False                     # True only for tables of user-named entries

class ConfigError(Exception):
    path: Path
    table: str
    key: str | None
    line: int | None

def read_toml(path: Path) -> dict[str, object]: ...
def load(table: Table, *, path: Path | None = None,
         environ: Mapping[str, str] | None = None,
         cli: Mapping[str, object] | None = None) -> Mapping[str, object]: ...
def hal() -> Mapping[str, object]: ...      # the [hal] table (this spec), defaults applied
def known_tables() -> list[Table]: ...      # every Table registered through register()
def register(table: Table) -> Table: ...
def write_private(path: Path, text: str) -> None: ...   # atomic, mode 0600
```

Behaviour:

- A missing file means "all defaults". A file that fails to parse raises `ConfigError` with the
  TOML line number. This replaces `halcore/config.py:43-48`, which returns `{}` on a broken
  file and silently resets Hal to defaults.
- An unknown key inside a registered table raises `ConfigError` naming file, table and key.
- A value of the wrong type, outside `choices`, or rejected by `check` raises `ConfigError`.
- An unknown top-level table is not an error, because a pack that owns it may be uninstalled;
  `hal doctor` lists such tables as warnings.
- Precedence, highest first: `cli` mapping, then `environ[key.env]`, then the file, then
  `key.default`. Env values are parsed by type: `bool` accepts `1 0 true false yes no`,
  `list` and `dict` take JSON.
- `load` reads the file on every call (no module-level cache), so long-running daemons pick up
  edits; daemons that must not change mid-run copy the mapping once at start.
- `hal()` is how packs read the model roles and the vault path; there are no per-pack copies
  of those defaults.

#### `halkit.files`

```python
def write_atomic(path: Path, data: bytes | str, *, mode: int = 0o600) -> None: ...
def append_jsonl(path: Path, record: Mapping[str, object], *,
                 max_bytes: int = 16 * 2**20, keep: int = 5) -> None: ...
def locked(path: Path) -> ContextManager[None]: ...   # the lock append_jsonl takes for path
```

`write_atomic` writes a temp file in the same directory, fsyncs, renames, then fsyncs the
directory. `append_jsonl` holds an `fcntl.flock` with a 5 s timeout (a writer holds it for
one `write`, so 5 s means a stuck process), rotates at `max_bytes` and keeps `keep` old files,
and serialises with `ensure_ascii=True`. The 16 MiB default bounds disk use for logs that have
no rotation today (`turns.jsonl`); a caller with a retention rule, such as the audit log in
01-security, passes its own values. `locked` holds the same lock so a caller can compact an
append-only file (01's routine grant file) without losing a concurrent append.

#### `halkit.timeutil`

```python
def utc_now() -> datetime: ...                        # timezone-aware UTC
def iso_z(dt: datetime | None = None, *, ms: bool = False) -> str: ...   # "2026-10-23T08:15:30Z"
def parse_iso_z(text: str) -> datetime: ...           # rejects strings without Z or offset
```

#### `halkit.log`

```python
def setup(name: str, *, level: str | None = None, json: bool = False) -> logging.Logger: ...
```

Only entry points call `setup`. It logs to stderr (journald captures stderr for units) at
`HAL_LOG_LEVEL` (default `INFO`). Library modules use `logging.getLogger(__name__)` and never
print; ruff rule `T20` enforces this outside `__main__.py` and `cli.py` modules.

#### `halkit.proc`

```python
class ProcTimeout(Exception): ...

def run(argv: Sequence[str], *, timeout: float, cwd: Path | None = None,
        env: Mapping[str, str] | None = None, input: bytes | None = None,
        check: bool = True) -> subprocess.CompletedProcess[bytes]: ...
```

`timeout` has no default, so every call site states one. It never uses a shell. On timeout it
kills the whole process group (`start_new_session=True`) and raises `ProcTimeout` with the
argv and elapsed time.

#### `halkit.secrets`

```python
class UnsafeSecretError(Exception): ...

def read_secret(path: Path) -> str: ...      # strips one trailing newline
def generate_token(path: Path, *, nbytes: int = 32) -> str: ...   # creates if missing, 0600
```

`read_secret` raises `UnsafeSecretError` when the file is group- or world-readable or owned
by another user. Today `~/.config/hal/config.toml` is mode 0664 although `configure.py`
creates it 0600 [verified: `stat` on 2026-10-23]; the config file holds the ntfy topic, which is
a bearer secret, so `hal doctor` reports that mode and `hal configure --fix-modes` sets 0600.

#### `halkit.redact`

```python
def redact(value: object, *, secrets: Collection[str] = ()) -> object: ...  # recursive copy, secrets -> "[redacted]"
def scrub_text(text: str, *, secrets: Collection[str] = ()) -> str: ...      # same rules over free text
```

The rule set (key patterns, value patterns such as bearer tokens) is defined in 01-security
"Redaction rules". It walks nested dicts and lists, unlike hal-hub's top-level-only redaction
today (measured during development, 2026-10). `secrets` adds exact values (such as a pack's
own secrets) that are replaced wherever they appear; `scrub_text` applies the value patterns
and `secrets` to free text such as worker stderr and tool output (04-hub).

#### `halkit.canon`, `halkit.termsafe`, `halkit.fsafe`, `halkit.netguard`

Signatures as proposed in 01-security "Proposed halkit additions"; the encoding, escaping,
path-walk and URL rules are 01's and are not restated here.

```python
# halkit.canon: input and encoding rules in 01-security "Approval binding"
def canonical_json(value: object) -> bytes: ...             # objects, lists (routine steps), scalars

# halkit.termsafe: display rules in 01-security "Approval surfaces and protocol"
def escape(text: str, *, multiline: bool = False) -> str: ...
def strip_terminal_controls(text: str) -> str: ...          # for model text in the TUI

# halkit.fsafe: walk rules in 01-security "Proposed halkit additions"
def open_beneath(root: Path, rel: str, flags: int = os.O_RDONLY) -> int: ...  # fd; raises UnsafePathError
def is_beneath(root: Path, path: Path) -> bool: ...

# halkit.netguard: settings from 01-security policy.toml [egress]
def canonical_url(url: str) -> str: ...                     # comparison form for provenance (01 url:from-search)
class BlockedURL(ValueError):
    rule: str                                               # the deny rule, e.g. "url:scheme"

@dataclass(frozen=True)
class Response:
    url: str                                                # final URL after redirects
    status: int
    headers: Mapping[str, str]
    body: bytes
    truncated: bool                                         # body cut at max_bytes

def check_url(url: str, *, domains: Collection[str], allow_http: bool, allow_ip_literals: bool,
              allow_private: bool) -> tuple[str | None, bool]: ...       # (deny rule, host listed)
def fetch(url: str, *, domains: Collection[str], timeout: float, max_bytes: int,
          max_redirects: int = 3, method: str = "GET", headers: Mapping[str, str] | None = None,
          body: bytes | None = None, resolver: Callable[[str], list[str]] | None = None,
          allow_http: bool = False, allow_ip_literals: bool = False, allow_private: bool = False,
          context: ssl.SSLContext | None = None) -> Response: ...   # context: tests trust a local CA
```

`fetch` raises `BlockedURL` when `check_url` names a deny rule for the URL or any redirect
target. These four modules are the only implementations of their jobs (C10): `halhub`,
`halsandbox`, `halcore` and the packs import them instead of keeping their own
`canonical_json`, terminal sanitiser, `open_beneath` or URL guard.

#### `halkit.when`

```python
@dataclass(frozen=True)
class When:
    start: datetime | None
    end: datetime | None
    all_day: bool
    rrule: str | None
    rest: str                     # the text left after the date, time and repeat are taken out

def parse(text: str, now: datetime | None = None, default_time: time | None = None,
          prefer_pm: bool = True) -> When: ...
def next_occurrence(at: datetime, rrule: str) -> datetime: ...
```

A port of `halcal/when.py`, the 336-line parser copied byte for byte into calendar, clock and
tasks, plus the routines fork [source: 08-packs-and-flagships.md "Shared code moved to
halkit"]. `next_occurrence` is the one repeat-rule implementation; where the three copies
disagree, calendar's RRULE handling wins. Times are timezone-aware in the local zone for
display; anything stored or logged goes through `halkit.timeutil`.

#### `halkit.notify`

```python
def send(title: str, body: str = "", *, urgent: bool = False, speak: bool = False,
         channels: Sequence[str] | None = None, url: str = "", source: str = "") -> dict: ...
def dnd(minutes: int) -> dict: ...
def state() -> dict: ...
def history(limit: int = 20) -> list[dict]: ...
```

The notification router moved from `halnotify/tools.py` so every daemon shares one path. It
reads the `[notify]` table (keys defined by 08-packs-and-flagships): desktop through
`notify-send` via `halkit.proc.run`, push through `halkit.netguard.fetch` to
`[notify].ntfy_server` (https only), speech through `[notify].say_command`. It is host-side
pack code: a model reaches it only through the `notify_send` tool under policy. Channels are
`desktop`, `phone` and `voice`; Telegram stays with the hal-telegram pack. A failed channel is
logged and left out of `reached`, never raised. Tested in `hal-kit/tests/test_notify.py`.

#### `halkit.harness`

```python
@dataclass(frozen=True)
class McpEntry:
    name: str                     # "hal-desktop"
    command: str | None           # absolute path of the stdio command
    args: tuple[str, ...]
    url: str | None = None        # Streamable HTTP alternative
    token_file: Path | None = None

@dataclass(frozen=True)
class Endpoint:
    base_url: str                 # "http://127.0.0.1:8741"
    token_file: Path              # never the token itself
    models: Mapping[str, str]     # role alias -> model id shown to the harness
    mcp: McpEntry | None          # stdio command or HTTP url
    label: str                    # first line of the output, e.g. "Hal on desk.lan via SSH tunnel"

def render(harness: str, endpoint: Endpoint, *, shell: str, show_token: bool) -> str: ...
def env_vars(harness: str, endpoint: Endpoint) -> dict[str, str] | None: ...  # None: needs a file
def in_harness(environ: Mapping[str, str] | None = None) -> bool: ...       # True when HAL_HARNESS is set
```

The one renderer behind `hal connect` (06-core-cli) and `hal remote env|connect`
(07-remote), so the two never drift. `shell` is `sh`, `powershell` or `cmd`. Every rendering
starts with the `label` line and says the harness is unsandboxed. `in_harness` is the one reader of
`HAL_HARNESS` (01-security, 04-hub). Each per-harness renderer
states the harness version it was checked against; 07-remote "env and connect" shows example
output.

#### `halkit.llm`

```python
class GatewayError(Exception):
    status: int
    body: str

class ModelUnavailable(GatewayError): ...   # cannot connect, 502, 503 (backend down or `hal off`)

def chat(messages: list[dict[str, object]], *, model: str = "hal-chat",
         timeout: float, max_tokens: int | None = None,
         temperature: float | None = None) -> str: ...
def stream_chat(messages: list[dict[str, object]], *, model: str = "hal-chat",
                timeout: float) -> Iterator[str]: ...
def embed(texts: Sequence[str], *, model: str = "hal-embed",
          timeout: float) -> list[list[float]]: ...
```

A minimal OpenAI Chat Completions and Embeddings client for host-side packs (summaries,
translation, the briefing, vault search). Endpoint resolution, first match wins:

1. `[gateway].url` set (client mode, a laptop through `hal remote`): that URL with the client
   token that 05-gateway's client-mode settings name.
2. `runtime_dir()/gateway.sock` exists: the gateway's unix socket, authenticated by peer uid
   (05-gateway REQ-GW-02); no token is read.
3. No `hal-gateway` distribution is installed (`importlib.metadata` finds none): the
   standalone fallback, `[hal].ollama_url` plus `/v1` (Ollama's OpenAI-compatible routes).
   This exists only for a pack used without Hal (G5).
4. Otherwise `ModelUnavailable`, naming `hal-gateway.service`. A Hal install with the gateway
   stopped never falls back to a direct URL (REQ-SYS-27).

It replaces the per-extension `llm.py` copies that call Ollama directly and the `gpu_busy()`
guard duplicated in 8 extensions (measured during development, 2026-10); GPU contention is
the gateway's job. Model names are gateway role aliases (05-gateway), not backend model ids.

#### `halkit.version` and `halkit.testing`

```python
__version__: str
def versions() -> dict[str, str]: ...    # every installed hal-* distribution and its version
```

`halkit.testing` holds stdlib fakes shared by all packages: `FakeModelServer` (scripted
Chat Completions, Responses, Anthropic Messages and Embeddings, over TCP or a unix socket),
`FakeBroker` (scripted MCP tool results over a unix socket), `TempHalHome` (a context manager
that points `HAL_CONFIG`, `HAL_POLICY`, `HAL_STATE_DIR`, `HAL_DATA_DIR`, `HAL_RUNTIME_DIR` at a
temp directory), `FakeClock` and `FakeResolver` (for `netguard`).

### End-to-end flows

Each flow names its components and the protocol on every hop. The inside of each box belongs
to its spec. Paths in containers are fixed; host paths use `$R` for `runtime_dir()` (on this
machine `/run/user/1000/hal`), `$STATE` for `state_dir()` (default `~/.local/state/hal`) and
`<sid>` for a 12-hex session id.

#### Flow 1: `hal code` (sandboxed coding agent)

```text
user terminal
  |  keys, render
  v
hal (halcore TUI, host) ------ ACP JSON-RPC over docker stdio -------> hal-agent --acp (box)
  |  owns: launch, session record, checkpoints, approvals UI               |   |
  |                                                                        |   | coding tools run here:
  +-- broker thread (halsandbox) <== MCP, stdio framing, unix socket ======+   | read write edit grep
  |     listens $R/s/<sid>/run/broker.sock (= /run/hal/broker.sock, ro dir)    | glob run todo finish
  |     policy, approvals, audit (halhub) -> runner (halhub) -> pack process   | on /work only
  |     audit -> $STATE/audit/<date>.jsonl (01 schema)                         |
  +-- model forwarder thread (halgateway) <== HTTP over unix socket ==========+
        listens $R/s/<sid>/run/model.sock (= /run/hal/model.sock)
        adds Authorization: Bearer <session token>
          |
          v  HTTP over $R/gateway.sock
        hal-gateway (host daemon) --> llama-server 127.0.0.1:8080 | Ollama 127.0.0.1:11434
```

Sequence:

1. `hal code [DIR]` resolves the workspace (default: cwd), loads config and policy (fails
   loudly on errors), and picks a backend (02-sandbox "Backends and detection"): docker with
   runsc, docker with runc, bubblewrap, or none. runsc reaches the bind-mounted session sockets only when registered with
   `--host-uds=open`, so a failed runsc probe falls back to runc with a status note naming
   that fix [verified: host check, 2026-10-23]. When the cached probe last saw docker
   and docker is now down, `hal code` refuses to start rather than drop isolation; backend
   `none` asks once per project on a terminal.
2. It creates `$R/s/<sid>/` (0700), starts the broker and the model forwarder on the sockets
   in `run/`, asks the gateway for a session token scoped to this session (05-gateway), and
   renders `run/agent.json` (03-agent schema, 06-core-cli renders it) and the run files.
3. It takes checkpoint 0 of the workspace under `refs/hal/checkpoints/<sid>/0`; a non-git
   directory uses 02's shadow git repository (02-sandbox "Checkpoints").
4. It starts the box: `--network none`, read-only rootfs, tmpfs, `--cap-drop ALL`,
   `no-new-privileges`, limits, host uid. Mounts: `/work` (rw), `/work/.git` (ro), each file
   matching `[sandbox].mask` (default `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`,
   `id_ed25519*`) bound over by an empty read-only file with a notice and a per-project
   `--unmask` (02-sandbox), and `$R/s/<sid>/run/` at `/run/hal` read-only. Nothing else from
   the host.
5. halcore spawns `hal-agent --acp` in the box and speaks ACP over the container's stdio:
   `initialize`, `session/new`, then `session/prompt` per user message. The agent finds the
   broker and the model socket from `/run/hal/agent.json`; the ACP `mcpServers` list carries
   only user MCP servers, which run inside the box (03-agent). For every prompt it sends,
   halcore calls `Broker.on_human_message(session, text, surface)` (01-security, architect
   F5). Updates stream back as `session/update`.
6. Inside the box the agent runs coding tools freely. Model calls go to `/run/hal/model.sock`.
   Host tools (web fetch, memory, calendar) go to `/run/hal/broker.sock` as MCP `tools/call`.
7. The broker applies policy (01-security "Decision algorithm"). `allow` runs the tool through
   the hub runner; `ask` sends a host-originated approval request, bound to the key in
   01-security "Approval binding", to the TUI and to the control socket; `deny` returns an
   error result. Prompt scopes are `once` and `session` only (B2). Every decision is appended
   to the audit log; an audit write failure refuses the call.
8. Before each new turn the host takes the next checkpoint; `/undo` and `/diff` use them.
   Ignored files and files over 50 MiB are not covered, and `/undo` says so (R-new-5). With
   `--isolated` the agent works on a `git worktree` and the diff applies only on accept.
9. On exit the host stops the box, removes `$R/s/<sid>/`, revokes the session token and closes
   the transcript at `$STATE/sessions/<sid>/` (`meta.json`, `transcript.jsonl`; 03 owns the
   record schema, 06 writes it from `_hal/record` notifications).

Dependency prep (architect F3, 02-sandbox owns): when a lockfile changed, `hal code` offers
`hal code --deps`. It runs `uv sync --frozen`, `npm ci` or `cargo fetch` in a separate prep box
whose only egress is the host egress proxy with a registry allowlist; it writes only inside
the workspace (`/work/.hal/venv`, `node_modules`, `CARGO_HOME` under `/work/.hal/`), with
Python from the box image. It costs one `act` approval per lockfile sha256. The coding box
itself never gets a network.

Variant: `hal acp [DIR]` runs steps 1 to 4 and 9 and proxies ACP between its own stdio and the
box, so Zed or any ACP editor is the client. Broker approvals reach the editor as
`session/request_permission` requests that the host proxy itself originates, tagged
`_meta: {"hal/origin": "broker"}`, with the options 01-security "Approval surfaces and
protocol" lists; the proxy drops that tag from anything the agent sends, so an
agent-originated prompt can never pass as a broker prompt (Decision D7).

#### Flow 2: `hal chat` (assistant in the terminal)

Same process model as Flow 1 with these differences (A1; 06-core-cli owns the launch,
03-agent the toolset):

- The agent runs with `--toolset assistant`: no coding tools; host tools through the broker,
  offered by lexical search (`hal_find_tools`) plus pinned tools (04-hub).
- `/work` is a per-channel scratch directory `$STATE/assistant/<channel>/work`, never the
  vault. The vault is reachable only through hal-memory tools on the broker, which run on the
  host under policy. A rogue model therefore cannot read or rewrite notes in bulk.
- The box is long-lived per channel: it stays up for as long as the `hal` process that
  started it (hal-voice and the Telegram bridge keep theirs all day), so the per-turn cost is a
  socket hop, not a container start. How repeated `session/new` calls map onto one box is
  06-core-cli's.
- There is no workspace checkpoint: the scratch `/work` holds nothing of the user's.
- User MCP servers not marked `host` run in the box; with backend `none` they are refused
  (architect F4).
- Unboxed fallback: with backend `none` (no docker, no bubblewrap), the agent runs in the `hal`
  process (`halagent.acp.serve` over pipes, 06-core-cli) with the same broker, policy and
  audit, and every surface shows an "unboxed" notice (REQ-SYS-26). The residual risk is
  01-security's R-new-9: model output is then parsed in a host process.

`hal chat "message"` and `hal -p "task" --chat [--json | --stream-json]` are the headless
forms; flag names and exit codes are 06-core-cli's, the output event schema is 03-agent's. In
headless mode an `ask` decision with no approver is a deny (fail closed), logged with the
reason `no-approver`, unless `--approvals queue[:SECONDS]` lets `hal approve` answer it.

#### Flow 3: voice to agent

```text
mic -> hal-voice (own venv, host)  wake word or push-to-talk, STT (faster-whisper, CPU)
         | ACP over stdio of a child process
         v
       hal acp --channel voice --toolset assistant   (Flow 2 process model)
         | session/update agent_message_chunk (streamed text)
         v
       hal-voice sentence splitter -> TTS POST 127.0.0.1:9880/tts -> speaker
```

- hal-voice spawns `["hal", "acp", "--channel", "voice", "--toolset", "assistant"]` once at
  start (E1) and sends one `session/prompt` per utterance. It sends no vault `cwd`. It no
  longer imports `halcore.brain` through `sys.path` (`halvoice/brain.py` `find_core`), so it
  has no import-time dependency on Hal.
- Approvals for `ask` tools arrive as `session/request_permission` from the host proxy. The
  voice surface cannot show every argument (`shows_all_args = false`, 01-security), so it
  speaks the host-made summary and listens for yes or no within 20 s; anything else is a
  deny. A call whose parameters include content (`content = true`) is never routed to voice:
  it goes to the TUI, Telegram or `hal approve`. The binding to exact arguments is done by
  the broker, so a spoken yes approves only that call. Audio from another source can say yes;
  01-security lists that as R-new-7.
- Long jobs: the agent calls the hub tool `core_long_task` in halcore's built-in module (tier
  `act`, so it asks), which runs `hal -p - --json --isolated --dir <workspace> --approvals
  queue:<[long_task].queue_timeout_s> --notify` in a sandbox with the task on stdin and
  notifies on completion (06-core-cli, "core_long_task"). The cloud path exists
  only when the user sets `[agent].cloud_jobs = true`, which is off by default (F2).
- TTS: GPT-SoVITS runs in a GPU container (`--runtime=nvidia`) by default; host mode is an
  opt-in with sha256-pinned weights and `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD` unset (G2,
  08-packs-and-flagships). hal-voice claims the GPU as GL's hal-voice recipe says: a `service`
  claim for the resident set and an `interactive` claim on wake.
- Overhead target: the assistant box adds at most 150 ms p50 to time-to-first-token compared
  with a direct gateway call (REQ-SYS-16). This is a target, not a measurement [unverified:
  measure with the e2e benchmark in "Test plan"].

#### Flow 4: Telegram

```text
phone -> Telegram Bot API (HTTPS, long poll) <- Telegram bridge (comms pack service, host)
                                                   | pairing: one chat, rate-limited code
                                                   | voice note -> transcription (hal-voice CLI)
                                                   v ACP over stdio of a child process
                                     hal acp --channel telegram --toolset assistant
                                                   | replies as text, or TTS when enabled
```

- The bridge is `hal-telegram.service` from the `comms` pack in hal-tools (08). It holds the
  bot token (host side only) and is an ACP client of `hal acp --channel telegram --toolset
  assistant` (B9). It no longer constructs `halcore.brain.Brain` in-process and no longer
  borrows hal-voice's venv (`hal-telegram.service` today sets `HAL_TELEGRAM_PYTHON` to
  hal-voice's `.venv`).
- Approvals arrive as `session/request_permission` and are shown as inline buttons; the
  callback data carries the approval id and a nonce, checked against the paired chat
  (01-security "Approval surfaces and protocol"), never a model-chosen flag.
- Every message the bridge sends disables link previews, so a URL in assistant text is not
  fetched by Telegram (R-new-2).
- The policy profile for this channel is `telegram` (01-security): `system` tier denied,
  `send` and `act` ask, and pairing attempts are rate-limited.

#### Flow 5: external harness through `hal box` or `hal connect`

`hal box -- <cli> [args]` runs a third-party agent CLI in the same sandbox:

```text
hal (host) -- starts box with image hal-box-<cli> --> <cli> (box)
                                                        | model API: http://127.0.0.1:8741 (box loopback)
                                                        v
                                                      hal-relay (box) -> /run/hal/model.sock -> host forwarder -> gateway
                                                        | MCP stdio
                                                      hal-mcp-bridge (box) -> /run/hal/broker.sock -> broker (host)
```

- The relay listens on `127.0.0.1:8741` inside the container. Loopback exists under
  `--network none` [verified: a socket bound to 127.0.0.1 in a `--network none` container on
  2026-10-23]. Using the gateway's own port number makes the box config and the
  `hal connect` config look the same.
- `hal box` writes the CLI's config into the container's tmpfs home: base URL, a dummy API key
  (the host forwarder adds the real session token, so no token exists inside the box), and an
  MCP server entry that runs `hal-mcp-bridge` (02-sandbox owns the per-CLI templates for
  claude, qwen, opencode, codex, crush, goose).
- Approvals for a `hal box` session come only from host surfaces (`hal approve` in another
  terminal, a desktop notification for `once`). MCP elicitation is never used, because a
  client inside the box could answer its own request (B4).
- `hal connect <harness>` turns on the gateway's TCP listener and prints, through
  `halkit.harness`, the config for running the harness on the host with the gateway at
  `http://127.0.0.1:8741` and a token, and the hub MCP server as `hal-hub mcp` (stdio) or
  `http://127.0.0.1:8742/mcp` with `hub-token`. The output starts with a line that says the
  harness is not sandboxed. Approvals come through MCP elicitation when the client supports
  it, else out of band through `hal approve`; they are advisory, because an unsandboxed
  harness can reach the user's shell anyway (R-new-4).
- `hal claude [args]` replaces `hal here`: Claude Code on the host against the gateway root
  URL `http://127.0.0.1:8741`, with a warning that it is unsandboxed and that the network
  block is advisory. It keeps the telemetry-off env block from `hal:234-255`, now described as
  privacy, not security.

The harness CLIs `qwen`, `opencode`, `codex`, `crush`, `goose` and `cline` are supported only when installed
[verified: `command -v` on 2026-10-23].

#### Flow 6: laptop through `hal remote`

```text
laptop                                                     desktop
hal-remote up  -- ssh -N -L 127.0.0.1:8741:127.0.0.1:8741 --> sshd (key-only, restrict, permitopen)
  harness or laptop hal  -> http://127.0.0.1:8741 (+ token)      -> hal-gateway 127.0.0.1:8741
harness MCP entry: ssh -T hal-desktop  -- forced command ------> hal-hub mcp --transport stdio
                                                                   --channel remote --origin remote:<client>
```

- The desktop binds nothing beyond loopback; a named client token (05-gateway) is still
  required through the tunnel. `hal remote` setup turns the gateway's TCP listener on.
- Remote tool calls use the hub's MCP server over SSH stdio under policy profile `remote`;
  audit records carry origin `remote:<client>` and `remote_addr` (B8). An `ask` decision goes
  to MCP elicitation when the laptop client supports it, else out of band to `hal approve` on
  the desktop; with no answer before the timeout it is a deny (07-remote and 01-security own
  the details).
- The tunnel key carries `restrict`, `permitopen` and `permitlisten="127.0.0.1:1"`, so `-R`
  cannot open a usable listener. `hal remote server-check` recommends
  `AllowTcpForwarding local` and `AllowStreamLocalForwarding no` with a warning, not as a
  requirement, because those change sshd for the user's other keys. `hal remote` does not use
  the hal-vm.
- A Linux laptop with docker or bubblewrap may run `hal code` locally with `[gateway].url`
  pointing at the tunnel; the box, broker and forwarder run on the laptop, and the forwarder
  targets that URL with the client token (05-gateway). Windows laptops get `hal-remote` only
  (tunnel, `env`, `connect`).
- A desktop whose `openssh-server` is active and key-only
  (`/etc/ssh/sshd_config.d/10-hal-hardening.conf`) [verified: host check,
  2026-10-23], so `hal remote` setup only adds the restricted key.

### Configuration

#### Files

| File | Owner spec | Purpose | Mode |
| --- | --- | --- | --- |
| `~/.config/hal/config.toml` | this spec (`[hal]`, `[compat]`); each table's owner | Main config, one table per component or pack. Path kept: hal-employment checks it exists | 0600 |
| `~/.config/hal/policy.toml` | 01-security | Policy profiles, per-tool and per-pack overrides, `[paths]`, `[egress]` | 0600 |
| `~/.config/hal/models.toml` | 05-gateway | Gateway model manifest: sha256 mandatory for every file the gateway loads; Ollama models recorded by digest (resolution D2) | 0600 |
| `~/.config/hal/model-profiles.toml` | 03-agent | Agent per-model settings (resolution D2) | 0644 |
| `~/.config/hal/presets/*.ini` | 05-gateway | User preset overrides (shipped presets are package data) | 0644 |
| `~/.config/hal/AGENTS.md` | 03-agent | User-wide agent instructions | 0644 |
| `~/.config/hal/skills/<name>/SKILL.md` | 03-agent | User skills | 0644 |
| `~/.config/hal/gateway-token` | 05-gateway | Gateway owner token, created at the first gateway start (05 REQ-GW-07) | 0600 |
| `~/.config/hal/hub-token` | 04-hub | Bearer token for `hal-hub mcp --transport http` | 0600 |
| `~/.config/hal/console-token` | 06-core-cli | Console token | 0600 |
| `~/.config/hal/{caldav-password,telegram-token,telegram-chat,obsidian_api_key}` | hal-tools, hal-memory | Existing secret files, paths unchanged | 0600 |
| `<project>/AGENTS.md`, `CLAUDE.md`, `.agents/skills/` | 03-agent | Project instructions and skills | n/a |

There is no per-project Hal config file in v0.1. Project files are writable by the model, so a
project config could raise budgets or change policy from inside the box (Decision D4).

#### Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `HAL_CONFIG` | `$XDG_CONFIG_HOME/hal/config.toml` | Config file; its directory is `config_dir()` |
| `HAL_POLICY` | `config_dir()/policy.toml` | Policy file |
| `HAL_STATE_DIR` | `$XDG_STATE_HOME/hal` | State (sessions, audit, eval reports, off flag, `packs.json`) |
| `HAL_DATA_DIR` | `$XDG_DATA_HOME/hal` | Data (Hal venv, packs envs, llama.cpp, pack stores, images) |
| `HAL_RUNTIME_DIR` | rule in "`halkit.paths`" | Sockets |
| `HAL_LOG_LEVEL` | `INFO` | Log level for entry points |
| `HAL_STATE`, `HAL_DATA` | unset | Deprecated aliases, honoured with a warning |
| `HAL_HUB`, `HAL_CONF`, `HAL_ROOT` | unset | Removed: no `sys.path` lookup and no shell config remain; a set value logs a warning that it is ignored |

Per-key env overrides (such as `HAL_GATEWAY_PORT`) are declared in each spec's `Key.env`.
Variables with a security role (`HAL_HARNESS`, `HAL_REMOTE_ADDR`) are defined by the specs
that read them (01, 04, 06, 07).

#### Precedence

For every setting: command-line flag, then the key's environment variable, then
`config.toml`, then the built-in default. For policy: the profile named by `--policy-profile`
or `HAL_POLICY_PROFILE`, else `policy.toml` `profile`, else `default`; rules inside a profile
follow 01-security. Channel profiles (`telegram`, `remote`, `voice`) apply on top of the
selected profile and can only tighten it. A pack's own `mcp` command run without Hal uses the
`standalone` profile (01-security).

#### The `[hal]` table

| Key | Type | Default | Reason |
| --- | --- | --- | --- |
| `vault` | str | `"~/Documents/vaults/Hal"` | Existing default and live value; hal-memory and the briefing read it |
| `chat_model` | str | `"gemma4:12b-it-qat"` | Chosen by `hal eval` on 2026-10-10: 33/38 and 74/76, the best of 6 models (measured during development, 2026-10); single runs, assistant scenarios only |
| `fast_model` | str | `"qwen3.5:4b"` | Existing default for classifying and titling |
| `code_model` | str | `""` (means `chat_model`) | Role for `hal code`; empty keeps one model loaded on a 12 GB card. The coding eval (03-agent) picks the shipped default; until then this alias |
| `writer_model` | str | `""` (means `chat_model`) | Prose role; also feeds `HAL_WRITER_MODEL` in the compat file |
| `embed_model` | str | `"embeddinggemma"` | Existing default for vault search |
| `embed_on_cpu` | bool | `true` | Keeps the vault index off the GPU, as today |
| `vision_model` | str | `"qwen3-vl:8b"` | Existing default for the screen pack |
| `ollama_url` | str | `"http://127.0.0.1:11434"` | Read by the gateway as the default Ollama upstream, and by `halkit.llm` only in its standalone fallback (no Hal gateway installed). A non-loopback host is rejected (REQ-SYS-09) |
| `user_name` | str | `""` | How Hal addresses the user; replaces names hard-coded in hal-memory (`capture.py:47,63`) |
| `profile` | str | `""` | Informational: the hardware profile last chosen by `hal configure` (06-core-cli), migrated from `HAL_PROFILE`; nothing reads it to change behaviour |

These keys are the single source of truth for model roles. The gateway resolves the aliases
`hal-chat`, `hal-fast`, `hal-code`, `hal-writer`, `hal-embed`, `hal-vision` from them
(05-gateway); `hal/main` and `hal/agent` stay for router modes. The defaults that
`halcore/config.py:16-22`, the scaffold template and `install.sh` each repeat today are
deleted; packs read `[hal]` through `halkit.config.hal()`.

#### Other tables and owners

| Table | Owner |
| --- | --- |
| `[agent]` (including `cloud_jobs`, default `false`), `[agent.budgets]` | 03-agent |
| `[mcp]`, `[mcp.servers.<name>]` (`host = true` entries become broker upstreams, pack `mcp-<name>`, 02-sandbox) | 03-agent |
| `[sandbox]` (including `mask`, `require`) | 02-sandbox |
| `[hub]` (`pinned`, `disabled`, `module_paths` as a development fallback, `http_port`) | 04-hub |
| `[gateway]` (including `tcp`, default off, and `url` for client mode), `[gateway.upstreams.<name>]` | 05-gateway |
| `[console]`, `[ui]`, `[units]`, `[claude]`, `[long_task]` | 06-core-cli |
| `[remote]` | 07-remote |
| `[compat]` | this spec |
| `[brain]` | deprecated; mapped to `[agent]` by `hal configure --migrate` (03-agent defines the mapping) |
| pack tables (`[calendar]`, `[notify]`, `[jobs]`, `[telegram]`, `[memory]`, ...) | the pack (08) |

#### The `[compat]` table

| Key | Type | Default | Reason |
| --- | --- | --- | --- |
| `hal_conf` | list of str | `[]`, filled by `hal configure --migrate` | Paths of the `hal.conf` files that hal-employment's `addon.hal_root` may read |
| `hal_model` | str | `""` (means `chat_model`) | `HAL_MODEL` line, role "checker" in hal-employment |
| `writer_model` | str | `""` (means `[hal].writer_model`) | `HAL_WRITER_MODEL` line, role "writer" |
| `fast_model` | str | `""` (means `fast_model`) | `HAL_FAST_MODEL` line, role "quick" |

hal-employment calls Ollama with these names, so they must be Ollama model names. A hal-ecosystem
`hal.conf` typically sets `HAL_MODEL`, `HAL_WRITER_MODEL` and `HAL_FAST_MODEL` to Ollama model
names. `hal configure --migrate` copies those three values into `[compat]` so nothing changes
for hal-employment.

### Ports and sockets

| Endpoint | Bound by | Auth | Who connects | Notes |
| --- | --- | --- | --- | --- |
| `127.0.0.1:8740` | `hal console` (halcore) | Token (cookie or header) and Origin/Host check; `/open?t=` one-time link; non-JSON POST gets 415 | Browser on the same machine | Port kept from today; fixes the CSRF exposure in `console.py` `do_POST` |
| `127.0.0.1:8741` | hal-gateway | Owner token or a named client token; foreign Host or Origin gets 403 | Host harnesses, `hal remote` tunnel | TCP listener off by default; `hal connect`, `hal claude` and `hal remote` turn it on (resolution D4). `GET /health` needs no token (REQ-SYS-10 exception) |
| `$R/gateway.sock` | hal-gateway | Peer uid (`SO_PEERCRED`); forwarders present session tokens; socket 0600 in a 0700 dir | Host forwarders, host packs through `halkit.llm` | `/run/user/1000/hal/gateway.sock` for uid 1000 |
| `127.0.0.1:8742` | `hal-hub mcp --transport http` | Bearer `hub-token`, Origin/Host check | Host harnesses that cannot spawn a stdio server | Runs only when started (resolution D14) |
| `127.0.0.1:8080` | managed llama-server (halgateway) | `--api-key-file` held by the gateway | Gateway only | Explicit `--host 127.0.0.1 --port 8080`; installed build b11512 defaults to 8080 and accepts a `.sock` path in `--host` [verified: `llama-server --help`] |
| `127.0.0.1:11434` | Ollama (system service, not Hal) | none (Ollama has no auth) | Gateway, hal-employment | Ollama 0.40.1 listening on 127.0.0.1 [verified: `/api/version` and `ss -ltn` on 2026-10-23] |
| `127.0.0.1:9880` | hal-voice TTS (GPT-SoVITS `api_v2`, GPU container by default) | none | hal-voice, `hal-say`, briefing | Unchanged port; owned by hal-voice |
| `127.0.0.1:8737` | hal-employment | its own key, `X-Hal: 1` check | jobs pack | Untouched |
| `127.0.0.1:6742` | OpenRGB server (hal-tools rgb unit) | none | rgb tools | Bind address [unverified: check `openrgb --help` for a host option when Hal is on] |
| `127.0.0.1:8079` | `hal-route.py` | none | Claude Code | Removed: router policies move into the gateway |
| `$R/s/<sid>/run/broker.sock` -> `/run/hal/broker.sock` | broker (host) | The socket is the capability: one per session, 0600, directory mounted read-only | Agent or `hal-mcp-bridge` in that session's box | MCP with stdio framing, 4 MiB message cap (E9) |
| `$R/s/<sid>/run/model.sock` -> `/run/hal/model.sock` | model forwarder (halgateway, started by halcore) | Socket capability; forwarder adds the session token | Agent or `hal-relay` in the box | HTTP/1.1; route allowlist in 01-security |
| `$R/control/<sid>.sock` | the process that owns the broker | 0600, uid check with `SO_PEERCRED` | `hal approvals`, `hal approve`, `hal deny` | One per broker (B3); outside the session dir, so the box cannot reach it |
| `127.0.0.1:8741` inside a box | `hal-relay` | none (box loopback is private to the box; REQ-SYS-10 exception) | Third-party CLI in `hal box` | Exists only in the container's network namespace |
| ACP stdio | `hal-agent --acp` | Process boundary | `hal` TUI, `hal acp` proxy | No socket |
| `$XDG_RUNTIME_DIR/gpu-light/daemon.sock` | gpu-light daemon | gpu-light's own (peer uid) | halgateway, hal-voice | JSON-RPC 2.0 defined by GL, not Hal |

02-sandbox lists every other file in the session directory (for example `lifeline.sock`).

Rules:

- No Hal component binds a non-loopback address. A config value that names one is a
  `ConfigError` (REQ-SYS-09). Remote access is only through SSH (07-remote).
- Every Hal TCP listener on the host requires a token compared with `hmac.compare_digest`;
  every HTTP listener answers a `Host` that is not the loopback name and port, or a browser
  `Origin` that is not its own, with 403 (REQ-SYS-10). This blocks DNS rebinding and CSRF.
  The exceptions are the in-box relay and the gateway's `GET /health`.
- Unix sockets live in `ensure_private_dir` directories, and are created with mode 0600.
- A hardened runc container reached a host unix socket through a read-only bind mount, with
  `--network none`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--pids-limit`, `--memory`, `--cpus` and the host uid; outbound HTTP failed and a write to the
  rootfs failed with EROFS. A read-only bind of `/work/.git` over a read-write `/work` rejected
  a write to `.git/hooks` [verified: experiment with `python:3.13-slim-trixie` on 2026-10-23;
  its scratch directory has since been wiped, and `tests/e2e/test_flow_code.py` re-checks
  both]. runsc refuses the same socket unless registered with `--host-uds=open` [verified:
  host check, 2026-10-23].

### Compatibility contract

hal-employment must keep working with no change (measured during development, 2026-10). Hal keeps:

1. `~/.config/hal/config.toml` at that path. `HAL_CONFIG` may move it for tests only; the
   installer never changes it.
2. The off flag `~/.local/state/hal/off`. `hal off` creates it and `hal on` removes it. When
   `HAL_STATE_DIR` is set to anything else, Hal also writes the flag at the default path and
   `hal doctor` warns that hal-employment reads only the default. `hal off` pauses local
   coding only for sessions without a gateway lease.
3. `hal.conf` with the three model lines. `hal configure` writes a generated file only to a
   path in `[compat].hal_conf` that it created itself (the file starts with its header saying
   it is generated and that nothing in Hal sources it). A listed file it did not create, such
   as the `hal.conf` of a hal-ecosystem install, is never rewritten: Hal only verifies its
   three model lines and reports a mismatch (H3).
4. `X-HalGPU=yes` or `X-HalOff=yes` in a unit and the `ConditionPathExists=!<state>/off`
   injection; `hal off` stops those units and unloads Ollama models.
5. The unit name `hal-employment.service` (rendered from the `jobs` pack in hal-tools),
   port 8737, `GET /healthz`. Unit templates keep the `@ROOT@` placeholder; the jobs launcher
   reads `[jobs].jobhunt_root` at run time, and a golden test covers the rendered unit (H2).
6. `[jobs].jobhunt_root` semantics: it has no default. While it is unset the jobs launcher exits
   with a message naming `[jobs].jobhunt_root`, and `hal configure --migrate` never writes it (H1).
7. `hal kill` stops only units recorded in `units.json` plus `hal.target`, never a
   `hal-*` glob that would reach hal-employment's own units (H4).
8. A speak executable that takes the text as `argv[1]` (`hal-say` from hal-voice), and the
   vault layout `Areas/Job Search/Pipeline.md` with `- [ ]` tasks, read by hal-memory's brief
   (08-packs-and-flagships owns the layout test).
9. hal-employment self-gates on `nvidia-smi` and ignores processes whose cmdline contains
   `ollama`, `GPT-SoVITS`, `api_v2.py`, `hal-voice`, `halvoice` or `faster_whisper`. A managed
   llama-server matches none of these, so hal-employment waits while it holds the GPU, as it
   does with `hal-serve` today. No change is planned.

## Failure modes

Summary per layer. "Closed" means the action does not happen; "open" means work continues.
01-security is authoritative for policy, approvals, guard, audit and broker; 02-sandbox for
backends.

| Layer | Failure | Behaviour | Stance |
| --- | --- | --- | --- |
| Config | Malformed TOML, unknown key, bad type | Entry point exits 2 with file, line, table and key | Closed |
| Paths | No runtime directory by the F6 rule | Entry point exits naming `HAL_RUNTIME_DIR` | Closed |
| Policy | `policy.toml` malformed | Broker does not start; the session starts with no host tools and the UI says why | Closed |
| Inference | llama-server or Ollama down | Gateway returns 503 with a JSON error; the agent keeps the session and reports it; no fallback to another provider unless configured | Closed |
| Gateway | Token missing or wrong | 401; nothing proxied | Closed |
| Gateway | Foreign `Host` or `Origin` | 403 | Closed |
| Gateway | Owner token file with a bad mode | The TCP listener does not start | Closed |
| `halkit.llm` | Gateway down on a Hal install | `ModelUnavailable`; no direct-URL fallback (REQ-SYS-27) | Closed |
| Sandbox | runsc probe fails (not registered with `--host-uds=open`) | Uses runc and shows the fix in the status line | Degraded (documented) |
| Sandbox | Docker down but the cached probe last saw docker | Refuses to start, naming the cause | Closed |
| Sandbox | No backend (no docker access, no bwrap) | Asks once per project on a terminal; `run` (bash) is disabled; file tools are path-confined in process; user MCP servers not marked `host` are refused; the UI shows a banner; `[sandbox].require = true` refuses to start instead | Degraded for files, closed for exec |
| Assistant | Backend `none` | Agent runs in the `hal` process with the same broker and policy; "unboxed" notice on every surface (R-new-9) | Degraded (documented) |
| Sandbox | Box exits unexpectedly | Session marked crashed; transcript and checkpoints kept; `hal code --resume <sid>` restarts | Closed |
| Broker | Crash or socket gone | Tool calls fail with an error result; the agent continues without host tools | Closed |
| Broker | Audit log write fails | The tool call is refused (no knob) | Closed |
| Broker | Restart | Session taint starts at `untrusted` (architect F5) | Closed |
| Approvals | No surface answers before the timeout (headless without `--approvals queue`, remote without elicitation and no `hal approve`) | `ask` becomes deny, logged with the reason | Closed |
| Approvals | Content call and only a surface without `shows_all_args` | Not offered there; goes to the TUI, Telegram or `hal approve`, else deny | Closed |
| Guard | Prompt Guard or reviewer error, timeout or uncertainty | Goes to the human; never auto-approves | Closed |
| Hub runner | Pack exceeds timeout or crashes | Process group killed; error result `{"status":"error","error_type":"timeout",...}` | Closed |
| Checkpoint | Checkpoint before a turn fails | The turn does not start; the UI offers `--no-checkpoint` | Closed |
| Checkpoint | Ignored or over-50-MiB file changed | Not restorable; `/undo` says so (R-new-5) | Open (documented) |
| gpu-light | Not installed, daemon down, wait times out | Hal proceeds with a warning; scheduling is not a security boundary (same as `halcore/gpulight.py` today) | Open |
| Remote | Tunnel down | Client gets connection refused; no direct fallback | Closed |
| Compat | Writing or verifying `hal.conf` fails | `hal configure` exits non-zero naming the path; Hal itself keeps running | Open for Hal |

## Migration

What replaces what, by path in the older hal-ecosystem tree:

| Today | Becomes | Spec |
| --- | --- | --- |
| `hal` (bash launcher, 263 lines) | `hal` console script in hal-core; `hal here` -> `hal claude`; `hal mode X here` -> `hal claude --preset X`; vault commands delegate to hal-memory's CLI | 06 |
| `hal-serve` | halgateway managed llama-server; `hal serve [preset]` and `hal stop` keep working as aliases | 05 |
| `hal-route.py` | halgateway router policies (`route`, `effort`, `swap`, context-overflow mapping) | 05 |
| `modes/*.ini`, `modes/*-prompt.txt`, `modes/qwen3.8-chat.jinja` | `packages/hal-gateway/src/halgateway/presets/` | 05 |
| `hal-kill` | `hal kill`, stopping units recorded in `units.json` plus `hal.target` and Hal processes by pid file and cgroup, not by `pkill -f 'python[0-9.]* -m hal[a-z]+'` | 06 |
| `halcore/brain.py` | halagent loop and providers; the claims check, leaked-call parser and "did NOT happen" note become repair and guard layers; no `Brain` shim remains | 03 |
| `halcore/chat.py` | halcore TUI and headless mode (`hal chat`, `hal -p`) | 06 |
| `halcore/config.py` | `halkit.config`, `halkit.paths`; tables registered per package | this spec |
| `halcore/configure.py` | `hal configure` (template, `--migrate`, `--fix-modes`, compat writer and verifier) | 06, this spec for compat |
| `halcore/console.py`, `console.html` | halcore console with token and Origin check | 06 |
| `halcore/evaluate.py`, `eval/scenarios.toml` | `hal eval` (06) over the `halagent` eval library (03); scenarios move to `eval/assistant/scenarios.toml` | 03, 06 |
| `halcore/ext.py`, `extensions.toml` | `hal packs list|add|remove|enable|disable|doctor` installing hal-tools packs from a pinned tag into the packs environment; `hal ext` stays as an alias for one minor version | 06, 04 |
| `halcore/hub.py` | removed: hal-core depends on hal-hub directly | this spec |
| `halcore/power.py`, `halcore/units.py` | halcore power and units; discovery comes from the hub, not a second regex scan (`units.module_roots`); `hal on` holds no claim and does not wait for the GPU | 06 |
| `halcore/gpulight.py` | `halgateway.gpu.lightv1` and `halgateway.gpu.lightv2` (claims for model serving), used by halcore power | 05 |
| `deep_work` in `halcore/brain.py` | hub tool `core_long_task` in halcore's built-in module | 06 |
| `setup.sh` | `scripts/install.sh` (user level, uv, no `curl | sh`), `scripts/host-setup.sh` (sudo), `hal configure`, `hal models pull` with pinned revision and sha256 (`halgateway.models`) | 06, 05 |
| `models/catalog.txt`, `pull-all.sh`, `build-roles.sh` | model manifest `models.toml` and `hal models` | 05 |
| `system/ollama-store.sh` | `scripts/host-setup.sh --ollama-store` | 06 |
| `systemd/*.service`, `hal.target`, `desktop/*.desktop` | package data in hal-core rendered by 06; `hal-gateway.service` shipped by 05 and rendered by 06 | 06, 05 |
| `tests/test_halcore.py`, `tests/test_gpu_light.py` | one test file per module under each package's `tests/` | each spec |
| `core/hal-hub` (separate repo, 7 commits, no remote) | `packages/hal-hub`, history imported, `.pyc` blobs dropped | 04 |
| `README.md`, `SETUP.md`, `MODES.md` | new README; `docs/user/`, `docs/howto/`, `docs/dev/`; MODES content moves to `docs/user/presets.md` | this spec, 05 |
| `media/graph2.gif` | stays (already public, harmless content) | none |

How existing users keep working:

- `~/.config/hal/config.toml` keeps its path and its `[hal]` keys. `[brain]` is still read with a
  deprecation warning until `hal configure --migrate` maps it to `[agent]`. `[hub].module_paths`
  still works as a development fallback for manifests on disk.
- `hal.conf` is no longer sourced by anything; `hal configure --migrate` reads its model lines
  once (for `[compat]`) and its `HAL_LLAMA_SERVER`, `HAL_MODELS_DIR`, `HAL_LLAMA_URL` and
  `HAL_MODE` values (for `[gateway]`, 05-gateway). It never rewrites that file (H3).
- `deep_work = "claude"` in the live config maps to local sandboxed long jobs.
  `[agent].cloud_jobs` stays `false` until the user sets it (Decision D6).
- `[jobs].jobhunt_root` has no default and `--migrate` does not write it; set it to the employment
  helper checkout (H1).
- Rendered units under `~/.config/systemd/user` keep their names; `hal units install`
  re-renders them with the new paths. Desktop launchers keep their names.
- An existing `hal-ecosystem` install keeps running unchanged until the user upgrades with
  `hal migrate ecosystem --from PATH`: install from the new repos, `hal configure --migrate`,
  `hal units install`, repoint `~/.local/bin/hal`, and stop the old units.

What is removed: sourcing `hal.conf` as shell; `HAL_HUB` and `HAL_ROOT` lookups; the proxy env
trick as a security claim; `_confirmed`, `confirmed` and `confirm` as meaningful arguments (the
broker strips and logs them); "always allow" approvals (durable allows are `policy.toml` rules
written by the user, B2); `hub-calls.jsonl` (one audit log, B8); `hal-route.py` on :8079;
`extensions.toml` per-repo URLs; the `.mcp.json` `hal` server pointing at
`<old checkout>/hal-mcp.py` (the vault config is the user's; `hal doctor` reports the
stale entry and the user removes it after the upgrade, because the old clone is load-bearing
until then).

## Test plan

Hermetic unit tests (default run, no markers):

- `packages/hal-kit/tests/`: `test_paths.py` (including `confine` and the runtime-dir rule),
  `test_config.py`, `test_files.py`, `test_timeutil.py`, `test_log.py`, `test_proc.py`,
  `test_secrets.py`, `test_redact.py`, `test_canon.py` (reads
  `tests/data/approval_vectors.json`), `test_termsafe.py`, `test_fsafe.py`,
  `test_netguard.py` (with `FakeResolver`), `test_when.py` (characterisation cases from the
  calendar, clock, tasks and routines copies), `test_notify.py`, `test_harness.py` (goldens per
  harness and shell), `test_llm.py` (against `FakeModelServer` on a unix socket),
  `test_testing.py` (the fakes themselves). Example names:
  `test_unknown_key_in_registered_table_is_an_error`,
  `test_malformed_toml_reports_line_number`,
  `test_runtime_dir_owned_by_another_user_is_refused`,
  `test_runtime_dir_without_xdg_uses_run_user_when_it_exists`,
  `test_runtime_dir_with_no_rule_is_an_error_not_tmp`,
  `test_paths_imports_without_getuid_on_windows`,
  `test_socket_path_over_107_bytes_is_refused`,
  `test_proc_timeout_kills_the_whole_process_group`,
  `test_llm_uses_direct_url_only_without_hal_gateway`,
  `test_llm_raises_model_unavailable_when_gateway_socket_is_missing`.
- Root `tests/`: `test_layering.py` (including "no module imports `gpu_light`"),
  `test_dependencies.py`, `test_versions.py`, `test_docs.py`, `test_style.py`,
  `test_markers.py`.
- `packages/hal-core/tests/test_compat.py`: the `hal.conf` writer refuses a listed file
  without its header and only verifies its model lines (H3), `--migrate` reading of an old
  `hal.conf` fixture (synthetic values), the off flag written at the default path when
  `HAL_STATE_DIR` differs, that `--migrate` never writes `jobhunt_root`
  (H1), the rendered `hal-employment.service` golden (H2).
- `packages/hal-core/tests/test_assistant_unboxed.py` (REQ-SYS-26, backend forced to `none`).

Marked tests (deselected by default; `pytest -m docker` and so on):

- `docker`: `tests/e2e/test_flow_code.py` (a scripted model edits a file in `/work`, calls a
  fake host tool, the broker logs it, `/undo` restores; `.git/hooks` write refused; a masked
  `.env` reads empty), `test_flow_chat.py` (assistant box, scratch `/work`),
  `test_flow_voice_acp.py` (an ACP client in the test plays hal-voice's role and answers a
  permission request; a `content = true` call is not offered to it), `test_flow_box.py` (a
  stub CLI in the box calls `127.0.0.1:8741` and the MCP bridge), `test_box_secrets.py`,
  `test_voice_overhead.py` (also `slow`). The flow tests run once per runtime the 02 probe
  reports usable: runc always, and runsc when it is registered with `--host-uds=open`.
- `slow`: `tests/e2e/test_ports.py`, `tests/e2e/test_install.py`.
- `bwrap`, `systemd`, `live`, `gpu`, `network`: none in this spec; 02, 06, 03 and 05 own
  them. hal-tools adds a `host` marker.

Fakes needed (all in `halkit.testing`, stdlib only): `FakeModelServer` with a script of
responses (text, tool calls, leaked calls, errors, slow first token, embeddings),
`FakeBroker`, `TempHalHome`, `FakeClock`, `FakeResolver`. The e2e image is 02's test build
`hal-sandbox:test-<ctx>` (02-sandbox Decision D12); ripgrep is not on this host's PATH
[verified: `timeout 5 rg --version` failed on 2026-10-23], so the image must ship it.

## Implementation plan

Phases across specs, in order. Each phase leaves every test green.

| Phase | Work | Spec |
| --- | --- | --- |
| 0 | Workspace, core `halkit`, gates, docs index, move halcore and hal-hub in | this spec |
| 1 | Manifest v2, registry, runner, MCP server on `mcp>=2.3`, `halhub.client`, `halhub.pack` | 04 |
| 2 | `halkit.canon`, `termsafe`, `fsafe`, `netguard`; tiers, policy engine, `halhub.approvals`, guard, reviewer, audit, redaction rules; then `halkit.when`, `notify`, `paths.confine` | 01, this spec |
| 3 | Gateway, presets, router policies, managed llama-server, `halkit.llm` | 05, this spec |
| 4 | Agent loop, providers, repair, toolsets, sessions, ACP server, eval library | 03 |
| 5 | Broker, backends, image, forwarder wiring, checkpoints, dependency prep, `hal box` | 02 |
| 6 | `hal` command tree, TUI, assistant launch, `hal acp`, `halkit.harness`, console, units, installs | 06, this spec |
| 7 | `hal remote` | 07 |
| 8 | Flow e2e tests, README, docs, CHANGELOG, version, release | this spec |

Commits owned by this spec, in order, on branch `next` (each one sitting, each green):

1. `Add uv workspace root with ruff and pytest config` - root `pyproject.toml` (virtual root,
   `members = ["packages/*"]`, ruff rules incl. `T20`, pytest markers `gpu docker network
   slow bwrap systemd live` with `-m "not gpu and not docker and not network and not slow and
   not bwrap and not systemd and not live"`), `.gitignore` additions (`.pytest_cache/`,
   `.venv/`, `*.bak`).
2. `Add hal-kit package with paths and time helpers` - `halkit.paths` (XDG and the F6
   runtime-dir rule), `halkit.timeutil`, tests; version `0.1.0`.
3. `Add HAL_STATE_DIR override` - `HAL_STATE_DIR`, `HAL_DATA_DIR`,
   `HAL_RUNTIME_DIR` and the deprecated aliases, tests.
4. `Add strict TOML config loader to hal-kit` - `halkit.config` including `hal()`, tests.
5. `Add atomic writes and JSONL append to hal-kit` - `halkit.files`, tests.
6. `Add logging setup and subprocess helper to hal-kit` - `halkit.log`, `halkit.proc`, tests.
7. `Add secret file reader to hal-kit` - `halkit.secrets`, tests.
8. `Add shared test fakes to hal-kit` - `FakeModelServer`, `FakeBroker`, `TempHalHome`,
   `FakeClock`, tests.
9. `Move halcore into packages/hal-core` - pure move plus the launcher `PYTHONPATH` and test
   path changes needed to stay green; `hal-core` `pyproject.toml`.
10. `Split halcore tests into one file per module` - mechanical.
11. `Add hal-hub history under packages/hal-hub` - merge of `core/hal-hub` rewritten with
    `git filter-repo --to-subdirectory-filter packages/hal-hub` and `.pyc` paths dropped,
    using `--allow-unrelated-histories`.
12. `Add pyproject for hal-hub workspace member` - src layout move, console script.
13. `Remove sys.path lookup of hal-hub from halcore` - delete `halcore/hub.py`, depend on
    `hal-hub`.
14. `Add agent, sandbox, gateway and remote skeletons` - four packages with version,
    module header and one smoke test each.
15. `Add package layering test` - `tests/test_layering.py`.
16. `Add dependency allowlist and version tests` - `tests/test_dependencies.py`,
    `tests/test_versions.py`.
17. `Add ASCII gate script` - `scripts/check-ascii.sh`, `tests/test_style.py` part 1.
18. `Add style tests for timestamps and timeouts` - `tests/test_style.py` part 2.
19. `Add CI workflow for lint, ASCII gate and tests` - `.github/workflows/ci.yml`.
20. `Add gitleaks scan to CI`.
21. `Add docs index and design spec index` - `docs/README.md`, this file linked.
22. `Add requirement ID check for design specs` - `tests/test_docs.py`.
23. `Add AGENTS.md and CLAUDE.md pointer`.
24. `Add SECURITY.md and CONTRIBUTING.md`.
25. `Add date parser and repeat rules to hal-kit` - `halkit.when`, ported from
    `halcal/when.py` with characterisation tests from every copy (phase 2).
26. `Add path confinement helper to hal-kit` - `halkit.paths.confine`, tests (phase 2, after
    01's `policy.toml` `[paths]` loader).
27. `Add notification router to hal-kit` - `halkit.notify`, tests (phase 2, after 01's
    `halkit.netguard` commit).
28. `Add gateway chat and embedding client to hal-kit` - `halkit.llm` with the standalone
    rule of REQ-SYS-27 (phase 3, once the gateway socket protocol is fixed).
29. `Add harness config renderer to hal-kit` - `halkit.harness`, goldens per harness and
    shell (phase 6, with `hal connect`).
30. `Add hal.conf compat writer for hal-employment` - `[compat]` table, header-checked writer
    and line verifier, tests (phase 6, after `hal configure` exists in its new form).
31. `Add end-to-end flow tests` - `tests/e2e/` (phase 8).
32. `Rewrite README for monorepo release` (phase 8).
33. `Add CHANGELOG entry for 0.1.0` (phase 8).
34. `Update package versions to 0.1.0` (phase 8).

`halkit.canon`, `termsafe`, `fsafe` and `netguard` land through 01-security's implementation
plan, with the signatures in this spec.

Release actions after commit 34 (not commits; each needs the user's go-ahead): push `next`,
open the PR, merge, tag `v0.1.0`.

## Decisions

- **D1 halkit API lives in this spec.** Every repo depends on it, so one owner avoids drift
  between specs. Reason: today a near-identical `config.py` is copied into each extension
  (8 of the 9 surveyed), each repeating the shared model defaults, so changing a default
  touches about 30 repos (measured during development, 2026-10).
- **D2 Packs may import `halkit` and `halhub` (client and pack CLI only); channels use
  `hal acp`; hal-voice imports nothing from Hal.** Reason: a pack's standalone `mcp` then uses
  the same validator and policy engine as Hal's hub instead of a second copy (A3). hal-voice
  and hal-telegram today put core hal on `sys.path` and build the brain in-process, which
  couples their environments and bypasses any sandbox; ACP is an open standard, so the same
  client code works against any ACP agent.
- **D3 Assistant sessions get a scratch `/work`, not the vault.** Reason: the vault holds
  health, work and personal notes; reaching it only through hal-memory tools on the broker
  keeps every access tiered and audited.
- **D4 No per-project Hal config in v0.1.** Reason: files in the workspace are writable by the
  model; AGENTS.md covers project instructions without any power over policy or budgets.
- **D5 Unknown top-level config tables warn, unknown keys inside a known table fail.**
  Reason: pack tables outlive uninstalled packs; typos inside a table are the common error
  and must fail loudly.
- **D6 Cloud long jobs exist but are off by default, and migrated `deep_work = "claude"`
  becomes local.** Reason: `"claude"` was the built-in default in `halcore/config.py` `BRAIN`,
  so its presence in the file does not prove the user chose cloud jobs, and the brief requires
  cloud to be an explicit opt-in.
- **D7 Broker approvals travel as host-originated ACP permission requests.** Reason: it gives
  every ACP client (TUI, Zed, hal-voice, the Telegram bridge) one approval path, while the
  broker stays the only component that binds approvals to arguments.
- **D8 Relay port 8741 inside the box.** Reason: identical config for `hal box` and
  `hal connect`; the box's loopback is private, so there is no clash with the host gateway.
- **D9 Gateway TCP listener off by default.** `hal connect`, `hal claude` and `hal remote`
  turn it on. Reason: nothing in Hal needs TCP (everything uses the unix socket), so a
  loopback listener that any local process could probe exists only when a host harness asks
  for it (resolution D4).
- **D10 ACP in stdlib, not the `agent-client-protocol` package.** Reason: the brief's
  dependency allowlist; ACP v1 is a small JSON-RPC surface.
- **D11 Merge commit for the release PR, tag `v0.1.0`.** Reason: `0.1.0` is honest for a new
  harness and leaves `1.0` for after real use (C3); the 27 commits since `03b8652` and the new
  ones are each reviewed and green and keep the lineage readable.
- **D12 Hal in its own environment, packs in one shared packs environment, own environments
  only where needed.** Packs that declare `env = "own"` and hal-voice get their own; the
  `desktop` pack gets `system-site-packages`. Reason: uv's guidance on heavy or conflicting
  members; pack isolation for security is done by the hub's out-of-process runner, not by
  environments; the installer's `packs.json` tells the hub each module's interpreter (C8).
- **D13 One 12-hex session id per session.** It names the runtime directory, checkpoint refs,
  the ACP `sessionId` and the transcript directory. Reason: keeps socket paths far below the
  107-byte AF_UNIX limit, makes collisions negligible for one user, and gives every tool one
  id to look up (E2).
- **D14 Assistant channels run in a long-lived box per channel, with an unboxed fallback.**
  Reason: the box also contains a code-execution bug in the agent's parsing of model output
  and keeps user MCP servers off the host; a box that lives as long as its channel process
  costs a socket hop per turn, not a container start. With backend `none` the
  agent runs in the `hal` process with the same broker and a visible notice, so voice still
  works and the weaker isolation is never silent (A1, R-new-9).
- **D15 One runtime-directory rule with no `/tmp` fallback.** Reason: a shared `/tmp` allows a
  pre-created directory attack, and 05 and 07 each needed the same answer for a missing
  `XDG_RUNTIME_DIR` and for Windows (F6).
- **D16 `halkit.llm` falls back to a direct URL only when the gateway is not installed.**
  Reason: Hal components reach models only through the gateway (G5); a Hal install with the
  gateway stopped must fail loudly rather than bypass it, while a pack used on its own still
  works against Ollama.
- **D17 Shared helpers live in `halkit`, with no `halkit.gpu`.** `when`, `notify`, `harness`,
  `canon`, `termsafe`, `fsafe`, `netguard`, `paths.confine` and `llm.embed` were needed by more
  than one repo (C6, C10). Reason: one implementation each; GPU claims belong to the gateway
  and hal-voice, not to packs (A6).
