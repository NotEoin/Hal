# Hal tool hub (halhub v2)

`halhub` is the registry and runner behind every host tool Hal offers. It finds packs, checks
their manifests, puts each call through 01-security's policy pipeline, runs the tool in a
separate process with an enforced timeout, and serves the tools over MCP to any harness. This
spec replaces the hand-rolled `core/hal-hub` (7 commits, stdlib-only, tools in-process, MCP
loop written by hand) with manifest v2, entry-point discovery, warm worker processes and an
MCP server on the official Python SDK.

Status: draft 2026-10-23, revised 2026-10-24 to match
the design resolutions of 2026-10-23 (binding).

## Scope and non-goals

This spec owns (see the index in [00-overview.md](00-overview.md)):

- the manifest v2 schema (`hal-module.toml`, `schema = 2`) and loading v1 manifests
  (resolution C1 makes this spec the single schema owner);
- discovery through the entry-point group `hal.packs`, the installed-pack record and path
  scanning, and the registry;
- argument normalisation before policy: declared aliases, type coercion, bounds and list
  checks, and the CLI-only near-miss rule;
- the call pipeline that every front end uses (Python API, CLI, MCP, broker, nested call): it
  runs 01-security's decision, approval, guard, reviewer and audit steps in 01's order, with
  per-session state held in a `HubSession`;
- two approval surfaces that implement 01's surface protocol: `elicitation` (MCP) and `tty`
  (the `hal-hub` CLI);
- the runner: worker processes, the worker protocol (including the secrets envelope),
  timeouts, cancellation, nested calls;
- tool search;
- the MCP server (stdio, streamable HTTP, and a stream API the broker reuses);
- the external tool provider interface (host MCP servers that 02-sandbox's broker runs);
- the pack-facing API: `halhub.client.call` and `halhub.pack.main` (resolution C9);
- the `hal-hub` CLI;
- the library side of the pack lifecycle: declared install steps and declared units.

It uses, and does not redefine:

- 01-security: what each tier and per-tool flag means for policy; `policy.toml`, its profiles
  (`default`, `strict`, `trusted`, `standalone`) and channel profiles (`voice`, `telegram`,
  `remote`); the policy types and `prepare`, `decide`, `explain` in `halhub.policy`; the
  approval key and its test vectors; grants, `ApprovalRequest`, the `Approver` protocol, the
  router, the surface protocol with `shows_all_args`, and the control socket, all in
  `halhub.approvals`; the audit schema and `AuditLog` in `halhub.audit`; the untrusted-content
  envelope; `halhub.guard` and `halhub.reviewer`; taint rules; routine grants; the pack
  secrets declaration; redaction rules. 01 owns those modules even though they live in the
  `hal-hub` distribution (resolution A4, B1 to B11);
- `halkit` (00-overview, "halkit API"): `paths`, `config`, `files`, `timeutil`, `log`,
  `proc`, `secrets`, `redact`, `canon`, `termsafe`, `notify`, `harness`, `testing`;
- 02-sandbox: the broker process, session sockets and session ids, running host MCP servers
  as upstreams, and `result._meta.hal` (resolution E7);
- 03-agent: the `[mcp.servers.<name>]` table whose `host = true` entries become broker
  upstreams (resolution E6);
- 06-core-cli: pack installation and its record `state_dir()/packs.json` (resolution C8),
  unit rendering and enabling, and the `hal approve`, `hal deny`, `hal approvals` commands
  (resolution B12);
- 07-remote: the SSH forced command that starts `hal-hub mcp` for a laptop;
- 08-packs-and-flagships: the pack list and the per-tool tiers. The hal-tools, hal-memory and
  hal-voice repos convert their manifests to v2.

Non-goals for this release:

- Filesystem confinement of individual host tools. The runner drops the network for tools
  that declare `network = false` when bubblewrap works (REQ-HUB-70, 01 REQ-SEC-58); beyond
  that, host tools run as the user. 01-security lists this as a residual risk.
- MCP resources, prompts, sampling, roots, the logging capability and the Tasks extension.
- Changing the tool list inside a running MCP session (`listChanged` stays false).
- Any HTTP bind other than loopback, and OAuth. Remote use goes through SSH (07-remote).
- Installing packages at run time. The hub never runs `pip` or `uv` on its own.
- Windows or macOS as the host that runs the hub.

## Requirements

Each requirement names the test that proves it. Unit tests live in
`packages/hal-hub/tests/test_<module>.py`. "Integration" tests are hermetic and marked `slow`
unless stated. Ids are stable: requirements added in the 2026-10-24 revision use numbers from
63 up, placed in the section they belong to.

### Manifest

- **REQ-HUB-01** The hub MUST parse a `hal-module.toml` with `schema = 2` against "Manifest
  v2" and MUST reject an unknown key, a wrong type, a missing required key or a value outside
  its allowed set with `ManifestError` naming the file, the table path and the key. Test: unit
  (`test_manifest.py`, one case per rule).
- **REQ-HUB-02** A manifest without `schema` MUST load as v1 with every tool at tier `act` and
  `reviewable = false`, whether or not it sets `confirm` (resolution B6, 01 REQ-SEC-01),
  unless `policy.toml` sets the verdict by name, and MUST log one warning per module naming
  the file. Test: unit (`test_policy_meta.py::test_v1_tool_without_tier_registers_as_act`, the
  test 01 names).
- **REQ-HUB-03** Tool names MUST match `^<module>_[a-z0-9]+(_[a-z0-9]+)*$` and be at most 64
  characters; the prefix `hal_` is reserved for the built-in `hub` module, and a double
  underscore is reserved for provider tools (`<name>__<tool>`, REQ-HUB-72). Test: unit.
- **REQ-HUB-04** A v2 parameter type MUST be one of `string`, `integer`, `number`,
  `boolean`. A v1 `array` MUST load as `string` with `list = true` and a v1 `object` as
  `string` with `format = "json"`, each with a warning. Test: unit.
- **REQ-HUB-05** A v2 manifest MUST NOT declare a parameter whose name starts with `_` or is
  `confirm` or `confirmed`. Test: unit.
- **REQ-HUB-06** A module whose `requires_hal` excludes the running hub version MUST be
  rejected with an error naming both versions. Test: unit.
- **REQ-HUB-63** `[module] pack` MUST match `^[a-z][a-z0-9-]{0,31}$`, MUST NOT start with
  `mcp-` (reserved for host MCP servers, resolution E6), and defaults to the module name;
  `[[module.secrets]]` entries MUST have unique `name`s and the keys 01-security defines;
  `domains` entries MUST be lowercase IDNA host names or `*.` wildcards. For a module listed
  in `packs.json`, the declared `pack` MUST equal the pack name that record files it under
  (the key `p` in `packs.<p>.modules`), else the module is excluded as a registry problem. A
  module not listed there (path-scanned, or an entry point `packs.json` does not record) MUST
  run with pack equal to its module name whatever it declares, and a different declared value
  MUST be reported in `Registry.problems`. The reason: `pack` feeds the approval key and the
  `[packs.<p>]` policy rules (resolution C7), and a third-party pack must not claim another
  pack's rules and grants by declaring its name. Test: unit (`test_manifest.py`;
  `test_registry.py::test_pack_name_not_matching_packs_json_is_excluded`,
  `test_registry.py::test_path_scanned_module_uses_module_name_as_pack`).
- **REQ-HUB-64** A tool with a `routine` table MUST have tier `act`, MUST NOT set
  `reviewable = true`, and its `routine.steps` MUST name a declared `format = "json"`
  parameter. Test: unit.

### Discovery and registry

- **REQ-HUB-07** Discovery MUST read entry points in the group `hal.packs` without importing
  any pack code. Test: unit (`test_registry.py::test_discovery_imports_no_pack_module`, the
  test 01 REQ-SEC-57 names: a fixture package whose `__init__.py` raises; discovery succeeds).
- **REQ-HUB-08** Path scanning MUST search each `HAL_MODULE_PATH` entry and each
  `[hub].module_paths` entry recursively to depth 4, skipping hidden directories and
  `__pycache__`, `.venv`, `venv`, `node_modules`, `build`, `dist` and `site-packages`. Test:
  unit (`test_discovery.py`).
- **REQ-HUB-09** Two manifests that declare the same module name MUST make `Registry.load`
  raise `DuplicateModuleError` naming both manifest paths and their sources. `hal-hub doctor`
  MUST report the error and exit 1, not crash. Test: unit.
- **REQ-HUB-10** A manifest that fails to parse MUST exclude only its own module, record the
  error in `Registry.problems`, and log it to stderr. Nothing MUST be written to stdout. Test:
  unit (captures stdout).
- **REQ-HUB-11** A module whose `requires` is not met (missing, disabled, wrong version,
  unavailable itself, or in a cycle) MUST be `unavailable`: its tools are not listed or
  searchable, and a call returns `error_type = "unavailable"` with the reason. Test: unit.
- **REQ-HUB-12** Modules named in `[hub].disabled` MUST be excluded. A v1 manifest named
  `hub` found by path scanning (the old `core/hal-hub` checkout) MUST be skipped with a
  warning. Test: unit.
- **REQ-HUB-65** Every manifest listed in the installed-pack record (`state_dir()/packs.json`,
  06-core-cli, `packs.<p>.modules.<m>.manifest`) MUST be discovered and run with the
  interpreter argv that record names for that module (`packs.<p>.modules.<m>.interpreter`);
  a listed manifest that is missing MUST be a registry problem, not a crash. Test: unit
  (`test_discovery.py::test_installed_pack_record_sets_interpreter`).

### Arguments

- **REQ-HUB-13** Invalid arguments MUST end the call before any tool code runs, with
  `error_type = "invalid_arguments"` naming the parameter and what it accepts, whether the
  hub's own checks (bounds, list items, JSON format) or 01's `decide` (`bad-args:<code>`: an
  unknown name, wrong type, value outside `enum`, over `max_length`, missing, NUL) found the
  problem. Test: unit (`test_args.py`, `test_hub.py`).
- **REQ-HUB-14** Coercion MUST follow the table in "Coercion" exactly. In particular a boolean
  MUST NOT be accepted for `integer` or `number`, and a string MUST become a boolean only from
  the listed words. Test: unit, one case per row.
- **REQ-HUB-15** Declared `aliases` MUST be applied for every front end. Near-miss argument
  names MUST be mapped only for the `hal-hub` CLI front end, only when exactly one parameter
  matches and that parameter is not also present, and each mapping MUST be logged and returned
  in the result's `warnings` (resolution B7, 01 REQ-SEC-05). Test: unit
  (`test_args.py::test_near_miss_name_is_mapped_on_the_cli_only`: `when` maps to `when_text`
  on the CLI and is `invalid_arguments` over MCP and the broker).
- **REQ-HUB-16** Arguments whose names start with `_` MUST reach 01's `decide` untouched, so
  that `decide` strips and records them and audits `self_approval_attempt` (01 REQ-SEC-06);
  the hub MUST NOT read them. Test: unit (`_confirmed: true` on an `ask` tool still asks, and
  the audit has a `self_approval_attempt` record).
- **REQ-HUB-17** The hub MUST copy each parameter's `format` and `access` into 01's `ToolMeta`
  unchanged and MUST NOT check or resolve URLs, hosts or paths itself; 01's `prepare` and
  `decide` do (REQ-SEC-09, REQ-SEC-10). Test: integration (`test_hub.py::test_http_url_is_denied_by_default`:
  under `default`, `http://example.org` and `file:///etc/passwd` to a `format = "url"`
  parameter are denied with rule `url:scheme`).

### Policy and approvals

- **REQ-HUB-18** Every call from every front end (Python API, CLI, MCP, broker, nested call)
  MUST be decided by 01's `decide` before any tool code runs. Test: unit (a spy runner asserts
  no run on `deny` or on `ask` without an approval).
- **REQ-HUB-19** An exception in `prepare`, `decide` or the session's context builder MUST
  result in a deny with `error_type = "policy_error"` and rule `internal-error`. Test: unit.
- **REQ-HUB-20** An `ask` verdict MUST run the tool only after a matching grant (01 grant
  store) or an approver answer `allow` whose scope is one of the request's scopes (`once` or
  `session`, resolution B2), and MUST run it with exactly `Decision.args`, the arguments whose
  key was computed (01 REQ-SEC-18). Test: unit (reuse of a `once` grant, changed argument,
  expired grant, scope not offered: each refused).
- **REQ-HUB-21** The hub MUST NOT write approval files. A pending request lives only in the
  process that made it and is reachable from other processes only through 01's control socket
  `runtime_dir()/control/<sid>.sock` (resolution B3). Test: unit
  (`test_session.py::test_pending_request_writes_no_files`).
- **REQ-HUB-22** `hal-hub approve ID`, `hal-hub deny ID` and `hal-hub approvals` MUST call the
  same `halhub.approvals` client functions as 06's `hal approve`, `hal deny` and
  `hal approvals`, with the same checks (a terminal on stdin and stdout, `HAL_HARNESS` unset),
  and MUST have no flag that skips the question. Test: unit (refused without a TTY, refused
  with `HAL_HARNESS=1`) and manual.
- **REQ-HUB-23** A tool whose listing verdict is `deny` for the session's profile and channel
  MUST be absent from MCP `tools/list`, from `hal_find_tools`, and from `function_specs`, and
  a call to it MUST return `error_type = "policy_denied"` (01 REQ-SEC-17). Test: unit.
- **REQ-HUB-66** The `elicitation` surface MUST declare `shows_all_args = true` and MUST
  report itself unavailable for a request whose rendered display is longer than
  `PREVIEW_CHARS` (2000), so 01's router never routes a call with `content = true` parameters
  to a dialog that cannot show them. Test: unit (`test_surfaces.py`).
- **REQ-HUB-67** When a human surface approves a call to a tool with a `routine` table, the
  hub MUST compute `steps_sha256` from the canonical step list and add 01's `routine` grants
  for the steps whose verdict is `ask`, before it runs the tool; a step that does not validate
  against its target tool MUST make the call `invalid_arguments` before any prompt. Test:
  integration (`test_routine_grants.py`, the test 01 REQ-SEC-31 names, with a fake routine
  runner).
- **REQ-HUB-68** Each `HubSession` MUST keep 01's taint level: a result of an
  `untrusted_output` tool raises it, a guard flag raises it to `flagged`, and
  `HubSession.human_message` resets it per 01. Sessions that never receive human messages
  (`hal-hub mcp`, the CLI, `halhub.client`) MUST start `untrusted`. Test: unit
  (`test_session.py`).

### Runner

- **REQ-HUB-24** Tools MUST run outside the hub process, unless the module sets
  `inprocess = true` and is either the built-in `hub` module or listed in
  `[hub.runner].inprocess_modules`. Test: unit.
- **REQ-HUB-25** On timeout the hub MUST kill the worker's whole process group (SIGTERM, then
  SIGKILL after `kill_grace`) and return `error_type = "timeout"` within
  `timeout + kill_grace + 1` seconds. Test: unit (real subprocess, 0.5 s timeout, a tool that
  starts a grandchild `sleep`; the grandchild is gone afterwards).
- **REQ-HUB-26** Output a tool writes to `sys.stdout` or file descriptor 1 MUST NOT reach the
  worker protocol channel or an MCP stream. Test: unit (tool uses `print` and `os.write(1)`;
  the result is intact) and integration (REQ-HUB-37).
- **REQ-HUB-27** A worker's environment MUST contain only the allowlisted variables, the
  module's declared `env` names and the variables the hub sets. Test: unit (a fake
  `HAL_GATEWAY_TOKEN` in the hub's environment is absent in the worker).
- **REQ-HUB-28** A worker that exits during a call MUST produce `error_type =
  "worker_crashed"`; the call MUST NOT be retried; the next call MUST start a fresh worker.
  Test: unit.
- **REQ-HUB-29** A worker that does not send `ready` within its startup timeout MUST be
  killed, the call MUST fail with `error_type = "worker_failed"`, and further calls to that
  module MUST fail fast for 30 s. Test: unit (`FakeClock`).
- **REQ-HUB-30** v1 `exec` modules MUST keep working through the one-shot protocol (function
  name as the last argument, JSON on stdin, JSON or text on stdout), with the same process
  group kill on timeout. Test: unit.
- **REQ-HUB-31** A nested call from a worker MUST be allowed only to a tool listed in the
  calling module's `calls`, only when the verdict is `allow`, and only one level deep. Test:
  unit.
- **REQ-HUB-32** Cancelling a call (MCP `notifications/cancelled`, or the Python cancel event)
  MUST kill the worker running it within 1 s. Test: unit.
- **REQ-HUB-33** A result longer than the tool's `max_output` MUST be cut to head and tail
  with a marker giving the number of characters removed; a protocol line over 8 MiB MUST give
  `error_type = "output_too_large"`. Test: unit.
- **REQ-HUB-69** A secret declared in `[[module.secrets]]` MUST reach only a worker of the
  declaring module, only inside the `call` message on the protocol channel, never through the
  environment or argv; every exact secret value MUST be replaced with `[redacted]` in the
  result before it reaches the caller or the audit log (01 REQ-SEC-50, REQ-SEC-51). Test:
  integration (`test_runner_secrets.py`, the test 01 names).
- **REQ-HUB-70** When bubblewrap can start an unprivileged namespace, workers for tools with
  `network = false` SHOULD run under `bwrap --unshare-net --die-with-parent --new-session`
  and all other workers under `bwrap --die-with-parent --new-session`; when it cannot,
  workers run without it and `hal-hub doctor` says so (01 REQ-SEC-58). Test: integration
  (`test_runner_sandbox.py`, the test 01 names, marked `bwrap`: a fixture tool that opens a
  TCP socket fails).

### Search

- **REQ-HUB-34** Search MUST keep BM25 with k1 = 1.2 and b = 0.75 and the 25 % floor, and the
  hub's synonym table MUST NOT contain pack names or pack-specific words; pack vocabulary
  comes only from manifest `keywords`. Test: unit (ranking fixtures, and a check that no
  synonym value is a module name of the test fixtures or of the hal-tools pack list).
- **REQ-HUB-35** `function_specs` MUST return the `always_offer` tools (at most 8, sorted by
  name), then `[hub].pinned`, then search hits, without duplicates and without tools the
  session may not use. Test: unit.

### MCP server

- **REQ-HUB-36** The MCP server MUST use the official SDK (`mcp>=2.3,<3`) and MUST serve
  clients that use the `initialize` handshake (2024-11-05 to 2025-11-25) and clients that use
  the 2026-07-28 per-request envelope. Test: unit (SDK client in-memory in `legacy` and
  `2026-07-28` modes) and integration (stdio subprocess in both modes).
- **REQ-HUB-37** In stdio mode nothing but JSON-RPC messages MUST appear on stdout. Test:
  integration (spawn `hal-hub mcp` with a fixture tool that prints; every stdout line parses
  as JSON-RPC).
- **REQ-HUB-38** The server MUST support `meta` and `flat` modes. In `flat` mode a
  `tools/call` for a tool that is not listed MUST fail. Test: unit.
- **REQ-HUB-39** Tool annotations MUST be derived from the tier by 01's table ("MCP
  annotations") and MUST NOT be read back as input to any decision (01 REQ-SEC-87). Test:
  unit (`test_annotations.py`, the test 01 names).
- **REQ-HUB-40** In approval mode `auto` or `elicit`, on a connection whose client declared
  form elicitation and whose origin is not inside a box, the `elicitation` surface MUST be
  offered to 01's router: `elicitation/create` during the call on handshake-era connections,
  `input_required` with `requestState` on 2026-07-28 connections. A retry whose arguments
  give a different key MUST be refused and audited `approval_mismatch`; a decline or cancel
  MUST give `error_type = "approval_declined"`; no answer within 01's `approval.timeout_s`
  MUST give `policy_denied` with rule `approval-timeout`. Test: unit (both eras).
- **REQ-HUB-41** Without the elicitation surface, mode `auto` MUST wait for the other host
  surfaces (control socket, desktop notification) up to 01's `approval.timeout_s`, sending
  `notifications/progress` every 15 s when the request carried a progress token; mode
  `elicit` MUST deny with rule `no-approver`. Test: unit.
- **REQ-HUB-42** In mode `callback` (the broker), elicitation MUST NOT be used even when the
  client declares it; only the broker's approver answers (resolution B4). Test: unit.
- **REQ-HUB-43** The HTTP transport MUST bind only `127.0.0.1` or `::1`; any other host MUST
  be a `ConfigError` (exit 2). Test: unit.
- **REQ-HUB-44** Every HTTP request MUST carry `Authorization: Bearer <token>` compared in
  constant time (else 401), and MUST be refused with 403 when its `Origin` is present and not
  on the allow list or its `Host` is not the loopback name and port. Test: integration
  (ephemeral port, one case per rule).
- **REQ-HUB-45** The HTTP token file MUST be created 0600 through `halkit.secrets` when
  missing, and the server MUST refuse to start when it is group- or world-readable. Test:
  unit.
- **REQ-HUB-46** The text result of a tool with `untrusted_output = true` MUST be wrapped in
  01's untrusted-content envelope, and every result MUST carry `_meta.hal` as 02-sandbox
  defines it (`status`, `untrusted`, `guard`). Test: unit.
- **REQ-HUB-47** An error result MUST NOT contain a traceback. Test: unit.
- **REQ-HUB-48** `tools/list` MUST be in a stable order: meta tools first, then tools sorted by
  name. Test: unit.
- **REQ-HUB-71** `hal-hub mcp --channel remote` MUST use approval mode `auto` with the
  `elicitation` surface (when the client declares it and the display fits) and the `control`
  surface (`hal approve` on the desktop); the `notify` surface MUST NOT be offered, and an
  explicit `--approvals` other than `auto` with `--channel remote` MUST be a usage error
  (exit 2). A client without elicitation MUST wait for `hal approve` up to 01's
  `approval.timeout_s` (resolution B4; 07 REQ-RMT-25). `--origin` MUST be accepted only as
  `remote:<client>` together with `--channel remote` (else exit 2); `HAL_REMOTE_ADDR` MUST be
  recorded as `remote_addr` in every audit record of that session (resolution B8, F5). Test:
  unit (`test_mcp_remote.py`, the test 07 REQ-RMT-25 names:
  `test_remote_client_without_elicitation_waits_for_hal_approve`).
- **REQ-HUB-72** Tools from a registered `ToolProvider` MUST be listed, searched and called
  through the same pipeline as pack tools, with names `<name>__<tool>`, pack `mcp-<name>`, and
  the provider's default tier (01 REQ-SEC-88). Test: unit (`test_providers.py`).

### Pack entry point and client

- **REQ-HUB-49** `halhub.pack.main(pack, modules, argv=None, *, extra=None)` MUST provide
  `mcp`, `tools`, `call` and `doctor` for the named modules of one pack, using the same
  pipeline as `hal-hub`, with profile `standalone` by default. Each `extra` entry MUST add a
  human-only subcommand that receives the remaining argv and returns the exit code; it is
  never listed or callable as a tool, and a name that clashes with a built-in subcommand or
  does not match `^[a-z][a-z0-9-]{0,31}$` MUST raise `ValueError`. Test: unit
  (`test_pack.py::test_extra_subcommand_runs_and_is_not_a_tool`,
  `test_pack.py::test_extra_name_clash_raises`) and integration (fixture pack served over
  stdio).
- **REQ-HUB-73** `halhub.client.call(tool, args, caller)` MUST go through the nested-call
  channel when it runs inside a worker (`HAL_HUB_WORKER=1`) and through a process-local hub
  otherwise, and MUST raise `ToolDenied` for a deny (including an `ask` with no approver) and
  `ToolFailed` for a tool error. Test: unit (`test_client.py`).

### Audit

- **REQ-HUB-50** Every front end MUST write 01's audit records (`decision`,
  `self_approval_attempt`, `approval_request`, `approval`, `approval_mismatch`, `review`,
  `guard_scan`, `result`) through the session's `EventSink`; the hub MUST NOT write
  `hub-calls.jsonl` or any other log of its own (resolution B8). Test: unit
  (`test_audit_sink.py`, one case per front end).
- **REQ-HUB-51** When a record before execution cannot be written, the call MUST be refused
  with `error_type = "audit_unavailable"`; there is no setting that changes this. When the
  `result` record cannot be written after the tool ran, the result MUST still be returned and
  the missing record shows as a gap in `seq` (01 REQ-SEC-40). Test: unit (read-only audit
  directory).

### CLI

- **REQ-HUB-52** `hal-hub call` with no tool name MUST print usage and exit 2. Test: unit.
- **REQ-HUB-53** `--json` and `--yes` MUST work together, and `--yes` MUST be refused with
  exit 2 unless stdin is a terminal and `HAL_HARNESS` is unset (resolution B4). Test: unit.
- **REQ-HUB-54** A subcommand MUST NOT shadow a tool: subcommand names contain no underscore,
  tool names always do. Test: unit.
- **REQ-HUB-55** A malformed `config.toml` or `policy.toml` MUST make every subcommand exit 2
  with the file and line, without a traceback. Test: unit.
- **REQ-HUB-56** Exit codes MUST be: 0 success, 1 tool error, 2 usage or configuration error,
  3 denied or approval missing. Test: unit.
- **REQ-HUB-57** `hal-hub doctor` MUST start each module's worker, check that every declared
  function exists, and compare each function's signature with the manifest. Test: unit.
- **REQ-HUB-58** On the CLI an `ask` verdict MUST be answered by the `tty` surface (a `y/N`
  prompt showing 01's rendered request, or `--yes` as a `once` answer, REQ-HUB-53) when stdin
  and stdout are terminals; otherwise it MUST wait for the control socket and notification
  surfaces up to `approval.timeout_s` and exit 3 on deny or timeout. Test: unit.

### Install steps and units

- **REQ-HUB-59** Declared install steps MUST run only after a `y` typed on a terminal for each
  step (no `--yes`, refused without a TTY; resolution B4, 01 REQ-SEC-84), each with its
  timeout, stopping at the first non-zero exit; a step that already succeeded with the same
  digest MUST be skipped. Test: unit.
- **REQ-HUB-60** `ModuleSpec.units` MUST list the declared unit files as absolute paths; for a
  v1 module found on disk it MUST list `systemd/*.{service,timer,target,path}` and
  `desktop/*.desktop` by convention. Test: unit.

### Python API

- **REQ-HUB-61** The module-level `halhub.call(name, args)` MUST be a deprecated alias of
  `halhub.client.call` that returns a dict with the v1 keys `result` (success) or `error`
  (failure) plus `status`, and warns once per process. Test: unit.
- **REQ-HUB-62** `import halhub` MUST NOT import `mcp`. Test: unit (subprocess checks
  `sys.modules`).
- **REQ-HUB-74** The hub's argument normalisation followed by 01's key function MUST
  reproduce every entry of the shared vectors file `tests/data/approval_vectors.json`
  (resolution B1), which the tests of 01 and 06 also read. Test: unit
  (`test_approval_vectors.py`).

## Design

### Terms

- **Pack**: an installable unit of tools (08's list: `organiser`, `life`, `comms`, `online`,
  `desktop`, `system`, `work`, `jobs`, plus the flagships `memory` and `voice`). Distribution
  `hal-<pack>`. A pack holds one or more modules; `[module] pack` names it.
- **Module**: what one manifest declares: a name, how to run it, and its tools. The file name
  `hal-module.toml` and the `[module]` table keep their v1 names so existing manifests and
  docs stay valid.
- **Tool**: one callable function with a name, a description, a tier and parameters.
- **Front end**: a caller of the hub: Python API (`halhub.client`), `hal-hub` CLI, MCP server,
  broker (02-sandbox, through the MCP stream API), nested call from a worker.
- **Session**: a `HubSession`: the per-connection state 01's policy needs (id, origin,
  channel, profile, taint, grants, rates, approver).
- **Worker**: a child process that imports one module and runs its functions on request.

### Components and data flow

```text
 front ends                       halhub core                                       tools
 ----------                       -----------                                       -----
 halhub.client --+
 hal-hub CLI ----+--> Hub.call(name, args, session)
 MCP server -----+      1  Registry / ToolProvider lookup -> ToolSpec
 broker ---------+      2  args.normalise (aliases, coercion, hub checks)
 nested call ----+      3  halhub.policy.prepare  (01: path resolution)
                        4  session.context()     (01 Context: taint, grants, rates, workspaces)
                        5  halhub.policy.decide  (01, pure)          -> audit "decision"
                        6  ask: reviewer (01) or session.approver (01 router + surfaces)
                                                                      -> audit "approval_*"
                        7  Runner.run(Decision.args, secrets) ------------------> worker (module A)
                        8  scrub secrets, cap output                              worker (module B)
                        9  taint, guard scan (01)                  -> audit "guard_scan"
                       10  audit "result", rate record                            one-shot exec (v1)
 Registry <-- Discovery (entry points "hal.packs", packs.json, [hub].module_paths, HAL_MODULE_PATH)
 Search   <-- Registry (BM25 over name, module, description, keywords)
```

The steps are 01-security's "Components and data flow" steps 2 to 12, in that order; 01's
text is the authority on what each step decides, this spec on how the hub wires them. The
broker does not run a second pipeline: it serves the box through `serve_streams` with its own
`HubSession` (approver, taint, rate limits) and so gets the same steps (02-sandbox, 01).

### Manifest v2

The schema (every table and key, placeholders, an example and the v1 compatibility rules) is
defined in [docs/dev/manifest.md](../dev/manifest.md), the single reference for it; it moved
there from this spec at implementation (plan step 27). The requirements in "Manifest" above
still bind it.

Measured on the 33 manifests in `hal-ecosystem` [verified: parsed with `tomllib` on
2026-10-23]: 236 tools; parameter types `string` 242, `integer` 57, `boolean` 25, `array` 5
(`calendar_add.reminders`, `memory_remember.tags`, `notify_send.channels`,
`routines_save_scene.steps`, `tasks_add.tags`), `number` 5, `object` 2 (`routines_schedule.args`,
`routines_add_trigger.args`); timeouts from 30 s to 7200 s; every tool name carries its module
prefix except the hub's own; no parameter is named `confirm`, `confirmed` or starts with `_`.

### Discovery and registry

#### Sources

1. **Entry points.** `importlib.metadata.entry_points(group="hal.packs")` in the hub's own
   environment (the shared packs venv, resolution C8). Each entry point's value is a package
   name (`halcal`, or dotted `hal_organiser.calendar`); a value with `:attr` is an error. The
   entry point name is shown by `doctor` and SHOULD equal the module name.
2. **Installed-pack record.** `state_dir()/packs.json`, written by 06-core-cli's installer
   (resolution C8). For each module of each pack it records the manifest path and the
   interpreter argv of the pack's environment (shared, own, or the `system-site-packages` env
   of `desktop`). This is how
   packs outside the hub's environment are found without importing their metadata.
3. **`[hub].module_paths`** in `config.toml`: directories scanned recursively. Development
   checkouts and the transition from `hal-ecosystem` use it.
4. **`HAL_MODULE_PATH`**: colon-separated directories, scanned the same way. Unlike v1 it adds
   to `module_paths` instead of replacing them.

The built-in `hub` module (`halhub/builtin/hal-module.toml`) is always present and is not
discovered.

The fields this spec reads from `packs.json` are defined in 06-core-cli.md, section "Packs"
(06 owns the file and its schema): for each pack `p` and module `m`,
`packs.<p>.modules.<m>.manifest` (absolute path of its `hal-module.toml`) and
`packs.<p>.modules.<m>.interpreter` (argv list), plus the pack name `p` itself, which
REQ-HUB-63 checks against `[module] pack`. Enabling and disabling is not read from this file:
it is `[hub].disabled` (REQ-HUB-12), which `hal packs enable|disable` edits.

#### Interpreter

For a `python` module the worker runs with, in order: the module's `interpreter` argv from
`packs.json`; else, for a path-scanned module, its manifest `interpreter`; else the hub's own interpreter.
`exec` modules name their own command. An entry-point module in the hub's environment always
uses the hub's interpreter, because that is the environment its metadata came from.

#### Locating a manifest without importing code

For an entry point value `a.b.c`, the hub calls `importlib.util.find_spec("a")` (for a
top-level name this locates the package without running it [verified 2026-10-23 with a
throwaway prototype, since deleted: a package whose `__init__.py` prints was not imported;
`test_discovery_imports_no_pack_module` re-checks it]), takes each directory in
`submodule_search_locations`, appends `b/c`, and looks for `hal-module.toml` there and in
subdirectories up to depth 2. This is the same directory that `importlib.resources.files("a.b.c")`
returns for a regular package, without importing `a/__init__.py` or `a/b/__init__.py`. Zip
imports are not supported; `doctor` reports them.

#### Path scanning

Walk each directory with `os.scandir`, depth at most `[hub].scan_depth` (4), not following
symlinked directories. Skip names starting with `.` and `__pycache__`, `.venv`, `venv`,
`node_modules`, `build`, `dist`, `site-packages`. A directory that holds `hal-module.toml` is
a module; the walk does not descend into it. Paths in `[hub].ignore_paths` are skipped.
Depth 4 covers `hal-ecosystem/extensions/<repo>` and `hal-tools/packs/<pack>/<package>`.

#### Algorithm

1. Collect candidates from the four sources, in order: entry points, `packs.json`,
   `module_paths`, `HAL_MODULE_PATH`. A manifest path reached twice (for example a pack in the
   shared venv that is also listed in `packs.json`) counts once.
2. Parse each manifest. A `ManifestError` adds a problem and drops that candidate.
3. Drop v1 manifests named `hub` found by path scanning, with a warning (REQ-HUB-12).
   Settle each module's pack (REQ-HUB-63): a module listed in `packs.json` whose declared
   `pack` differs from the record's pack name is dropped as a problem; an unlisted module
   gets its module name as pack, with a problem noted when it declared another.
4. If two candidates share a name, raise `DuplicateModuleError` listing every
   `(name, manifest path, source)`; nothing loads. Fix: disable one in `[hub].ignore_paths`
   or uninstall it.
5. Drop modules named in `[hub].disabled` (state `disabled`, still shown by
   `hal-hub modules --all`); `hal packs enable|disable` edits that list (06-core-cli).
6. Check `requires_hal` (error) and `requires` (state `unavailable` with a reason), in
   dependency order; a cycle makes every module in it `unavailable`.

#### Interfaces

```python
# halhub/manifest.py
Tier = Literal["read", "write", "act", "send", "system"]
ParamType = Literal["string", "integer", "number", "boolean"]
ParamFormat = Literal["text", "path", "url", "host", "email", "phone", "command", "json"]

@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: ParamType = "string"
    description: str = ""
    required: bool = False
    enum: tuple[str | int | float, ...] | None = None
    default: str | int | float | bool | None = None
    list: bool = False
    item_type: Literal["string", "integer"] = "string"
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 65_536
    aliases: tuple[str, ...] = ()
    format: ParamFormat = "text"
    access: Literal["read", "write"] = "read"
    content: bool = False
    v1_object: bool = False          # v1 "object": the worker gets the parsed JSON

@dataclass(frozen=True)
class RoutineSpec:
    id_param: str
    steps_param: str

@dataclass(frozen=True)
class ToolSpec:
    name: str
    module: str
    pack: str
    function: str
    description: str
    tier: Tier
    tier_inferred: bool
    title: str | None = None
    network: bool = False
    untrusted_output: bool = False
    reviewable: bool = False
    reversible: bool = False
    runs_code: bool = False
    routine: RoutineSpec | None = None
    timeout: float = 60.0
    always_offer: bool = False
    keywords: tuple[str, ...] = ()
    max_output: int = 20_000
    params: tuple[ParamSpec, ...] = ()

    def input_schema(self) -> dict[str, object]:
        """Returns the JSON Schema object for the tool's arguments (flat, additionalProperties false)."""

    def meta(self, module: "ModuleSpec") -> "halhub.policy.ToolMeta":
        """Returns 01-security's `ToolMeta` for this tool: pack, pack_version, tier, flags,
        domains and per-parameter type, required, enum, max_length, format, access, content."""

@dataclass(frozen=True)
class SecretSpec:
    name: str
    config_key: str
    default_path: str

@dataclass(frozen=True)
class InstallStep:
    id: str
    summary: str
    run: tuple[str, ...]
    timeout: float = 600.0
    network: bool = False
    cwd: Literal["root", "state"] = "root"

@dataclass(frozen=True)
class UnitSpec:
    file: Path
    gpu: bool = False
    enable: bool = True

@dataclass(frozen=True)
class ModuleSpec:
    name: str
    pack: str
    summary: str
    version: str
    schema: Literal[1, 2]
    root: Path                      # directory holding the manifest
    manifest: Path
    source: Literal["entry_point", "installed", "path", "builtin"]
    distribution: str | None
    python: str | None
    exec: tuple[str, ...] | None
    protocol: Literal["jsonl", "oneshot"]
    interpreter: tuple[str, ...] | None   # resolved per "Interpreter"; None means the hub's
    inprocess: bool
    requires: tuple[str, ...]
    calls: tuple[str, ...]
    requires_hal: str | None
    env: tuple[str, ...]
    domains: tuple[str, ...]
    secrets: tuple[SecretSpec, ...]
    startup_timeout: float
    max_workers: int
    keywords: tuple[str, ...]
    tools: tuple[ToolSpec, ...]
    install: tuple[InstallStep, ...]
    units: tuple[UnitSpec, ...]
    desktop: tuple[Path, ...]
    warnings: tuple[str, ...]

class ManifestError(ValueError):
    path: Path
    key: str | None                 # dotted, e.g. "tools[3].params.when.type"

def load_manifest(path: Path, *, source: str, distribution: str | None = None,
                  interpreter: tuple[str, ...] | None = None) -> ModuleSpec: ...
```

```python
# halhub/discovery.py
@dataclass(frozen=True)
class Candidate:
    manifest: Path
    source: Literal["entry_point", "installed", "path"]
    distribution: str | None
    entry_point: str | None
    interpreter: tuple[str, ...] | None    # from packs.json

def discover(*, paths: Sequence[Path], use_entry_points: bool = True,
             installed: Path | None = None,          # packs.json; None skips the source
             ignore: Sequence[Path] = (), depth: int = 4,
             entry_points: Callable[[], Iterable[EntryPoint]] | None = None) -> list[Candidate]: ...

# halhub/registry.py
class RegistryError(Exception): ...
class DuplicateModuleError(RegistryError):
    duplicates: tuple[tuple[str, Path, str], ...]   # (name, manifest, source)

@dataclass(frozen=True)
class ModuleState:
    spec: ModuleSpec
    status: Literal["ok", "disabled", "unavailable"]
    reason: str = ""

@dataclass(frozen=True)
class Problem:
    path: Path
    message: str

@dataclass(frozen=True)
class PackInfo:
    name: str                       # [module] pack
    modules: tuple[str, ...]
    roots: tuple[Path, ...]         # manifest directories, for 06's unit and launcher lookup
    interpreter: tuple[str, ...] | None   # packs.json argv; None: the hub's interpreter
    enabled: bool                   # False when every module is in [hub].disabled

class Registry:
    @classmethod
    def load(cls, config: HubConfig | None = None, *, collect_errors: bool = False) -> "Registry": ...
    modules: Mapping[str, ModuleState]
    problems: tuple[Problem, ...]
    def tool(self, name: str) -> ToolSpec | None: ...        # only status "ok"
    def module_of(self, tool: ToolSpec) -> ModuleSpec: ...
    def tools(self) -> list[ToolSpec]: ...                   # status "ok", sorted by name
    def packs(self) -> list[PackInfo]: ...                   # sorted by name

def packs() -> list[PackInfo]: ...   # Registry.load().packs(); what 06 calls for units and doctor
```

`collect_errors=True` (used by `doctor`) turns a `DuplicateModuleError` into problems and
loads the rest, so the report shows everything at once. `halhub.registry.packs()` is the call
06-core-cli lists as "Pack registry"; 06's draft assumed an `env` field, which belongs to
06's own `packs.json` and is not repeated here.

### Arguments

The hub normalises arguments; 01's `decide` validates them. The split keeps one validator of
record (01) while the hub handles what only it knows: declared aliases, the CLI's string
inputs, bounds and list syntax.

#### Pipeline

1. Copy the raw mapping. A non-object (list, string) is `invalid_arguments`.
2. Set aside keys starting with `_`; they are passed on to `decide` unchanged (REQ-HUB-16).
3. Map names: exact name, then a declared alias, then (CLI front end only) the near-miss rule.
   A name that matches nothing is passed on unchanged, so `decide` rejects it as
   `bad-args:unknown` and lists the accepted names.
4. Coerce each declared value by type (table below), then check `minimum`, `maximum`, list
   items and `format = "json"`; a failure is `invalid_arguments` from the hub.
5. Check the canonical JSON is at most 1 MiB (`MAX_ARGS_BYTES`, so one call cannot fill the
   audit log or a worker pipe).
6. Hand the mapping (coerced values plus the set-aside keys) to 01's `prepare` and `decide`,
   which strip `_` keys, check names, types, `enum`, `max_length`, required and NUL, resolve
   paths, and compute the approval key over `canonical_json` (`halkit.canon`, 01-security
   "Approval binding").

The normalised value of every parameter is a flat `str`, `int`, `float` or `bool`, as 01's
canonical form requires. A `list = true` value stays a string until execution; the runner
splits it (step "Call arguments" in "Runner").

#### Coercion

| Declared | Accepted input | Normalised value |
| --- | --- | --- |
| `string` | string; integer or float (converted with `str`) | string |
| `integer` | integer (not boolean); float with integral value; string matching `^[+-]?[0-9]+$` | int |
| `number` | integer or float (not boolean); string parseable by `float()`, finite | float |
| `boolean` | boolean; string `true`, `false`, `yes`, `no`, `on`, `off`, `1`, `0` (any case); integer 0 or 1 | bool |
| `string`, `list = true` | string split on `,`, items stripped, empty items dropped, each item checked against `item_type`; a JSON array of scalars without commas is also accepted and logged | the items joined with `,` |
| `string`, `format = "json"` | a string that parses as JSON; for v1 `object` parameters also an object (serialised) | the string, re-serialised with `canonical_json` |

`enum` comparison is exact after coercion; a string that matches one enum value only when
compared without case is mapped to that value and logged. v1 turned any unknown word into
`False` for booleans; v2 rejects it.

#### Near-miss names (CLI only)

Tried only on the `hal-hub` CLI and the pack CLI, and only when neither the exact name nor a
declared alias matches. Candidates are declared parameters `p` with
`p.replace("_", "").lower() == name.replace("_", "").lower()`, or `p.startswith(name + "_")`,
or `name.startswith(p + "_")` (the v1 rule, `runner.py:45-52`). The name is mapped only when
there is exactly one candidate and the candidate is not also present in the call. The mapping
is logged at INFO and listed in the result as `warnings: ["argument 'when' read as
'when_text'"]`. Model-facing front ends (MCP, broker, nested, client) never map near misses:
01 REQ-SEC-05 puts argument repair in the agent (resolution B7).

```python
# halhub/args.py
class ArgumentError(ValueError):
    tool: str
    param: str | None
    message: str

@dataclass(frozen=True)
class Normalised:
    args: dict[str, object]                 # coerced declared values plus unmatched and "_" keys
    aliased: tuple[tuple[str, str], ...]    # (given, mapped)
    warnings: tuple[str, ...]

def normalise(tool: ToolSpec, raw: Mapping[str, object] | None, *,
              near_miss: bool = False) -> Normalised: ...      # raises ArgumentError
def call_arguments(tool: ToolSpec, args: Mapping[str, object]) -> dict[str, object]: ...
    # what the function receives: lists split by item_type, v1 objects parsed
```

The approval key is not computed here. 01's `decide` returns it in `Decision.key` and the hub
never recomputes it from other data (REQ-HUB-74 checks the two together against the shared
vectors).

### Call pipeline, sessions and approvals

#### `HubSession`

```python
# halhub/session.py
@dataclass
class HubSession:
    """Per-connection state that 01-security's policy reads; one per MCP connection, CLI
    process, client process or broker session."""
    id: str                                  # 12 hex chars (02/06 session id rule, resolution E2)
    kind: Literal["code", "box", "chat", "headless", "routine", "remote", "hub"]   # 01 Context.session_kind
    origin: str                              # 01 Origin value, or "remote:<client>"
    channel: str | None                      # 01 channel profile: "voice", "telegram", "remote"
    profile: str
    policy: "halhub.policy.Policy"           # 01 load_policy(profile=..., channel=...)
    approver: "halhub.approvals.Approver | None"   # 01; None: an ask becomes deny "no-approver"
    grants: "halhub.approvals.GrantStore"    # 01
    rate: "halhub.policy.RateLimiter"        # 01
    audit: EventSink
    taint: "halhub.policy.Taint"             # starts per REQ-HUB-68
    workspaces: Callable[[], tuple[Path, ...]]
    remote_addr: str | None = None
    last_human_message: str = ""

    def context(self, now: datetime, paths: Mapping[str, "ResolvedPath"]) -> "halhub.policy.Context": ...
    def human_message(self, text: str, *, surface: str) -> None: ...
        # 01 F5: resets taint to the starting level and keeps the text for the reviewer;
        # the broker's Broker.on_human_message calls it
    def close(self) -> None: ...             # 01 GrantStore.end_session; closes the control socket
```

`Hub.open_session(...)` builds one with defaults for a front end. The broker builds its own
(02-sandbox) with its approver and passes it in `ServeOptions.session`; that is how broker
calls get the broker's taint, rates and approvals while running this pipeline.

`workspaces` returns every active session workspace for 01's REQ-SEC-09 rule. The default for
host front ends reads the session records under `runtime_dir()/s/*/` with owner-pid liveness
(file format: 02-sandbox); the broker passes its own.

#### One call, step by step

`Hub.call(name, raw_args, session, *, front_end, cancel=None) -> CallResult`:

1. Resolve the tool in the registry, then in registered providers. An unknown name still goes
   to `decide` with `meta=None`, which denies it with rule `unknown-tool` and audits it.
2. `args.normalise(tool, raw_args, near_miss=(front_end == "cli"))`. An `ArgumentError` is
   audited as a `decision` record with rule `bad-args:<code>` and returned as
   `invalid_arguments`.
3. `paths = halhub.policy.prepare(call, meta, home=...)`, where `call` is 01's `ToolCall` with
   `call_id = "c_" + 16 hex`, the session's id, origin and channel, and `routine_id` for
   routine runs.
4. `ctx = session.context(now, paths)`.
5. `d = halhub.policy.decide(call, meta, session.policy, ctx)`. Append the `decision` record
   (and `self_approval_attempt` when `d.self_approval` is not empty) through `session.audit`.
   A write failure ends the call with `audit_unavailable`.
6. `deny`: return `policy_denied` (or `invalid_arguments` for `bad-args:*`) with `d.rule` and
   `d.reason`.
7. `ask`: if `d.reviewable`, ask `halhub.reviewer` (01) with the session's last human
   message; `approve` creates a `once` grant with source `reviewer`. Otherwise, or on
   `escalate`, build 01's `ApprovalRequest` from `d` and the tool metadata and call
   `session.approver.decide(request, timeout_s=policy.approval.timeout_s)`. The answer carries
   a verdict and a scope (resolution B1 names it `Decision(verdict, scope)`; 01 publishes the
   class). `allow` adds the grant for that scope to `session.grants`; a `once` grant is
   consumed under the store's lock in the same step that starts the call. `deny` returns
   `approval_declined`; a timeout returns `policy_denied` with rule `approval-timeout`; no
   approver returns `policy_denied` with rule `no-approver`. 01's router audits `approval_request`, `approval`
   and `approval_mismatch`.
8. For a tool with a `routine` table, after a human `allow` and before running: see
   "Routine-defining tools".
9. `Runner.run(module, tool, call_arguments(tool, d.args), secrets=..., timeout=tool.timeout,
   cancel=cancel)`.
10. Replace every exact secret value injected into the call with `[redacted]`
    (`halkit.redact.scrub_text`); cut to `max_output`.
11. If `tool.untrusted_output`: raise taint to `untrusted`; when 01's guard is enabled, scan
    the text that will be returned with `halhub.guard.GuardClient` and raise taint to
    `flagged` on a flag or an error (01 REQ-SEC-34, REQ-SEC-35); audit `guard_scan`.
12. Audit `result` (a failure here is logged to stderr only; the gap shows in `seq`), record
    the call in `session.rate`, and return the `CallResult`.

`execute` refuses an `ask` decision without a grant id or an approver `allow`, and refuses
`deny` always (defence in depth for front-end bugs). Nothing modifies `d.args` between step 5
and step 9 (01 REQ-SEC-18).

#### Approval surfaces implemented here

01-security defines the `Approver` protocol (with `shows_all_args`), the router that picks
surfaces (`ApprovalRouter`), and the `control` and `notify` surfaces (01-security.md,
"Approval surfaces and protocol"). This spec adds two surfaces for the
hub's own front ends:

| Surface | `shows_all_args` | Available when | Scopes it offers |
| --- | --- | --- | --- |
| `tty` (`halhub/surfaces.py`) | true | `hal-hub` or pack CLI, stdin and stdout are terminals | `once` |
| `elicitation` (`halhub/surfaces.py`) | true, and unavailable when the rendered display exceeds `PREVIEW_CHARS` (2000) | MCP approval mode `auto` or `elicit`, the client declared form elicitation, origin not `box_harness` or `box_agent` | `once` (01's surfaces table) |

Both render 01's `display` text from the request (host-made, escaped with
`halkit.termsafe.escape`), never model text.

Surfaces per front end:

| Front end | Approver | Surfaces |
| --- | --- | --- |
| Broker (`serve_streams`, mode `callback`) | the broker's (02-sandbox) | whatever the broker's host UI offers; never elicitation (REQ-HUB-42) |
| `hal-hub mcp`, mode `auto` (default) | 01 router built by the hub | `elicitation`, `control`, `notify` |
| `hal-hub mcp --channel remote` (mode `auto`, REQ-HUB-71) | 01 router | `elicitation`, `control`; never `notify`. Without elicitation the call waits for `hal approve` (07 REQ-RMT-25) |
| `hal-hub mcp`, mode `elicit` | 01 router | `elicitation` only; without it every `ask` is `no-approver` |
| `hal-hub mcp`, mode `out-of-band` | 01 router | `control`, `notify` |
| `hal-hub mcp`, mode `deny` | none | every `ask` is `no-approver` |
| `hal-hub call`, pack CLI `call` | 01 router | `tty` when on a terminal, else `control`, `notify` |
| `halhub.client.call` outside a worker | 01 router, or none when `unattended=True` | `control`, `notify` |
| Nested call | none | every `ask` is denied (REQ-HUB-31) |

Each `hal-hub mcp` connection and each CLI or client process with an approver owns one
control socket, `runtime_dir()/control/<sid>.sock`, through 01's `halhub.approvals.control`;
`hal approvals` lists its pending requests and `hal approve ID` answers them (resolution B3).

Elicitation answers on a host harness connection are advisory: the harness runs as the user
and could answer its own dialog or run `hal approve`. 01 records this as residual risk R-new-4
(resolution B4). A sandboxed CLI (`hal box`) reaches tools only through the broker, whose mode
is `callback`, so an in-box client can never answer its own elicitation.

#### Routine-defining tools

A tool with `routine = {id = "<p>", steps = "<q>"}` (the routines pack's save, schedule and
trigger tools, resolution B10) works as 01-security, section "Routines", describes. The hub's
part:

1. Before any prompt, parse parameter `q` as a JSON list of `{"tool": str, "args": object}`
   (a single object is a one-step list), normalise each step against its target tool, and run
   01's `decide` for each step under the session's policy with `origin = routine` and no
   grants, to learn which steps would ask. A step that
   fails normalisation or names an unknown tool makes the whole call `invalid_arguments`.
2. Put the expanded steps (tool, tier, canonical arguments, step verdict) into the
   `ApprovalRequest` so the host renders every step (01 "Routines" step 1).
3. On a human `allow`, compute `steps_sha256 = sha256(canonical_json(steps))` over the
   normalised steps and add one 01 `routine` grant per step whose verdict was `ask`, bound to
   the step's key, the routine id from parameter `p`, and `steps_sha256`. A reviewer approval
   never creates routine grants (REQ-HUB-64 forbids `reviewable` on these tools).
4. When the routines daemon runs a step it calls `halhub.client.call(..., routine=...)` with
   the routine id and stored steps; the hub recomputes `steps_sha256` and passes it in 01's
   `Context.routine_steps_sha256`. An unattended step that needs approval and has no matching
   grant is denied (`ToolDenied`), and the routines pack skips it and notifies the user
   (08, resolution B10).

### Runner

#### Worker process

For a `python` module the hub starts:

```text
[bwrap ...] <interpreter...> -P -X utf8 <halhub>/_worker/hal_worker.py --module halcal.tools --functions add,agenda,import_ics
```

- `hal_worker.py` is stdlib only, supports Python 3.11 and later, and is the only file in its
  directory. It is run by path, so the module's own environment (an own-env pack's venv, the
  system Python for the `desktop` pack's PyGObject) does not need `halhub` installed.
- `-P` stops Python from putting the script's directory first on `sys.path`, so a module named
  like a stdlib or SDK module next to the worker can never shadow an import [verified: `-P`
  sets `sys.flags.safe_path` on `/usr/bin/python3` 3.14.4 and hal-voice's venv is 3.11.15].
- The process starts with `start_new_session=True`, so it leads its own process group and a
  terminal Ctrl-C does not reach it.
- For an `exec` module with `protocol = "jsonl"` the hub runs the `exec` argv; the program
  speaks the same protocol (any language).

Startup inside the worker:

1. Duplicate fd 0 and fd 1 to private descriptors for the protocol; point fd 0 at
   `/dev/null` and fd 1 at fd 2; set `sys.stdout = sys.stderr`. Tool output then goes to
   stderr whatever way it is written [verified 2026-10-23 with a prototype, since deleted: a
   tool's `print` and `os.write(1, ...)` both reached stderr and the protocol stayed intact;
   `test_print_in_tool_does_not_corrupt_protocol` re-checks it].
2. If `SSL_CERT_FILE` is unset and the interpreter's OpenSSL CA file does not exist, set it to
   `/etc/ssl/certs/ca-certificates.crt` when that exists. This keeps the fix from hal-hub
   commit `1361783` (HTTPS in relocated virtualenvs) in the process that needs it.
3. Install the module `hal_worker` in `sys.modules` (see "Nested calls" and "Secrets").
4. Import the module, look up each requested function, and send `ready`.

#### Network drop with bubblewrap

01 REQ-SEC-58 asks the runner to drop the network for tools that do not need it. Workers are
pooled per `(module, needs_network)`: a module whose tools all have `network = false` gets
only offline workers; a mixed module gets two pools. Offline workers start under
`bwrap --dev-bind / / --unshare-net --die-with-parent --new-session --`, others under
`bwrap --dev-bind / / --die-with-parent --new-session --`. Unix sockets on the filesystem
(D-Bus session bus, PulseAudio) keep working without a network namespace's loopback; abstract
sockets do not, which is why tools that need them declare `network = true`.

At first use the runner probes `bwrap --unshare-net -- true` with a 5 s timeout. If it fails
(no bubblewrap, or AppArmor restricting unprivileged user namespaces), workers start without
bwrap, the hub logs one warning, and `hal-hub doctor` reports it. This is open by design:
the drop is defence in depth (01 failure table "Runner without bwrap").

#### Environment and working directory

The worker environment is built, not inherited:

- copied from the hub when set: `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LANGUAGE`,
  `LC_*`, `TZ`, `XDG_*`, `DBUS_SESSION_BUS_ADDRESS`, `DISPLAY`, `WAYLAND_DISPLAY`,
  `PULSE_SERVER`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`
  (and lower-case forms), `HAL_CONFIG`, `HAL_POLICY`, `HAL_STATE_DIR`, `HAL_DATA_DIR`,
  `HAL_RUNTIME_DIR`, `HAL_LOG_LEVEL`;
- copied when the manifest's `env` names them;
- set by the hub: `HAL_MODULE=<name>`, `HAL_MODULE_ROOT=<root>`,
  `HAL_MODULE_STATE=<state>/modules/<name>`, `HAL_HUB_WORKER=1`, `PYTHONIOENCODING=utf-8`,
  and `PYTHONPATH=<root>` for a `python` module found on disk.

Nothing else passes, so a gateway token, a cloud API key or a session token in the hub's
environment never reaches tool code (REQ-HUB-27). A manifest `env` name that looks like a
secret (`TOKEN`, `KEY`, `SECRET`, `PASSWORD` in it) is a `doctor` warning that tells the
author to use `[[module.secrets]]`.

The working directory is `state_dir()/modules/<name>` (0700) for v2 modules, so relative writes
land in Hal's state and not in the package or the user's current directory; it is the module
root for v1 modules, as before.

#### Secrets

For each call the hub resolves the module's `[[module.secrets]]` as 01-security, section
"Secrets", says (config value, else `default_path`; `halkit.secrets.read_secret`, which
refuses loose modes) and puts them in the `call` message's `secrets` field. Only the
declaring module's worker receives them, only for that call. Inside the worker, tool code
reads them with:

```python
from hal_worker import secret      # present only inside a worker

password = secret("caldav_password")   # raises KeyError when not declared or not set
```

The worker keeps them in a per-call mapping that it clears after the `result` is sent. The
hub scrubs every injected value from the result (REQ-HUB-69). v1 modules declare no secrets,
so the one-shot protocol is unchanged.

#### Worker protocol (v1)

The wire format between the hub and a worker (framing, messages, worker rules and the
mapping of `error.type`) is defined in [docs/dev/worker-protocol.md](../dev/worker-protocol.md),
the single reference for it. The runner's side (pool, timeouts, cancellation, nested calls)
follows here.

#### Pool, timeouts and cancellation

- Per pool, up to `max_workers` warm workers. A call takes an idle worker or starts one; when
  all are busy it waits up to `[hub.runner].queue_timeout` (30 s), then fails with
  `error_type = "busy"`.
- Workers idle longer than `idle_timeout` (600 s) get `shutdown`; a worker is recycled after
  `max_calls_per_worker` (500) calls to bound leaks.
- The hub waits for `result` with a selector loop that ticks every 0.2 s. On the deadline,
  or when the call's cancel event is set, it sends SIGTERM to the process group, waits
  `kill_grace` (2 s), then SIGKILL, reaps, and returns `timeout` or `cancelled`. The worker is
  replaced on the next call [verified 2026-10-23 with a prototype, since deleted: it killed a
  sleeping tool and its grandchild `sleep` after a 1.0 s timeout;
  `test_timeout_kills_tool_and_its_children` re-checks it].
- A worker that exits during a call gives `worker_crashed` with its exit code and its last
  stderr line (at most 200 characters). The call is never retried, because it may already have
  had effects.
- A malformed line or an `id` mismatch kills the worker and gives `protocol_error`.
- Startup: no `ready` within `startup_timeout` gives `worker_failed`; the module then fails
  fast for `FAILED_START_BACKOFF` (30 s) so a broken module cannot cause a spawn storm.
- Worker stderr is read on a thread, passed through `halkit.redact.scrub_text` with the
  module's secret values, logged at DEBUG with the module name, and the last 50 lines are kept
  for `doctor` and crash messages.
- `Runner.close()` sends `shutdown` to every worker, then kills any group still alive after
  `kill_grace`. Front ends call it on exit.

#### Nested calls

A tool that needs another pack's tool (the briefing reads the weather and the agenda) calls
`halhub.client.call`, which inside a worker uses the nested-call channel:

```python
from halhub import client      # or: from hal_worker import call_tool, for packs without halhub

forecast = client.call("web_weather", {"place": "Leeds"}, caller="briefing")
```

`call_tool` sends `tool_call` and blocks for `tool_result`. The hub runs it through the
pipeline with the parent call's session and origin, and refuses it with `policy_denied`
unless the tool is in the calling module's `calls`, the verdict is `allow`, and the parent is
not itself nested. The nested call's time counts against the parent's timeout. An `ask` is
denied, because no human can be shown a prompt from inside another tool's run, and a pack
must not launder approvals (the v1 routines pack auto-confirmed inner calls). v1 modules that
run `hal-hub <tool>` as a command keep working through the CLI.

#### Call arguments

`call_arguments` builds what the function receives from `Decision.args`: `list = true`
strings are split on `,` and converted by `item_type`; v1 `object` strings are parsed. This
is the only place the normalised flat form changes shape, after the decision and outside the
key.

#### One-shot protocol (v1 `exec`)

Defined in [docs/dev/worker-protocol.md](../dev/worker-protocol.md#one-shot-protocol),
which is the single reference for it.

#### In-process modules

Only the built-in `hub` module (search and module listing, which are hub code) runs in the hub
process by default. A user may add a module to `[hub.runner].inprocess_modules` for speed; the
module must be importable from the hub's environment (no `sys.path` changes) and must declare
no secrets. It runs on a thread with the tool's timeout. Python cannot stop a thread, so on
timeout the hub returns `timeout`, marks the module `tainted`, and refuses further in-process
calls to it until the process restarts. In-process tools share the hub's memory and
environment, and their timeouts cannot be enforced; that is why they are opt-in and never the
default.

```python
# halhub/runner.py
@dataclass(frozen=True)
class RunnerConfig:
    max_workers_per_module: int = 2
    idle_timeout: float = 600.0
    startup_timeout: float = 20.0
    queue_timeout: float = 30.0
    kill_grace: float = 2.0
    max_calls_per_worker: int = 500
    max_timeout: float = 7200.0
    max_result_chars: int = 20_000
    inprocess_modules: frozenset[str] = frozenset()
    bwrap: Literal["auto", "off"] = "auto"

@dataclass(frozen=True)
class RunOutcome:
    ok: bool
    result: object = None
    error_type: str | None = None
    message: str | None = None
    worker_pid: int | None = None
    seconds: float = 0.0

class Runner:
    def __init__(self, config: RunnerConfig, *,
                 popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic) -> None: ...
    def run(self, module: ModuleSpec, tool: ToolSpec, args: Mapping[str, object], *,
            timeout: float, secrets: Mapping[str, str] | None = None,
            cancel: threading.Event | None = None,
            on_tool_call: Callable[[NestedCall], RunOutcome] | None = None) -> RunOutcome: ...
    def probe(self, module: ModuleSpec) -> WorkerInfo: ...   # start, read ready, used by doctor
    def close(self) -> None: ...
```

### External tool providers

Host MCP servers from 03-agent's `[mcp.servers.<name>]` with `host = true`, which 02's broker
runs as upstreams (resolution E6), add tools that no manifest declares. They join the registry and the pipeline through one interface:

```python
# halhub/providers.py
class ToolProvider(Protocol):
    name: str                                   # "<name>"; pack becomes "mcp-<name>"
    default_tier: Tier                          # "act" unless policy.toml says otherwise (01 REQ-SEC-88)
    def tools(self) -> list[ToolSpec]: ...      # names "<name>__<tool>", untrusted_output true
    def call(self, tool: str, args: Mapping[str, object], *, timeout: float,
             cancel: threading.Event | None = None) -> RunOutcome: ...
    def close(self) -> None: ...

class Hub:
    def add_provider(self, provider: ToolProvider) -> None: ...   # raises on a name clash
```

The hub builds each provider tool's `ToolSpec` with `pack = "mcp-<name>"`, `tier` from the
provider, `untrusted_output = true`, `reviewable = false`, and parameter types taken from the
upstream's input schema where they are scalar (other parameters become `format = "json"`
strings). Annotations from the upstream are ignored (01 REQ-SEC-87). The provider runs the
call (02 owns process handling and restarts); every other pipeline step is the hub's.

### Hub facade and Python API

```python
# halhub/hub.py
@dataclass(frozen=True)
class CallResult:
    status: Literal["ok", "error", "denied"]
    call_id: str                    # "c_" + 16 hex (01 ToolCall.call_id)
    tool: str
    result: object = None
    error_type: str | None = None
    rule: str | None = None         # 01 rule id for denials, e.g. "no-approver", "rate:send"
    message: str | None = None
    warnings: tuple[str, ...] = ()
    untrusted: bool = False
    guard: Literal["clean", "flagged", "error", "off"] = "off"
    seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """Returns the dict form: 'status', then 'result' or 'error_type', 'rule', 'message' and the v1 'error' key."""

class Hub:
    def __init__(self, registry: Registry | None = None, *, runner: Runner | None = None,
                 guard: "halhub.guard.GuardClient | None" = None,
                 reviewer: "halhub.reviewer.Reviewer | None" = None,
                 config: HubConfig | None = None) -> None: ...
    registry: Registry
    def open_session(self, *, kind: str, origin: str, channel: str | None = None,
                     profile: str | None = None, approver: "Approver | None" = None,
                     audit: EventSink | None = None, session_id: str | None = None,
                     remote_addr: str | None = None) -> HubSession: ...
    def add_provider(self, provider: ToolProvider) -> None: ...
    def find(self, query: str, limit: int = 8, *, session: HubSession) -> list[ToolSpec]: ...
    def function_specs(self, query: str, limit: int = 8, *, session: HubSession) -> list[dict[str, object]]: ...
    def call(self, name: str, args: Mapping[str, object] | None, session: HubSession, *,
             front_end: Literal["python", "cli", "mcp", "broker", "nested"],
             cancel: threading.Event | None = None) -> CallResult: ...
    def close(self) -> None: ...
```

`open_session` loads the policy with 01's `load_policy(profile=..., channel=...)`, which
raises `ConfigError` on a malformed file (01 REQ-SEC-14); front ends then exit 2 and never run
with defaults. `audit` defaults to 01's `AuditLog` on `state_dir()/audit/`.

`CallResult.to_dict()` examples:

```json
{"status": "ok", "result": {"id": "ev42"}, "warnings": []}
{"status": "error", "error_type": "invalid_arguments", "message": "calendar_add: 'when_text' is required", "error": "calendar_add: 'when_text' is required"}
{"status": "denied", "error_type": "policy_denied", "rule": "approval-timeout", "message": "calendar_import needed the user's approval and none came within 120 s. Ask the user to approve it, then call it again with the same arguments.", "error": "..."}
{"status": "denied", "error_type": "policy_denied", "rule": "runs-code", "message": "shell_run is denied by profile default (runs_code)", "error": "shell_run is denied by profile default (runs_code)"}
```

Error types: `unknown_tool`, `unavailable`, `invalid_arguments`, `policy_denied` (with
`rule`), `policy_error`, `approval_declined`, `audit_unavailable`, `timeout`, `cancelled`,
`busy`, `worker_failed`, `worker_crashed`, `protocol_error`, `manifest_mismatch`,
`output_too_large`, `tool_error`.

#### `halhub.client`

```python
# halhub/client.py
class ToolDenied(Exception):
    rule: str
    message: str
class ToolFailed(Exception):
    error_type: str
    message: str

@dataclass(frozen=True)
class RoutineRun:
    routine_id: str
    steps: str                      # the stored canonical step list

def call(tool: str, args: Mapping[str, object], caller: str, *,
         unattended: bool = False, routine: RoutineRun | None = None,
         timeout: float | None = None) -> object: ...
```

- Inside a worker (`HAL_HUB_WORKER=1`) it sends a nested call; `caller` must equal
  `HAL_MODULE` and the nested rules apply.
- Elsewhere (a pack daemon such as the routines scheduler or a reminders unit) it uses one
  process-local `Hub` and one `HubSession` with `kind = "routine"` and `origin = routine` when
  `routine` is given, else `kind = "hub"` and `origin = cli` (see "Open questions"). The
  approver is 01's router with `control` and `notify` surfaces, or none when `unattended`.
- It returns the tool's result, raises `ToolDenied` for any deny (including `ask` without an
  approver or grant) and `ToolFailed` for any other error.

Module-level API in `halhub/__init__.py`, lazy (REQ-HUB-62):

```python
def default_hub() -> Hub: ...                     # process-wide, created on first use
def call(name: str, args: dict | None = None) -> dict: ...   # deprecated alias of client.call, dict form
def find(query: str, limit: int = 8) -> list[ToolSpec]: ...
def get(name: str) -> ToolSpec | None: ...
def all_tools(refresh: bool = False) -> dict[str, ToolSpec]: ...
def function_specs(query: str, limit: int = 8) -> list[dict]: ...
def ollama_tools(query: str, limit: int = 8) -> list[dict]: ...   # deprecated alias, warns once
```

The v1 `CORE` tuple is removed; its role is taken by `always_offer` (the hub's
`hal_find_tools` plus hal-memory's `memory_remember`, `memory_fact`, `memory_search` set it in
their manifests). `Registry` refreshes on a miss only when 30 s have passed since the last
load (`REFRESH_MIN_INTERVAL`), so a long-lived process sees a newly installed pack without
rescanning on every typo. The v1 `needs_confirmation` key and its retry protocol are gone:
approvals are host-originated requests (resolution B5).

### Search

`halhub/search.py` keeps the v1 algorithm [source: `core/hal-hub/halhub/search.py`]: tokens
`[a-z0-9]+` after lower-casing and replacing `_` with space; document = name twice, module
name, description, tool keywords, module keywords; BM25 with `K1 = 1.2` and `B = 0.75`
(the usual Okapi defaults); naive plural stripping; results under 25 % of the best score
dropped (`SCORE_FLOOR = 0.25`: matches that weak came only from common words in v1 use).

What changes:

- Only tools the session may use (listing verdict not `deny`, module `ok`) are ranked.
- The synonym table keeps only generic rewrites that name no pack: `remind -> reminder`,
  `appointment -> event`, `meeting -> event`, `todo -> task`, `chore -> task`,
  `search -> find`, `color -> colour`, `louder -> volume`, `quieter -> volume`,
  `recall -> remember`. Every other v1 entry moves into the `keywords` of the module it pointed
  at (table in "Migration"), so installing a pack brings its vocabulary and the hub knows no
  pack by name.
- Ties sort by tool name, so results are deterministic.

`function_specs(query, limit)` returns OpenAI-style function definitions:
`always_offer` tools (at most `MAX_ALWAYS_OFFER = 8`, sorted by name; more are ignored with a
warning), then `[hub].pinned`, then search hits, without duplicates (REQ-HUB-35).

The listing verdict is `session.policy.tool(meta).verdict` (01's `Policy.tool`, after
precedence and invariants), computed without arguments: argument-dependent rules (paths,
domains) cannot hide a tool; they can only deny a call.

### MCP server

#### SDK and protocol versions

The server uses the low-level `mcp.server.lowlevel.server.Server` of the official SDK, with
`on_list_tools` and `on_call_tool` handlers, because tool schemas come from manifests at run
time and `MCPServer` builds schemas from Python function signatures (see Decision D2).

Verified facts (the prototypes named below lived in a session scratch directory that has
since been deleted; each fact is re-checked by the test named with it):

- PyPI `mcp` 2.3.0, released 2026-10-02, `requires_python >=3.10`; 2.0.0 was released
  2026-07-28 [verified: PyPI JSON on 2026-10-23].
- SDK 2.3.0 negotiates `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25` through
  `initialize` and `2026-07-28` through the per-request envelope; the first request of a
  connection decides the era (`serve_dual_era_loop`) [verified: read
  `mcp_types/version.py` and `mcp/server/runner.py` in a scratch venv].
- A prototype on the low-level `Server` served both eras over stdio to the SDK client:
  `server/discover` then `tools/list` and `tools/call` on 2026-07-28, `initialize` on
  2025-11-25; elicitation worked in both eras [verified 2026-10-23, prototype; re-checked by
  `test_mcp_stdio.py`].
- The same `Server` ran over a unix socket pair with `stdio_server(stdin=..., stdout=...)`,
  which is what the broker needs [verified 2026-10-23, prototype; 02-sandbox confirmed the
  `stdio_server` signature with `inspect.signature` in mcp 2.3.0].
- Claude Code 2.1.293 is installed. Its embedded changelog says local (stdio) MCP connections
  negotiate 2026-07-28 by default and `MCP_PROTOCOL_NEGOTIATION=legacy` opts out; servers that
  do not answer the newer probe are remembered for 7 days and connected the older way. It lists
  handshake versions `2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05`, `2024-10-07`,
  declares `elicitation: {form: {}, url: {}}` when a feature gate is on, has an elicitation
  dialog, and reads `MCP_TOOL_TIMEOUT` [verified: strings in the `claude` 2.1.293 binary
  on 2026-10-23; which gate enables elicitation at run time is unverified: check with
  `MCP_PROTOCOL_NEGOTIATION` unset and a hub tool at tier `act` in a manual test].

So the hub supports 2026-07-28 clients such as Claude Code 2.1.293, and 2025-11-25 clients
(Qwen Code, opencode, Codex, Goose, Cline versions that predate 2026-07-28).

#### Entry points and options

```python
# halhub/mcp.py  (imports mcp lazily)
@dataclass(frozen=True)
class ServeOptions:
    mode: Literal["meta", "flat"] = "meta"
    profile: str | None = None            # None: policy default (01-security precedence)
    channel: str | None = None            # "remote": approvals "auto" without `notify`
    origin: str = "host_mcp"              # or "remote:<client>" with channel "remote"
    remote_addr: str | None = None        # from HAL_REMOTE_ADDR
    approvals: Literal["auto", "elicit", "out-of-band", "callback", "deny"] = "auto"
    approver: "Approver | None" = None    # 01 protocol; required for "callback"
    session: HubSession | None = None     # the broker passes its own
    packs: frozenset[str] | None = None   # limit to these packs
    tools: frozenset[str] | None = None   # limit to these tools
    server_name: str = "hal-hub"

def build_server(hub: Hub, options: ServeOptions) -> "mcp.server.lowlevel.server.Server": ...
async def serve_streams(hub: Hub, read_stream, write_stream, options: ServeOptions) -> None: ...
def serve_stdio(hub: Hub, options: ServeOptions) -> None: ...
def serve_http(hub: Hub, options: ServeOptions, *, host: str = "127.0.0.1", port: int = 8742,
               token: str, allow_origins: Sequence[str] = ()) -> None: ...
```

`serve_streams` is the broker's entry (02-sandbox): it runs the same server on the text
streams of a connected unix socket, with `approvals="callback"`, the broker's `approver` and
the broker's `session`. For `hal-hub mcp`, each connection gets its own `HubSession`
(`kind = "hub"`, or `"remote"` for channel `remote`) and its own control socket.

#### stdio hygiene

`serve_stdio` sets `sys.stdout = sys.stderr` before it starts `stdio_server()`. The SDK already
points fd 1 at stderr while serving, but in the prototype an unflushed `print()` from a handler
still reached the client as a non-JSON line, while the same `print` with `flush=True` went to
stderr [verified 2026-10-23, prototype: the client logged "Failed to parse JSONRPC message"
with the printed text; `test_mcp_stdio.py` re-checks it]. With `sys.stdout` replaced, no
Python-level write can reach the wire. Logging goes to stderr (`halkit.log.setup`). Tools run
in workers, so their output never reaches the server's stdout in any case.

#### Modes

| Mode | `tools/list` | `tools/call` |
| --- | --- | --- |
| `meta` (default) | `hal_find_tools`, `hal_call`, `hal_modules`, then `always_offer` tools and `[hub].pinned` that the session may use | The three meta tools, and the listed tools by name |
| `flat` | Every tool the session may use, limited by `packs` and `tools` | Listed tools only; anything else is `unknown_tool` (REQ-HUB-38) |

Meta mode keeps the client's context small (three schemas instead of 236). Flat mode lets a
harness apply its own per-tool permission rules (for Claude Code, `mcp__hal__calendar_add`),
which is impossible when everything goes through `hal_call`. The hub's policy applies in both
modes, including calls through `hal_call` and calls to unlisted names (01 REQ-SEC-89). Flat
mode logs a warning above 100 tools, because some clients cap the tool count [unverified: the
cap differs per client; check each harness's docs when writing the how-to].

Meta tool schemas:

```json
{"name": "hal_find_tools",
 "description": "Search Hal's tools for ones that can do what the user asked. Returns names, tiers, whether approval is needed, and parameters; run one with hal_call.",
 "inputSchema": {"type": "object", "additionalProperties": false, "required": ["query"],
   "properties": {"query": {"type": "string", "description": "what you want to do, in plain words"},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "most tools to return (default 8)"}}}}
{"name": "hal_call",
 "description": "Run one of Hal's tools by name. Some tools need the user's approval; the user is asked by Hal, not by you.",
 "inputSchema": {"type": "object", "additionalProperties": false, "required": ["tool"],
   "properties": {"tool": {"type": "string", "description": "tool name from hal_find_tools"},
                  "arguments": {"type": "object", "description": "the tool's arguments"}}}}
{"name": "hal_modules",
 "description": "List the installed Hal packs and their tool names.",
 "inputSchema": {"type": "object", "additionalProperties": false, "properties": {}}}
```

`hal_find_tools` returns, per tool: `name`, `module`, `pack`, `description` (first 300
characters), `tier`, `approval` (`"none"` or `"ask"`, from the listing verdict), `parameters`
(the input schema). Its own tier is `read`. An approval for a call made through `hal_call`
binds the inner tool and its arguments, never the `hal_call` wrapper.

Server `instructions`: "Hal's tools for this computer and its owner. Search with
hal_find_tools before saying something cannot be done. When a tool needs approval, Hal asks
the user directly; wait for the result." (meta mode; flat mode drops the second sentence).

#### Annotations

Derived from the tier by the table in 01-security, section "MCP annotations" (REQ-HUB-39);
the MCP schema says clients must not trust annotations from untrusted servers, and Hal never
reads them back. The meta tools use: `hal_find_tools` and `hal_modules` `readOnlyHint = true`,
`destructiveHint = false`, `idempotentHint = true`, `openWorldHint = false`; `hal_call`
`readOnlyHint = false`, `destructiveHint = true`, `idempotentHint = false`,
`openWorldHint = true`.

`title` is the tool's `title` when set. Each listed tool also carries `_meta`:
`{"hal/tier": "act", "hal/approval": "ask"}`.

#### Results

- Success: one `TextContent`. A string result is sent as is; anything else as compact JSON
  (`ensure_ascii=False`). Results over `max_output` are cut: the first 70 % and the last 30 %
  of the allowed characters with `\n[... 12345 characters cut ...]\n` between (REQ-HUB-33).
- `untrusted_output` tools: the text is wrapped in 01's untrusted-content envelope (01-security,
  section "Taint, guard and reviewer"; resolution B11) with the guard label of step 11.
- Every result carries `_meta.hal` as 02-sandbox defines it: `status`, `untrusted`, `guard`
  (resolution E7); no approval id.
- Errors: `isError: true` and the text is the JSON of `CallResult.to_dict()` without the v1
  `error` key: `{"status":"error","error_type":"timeout","message":"jobs_pack took longer than 1800 s"}`.
  No traceback (REQ-HUB-47).
- No `structuredContent`, because tools declare no output schema (Decision D13).
- `tools/list` uses the SDK defaults `ttlMs = 0`, `cacheScope = "private"` on 2026-07-28
  connections, and a stable order (REQ-HUB-48).

#### Approvals over MCP

The `elicitation` surface is offered to 01's router when all hold: the approval mode is `auto`
or `elicit`; the session's origin is not inside a box; the connection's client capabilities
declare `elicitation` with `form` (or a bare `elicitation: {}`, which the SDK treats as form
support); the rendered request fits `PREVIEW_CHARS`; on a handshake-era connection,
`ctx.session.can_send_request` is true. Returning `input_required` to a 2026-07-28 client that
did not declare elicitation makes the SDK client fail the call with `MCPError -32600
"Elicitation not supported"` [verified 2026-10-23, prototype; re-checked by
`test_mcp.py::test_no_elicitation_capability_waits_for_host_surfaces`], which is why the
capability check comes first.

Elicitation message and schema (the message is 01's host-rendered `display`):

```text
Hal wants to run a tool that needs your approval.

calendar_import  [act]  organiser
  path: /home/u/Downloads/invite.ics

Approve only if you asked for this. Approval id apr_5f2c9a0d1e7b4c33.
```

```json
{"type": "object", "required": ["decision"],
 "properties": {"decision": {"type": "string", "title": "Run calendar_import with these exact arguments",
                             "enum": ["once", "deny"],
                             "enumNames": ["Allow once", "Deny"],
                             "default": "deny"}}}
```

The enum is always `["once", "deny"]`: 01's surfaces table gives elicitation the `once` scope
only, so a `session` grant comes from another surface. `action == "accept"` with `decision =
"once"` is an `allow` answer with scope `once`; anything else, an error, or no answer is a deny. The surface passes the answer to 01's
router with source `elicitation`; the router checks the id and key, creates the grant and
audits it. The elicitation request carries `_meta` `{"hal/approval_id": "apr_...", "hal/key":
"sha256:..."}`, the names 01 uses on ACP.

Handshake era (2025-11-25 and older): inside the `tools/call` handler the router is called;
the `elicitation` surface's `decide` sends `await ctx.session.elicit_form(message, schema,
related_request_id=ctx.request_id)` and feeds the answer to the router; the handler then runs
or refuses the call in the same request. Other surfaces (control socket, notification) stay
open in parallel and the first valid answer wins.

2026-07-28 era, shape captured from the prototype (ids, names and the server name are the
prototype's):

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"demo_wipe","arguments":{"path":"/x"},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"mcp","version":"0.1.0"},"io.modelcontextprotocol/clientCapabilities":{"elicitation":{"form":{},"url":{}}}}}}
{"jsonrpc":"2.0","id":3,"result":{"inputRequests":{"hal_approval":{"method":"elicitation/create","params":{"message":"Approve demo_wipe {\"path\": \"/x\"}?","mode":"form","requestedSchema":{"properties":{"approve":{"type":"boolean","title":"Run it","default":false}},"required":["approve"],"type":"object"}}}},"requestState":"a1","resultType":"input_required","_meta":{"io.modelcontextprotocol/serverInfo":{"name":"hal-hub-proto","version":"0.0.1"}}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"inputResponses":{"hal_approval":{"action":"accept","content":{"approve":true}}},"requestState":"a1","name":"demo_wipe","arguments":{"path":"/x"},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"mcp","version":"0.1.0"},"io.modelcontextprotocol/clientCapabilities":{"elicitation":{"form":{},"url":{}}}}}}
{"jsonrpc":"2.0","id":4,"result":{"content":[{"text":"wiped","type":"text"}],"isError":false,"resultType":"complete","_meta":{"io.modelcontextprotocol/serverInfo":{"name":"hal-hub-proto","version":"0.0.1"}}}}
```

In the hub, the first `tools/call` starts the router request on a background task and returns
`input_required` at once, with `requestState = "hal-approval:" + <approval id>`, input request
key `hal_approval`, and the schema above. The request stays pending in the router until its
timeout; the control socket can answer it meanwhile. The retry carries the answer: the hub
re-runs steps 1 to 5 on the retried arguments, and passes the answer to the router with the
retry's key. A different key is an `approval_mismatch` (01 REQ-SEC-23) and the call is refused
with `approval_declined` and the message "arguments changed after approval". The state
carries no authority by itself: an unknown, expired or answered id starts a fresh request.

Without the elicitation surface (mode `auto`, client without elicitation, or a request too
long for a dialog), the call blocks while the request is pending on the control socket and a
desktop notification (01's `notify` surface, which names the `hal approve <id>` command). The
hub sends `notifications/progress` every 15 s when the request carried a progress token. On
timeout the result is `policy_denied` with rule `approval-timeout` and a message telling the
model to ask the user and call again with the same arguments.

The 2026-07-28 changelog speaks of a Claude Code permission display read from `_meta`
`anthropic/permissionDisplay` for elicitation-driven prompts [verified: string in the binary;
its exact shape is unverified]. The hub MAY add it to the elicitation request once a manual
test shows the effect; nothing depends on it.

#### Remote sessions

07-remote's forced command execs `hal-hub mcp --transport stdio --channel remote --origin
remote:<client>` with `HAL_REMOTE_ADDR` set (resolution F5). The hub then: opens a session
with `kind = "remote"`, channel `remote` (01's channel profile, which only tightens, denies
tier `system` and pack `desktop`), origin `remote:<client>`; uses approval mode `auto` with
the `elicitation` and `control` surfaces and never `notify` (REQ-HUB-71), so a laptop client
that declares elicitation gets the dialog, and one that does not waits for `hal approve` on
the desktop up to 01's `approval.timeout_s` (resolution B4; 07 REQ-RMT-25); and records
`remote_addr` in every audit record of the session. `--origin` without `--channel remote`,
or with a value not matching `^remote:[a-z0-9][a-z0-9-]{0,31}$`, is a usage error (exit 2).

#### Streamable HTTP

- `hal-hub mcp --transport http` serves `http://127.0.0.1:8742/mcp` through the SDK's
  `streamable_http_app(json_response=False, stateless_http=False)` under uvicorn (an SDK
  dependency). Sessions stay on so handshake-era clients can receive `elicitation/create`.
  The port is in 00-overview's ports table (resolution D14).
- Bind: `127.0.0.1` (default) or `::1`; anything else is a `ConfigError` before binding
  (REQ-HUB-43, REQ-SYS-09).
- An ASGI guard runs before the SDK app, in this order (REQ-HUB-44, REQ-SYS-10):
  1. `Host` must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`, else 403;
  2. `Origin`, when present, must equal an entry of `--allow-origin` (exact match, default
     none), else 403. Harness clients send no `Origin`; browsers always do;
  3. `Authorization` must equal `Bearer <token>` under `hmac.compare_digest`, else 401 with
     `WWW-Authenticate: Bearer`.
  Refusals are audited as `listener_auth_failed` (01, rate limited). The SDK's own
  DNS-rebinding check stays on as a second layer. The SDK alone would accept a request with
  no `Origin`, accept any `http://localhost:*` origin, and answer a bad `Host` with 421
  [verified: `mcp/server/transport_security.py` and a prototype], which is why the guard
  exists.
- Token: `config_dir()/hub-token`, created by `halkit.secrets.generate_token` when missing,
  refused when its mode allows group or other access (REQ-HUB-45). `hal-hub token --rotate`
  writes a new one.
- The prototype guard answered 401 without a token, 200 with it, 403 for
  `Origin: http://localhost:3000` and 421 (SDK) for `Host: evil.example` [verified 2026-10-23
  with curl against a prototype; `test_mcp_http.py` re-checks each case]. The hub's guard
  turns the last case into 403 before the SDK sees it.
- Request bodies are capped at the SDK default of 4 MiB, the same cap as the broker's
  messages (resolution E9).

Harness configuration examples:

```bash
claude mcp add hal -- hal-hub mcp --mode flat                       # stdio, Claude Code
claude mcp add --transport http hal http://127.0.0.1:8742/mcp \
  --header "Authorization: Bearer $(cat ~/.config/hal/hub-token)"   # HTTP, Claude Code
```

```json
{"mcpServers": {"hal-hub": {"command": "hal-hub", "args": ["mcp"]}}}
```

The last form is the v1 configuration and keeps working (meta mode, same tool names).

#### Long calls

Tools may run up to 7200 s (`jobs_pack` declares 1800 s). Clients enforce their own tool
timeouts; for Claude Code the variable is `MCP_TOOL_TIMEOUT` [verified: name in the binary;
default unverified]. The hub sends `notifications/progress` every 15 s while a call runs or
waits for approval when the request carried a progress token, which some clients use to
extend their timeout [unverified per client]. The how-to tells users to raise the client
timeout for long tools.

### Pack entry point

A pack runs as its own MCP server and CLI without Hal core (resolution A3: packs may import
`halhub`, client and pack CLI only):

```python
# packs/organiser/src/hal_organiser/__init__.py
"""Command line of the organiser pack: `hal-organiser mcp|tools|call|doctor`."""

from halhub import pack

PACK = "organiser"
MODULES = ("calendar", "tasks", "clock", "notify", "briefing", "routines")


def main() -> int:
    """Runs the pack CLI and returns its exit code."""
    return pack.main(PACK, MODULES)
```

```toml
# the pack's pyproject.toml
[project]
name = "hal-organiser"
dependencies = ["hal-kit>=0.1,<0.2", "hal-hub>=0.1,<0.2"]

[project.scripts]
hal-organiser = "hal_organiser:main"

[project.entry-points."hal.packs"]
calendar = "halcal"
```

```text
hal-<pack> mcp [--transport stdio|http] [--profile P] [--module NAME]... [--port N]   flat mode
hal-<pack> tools [QUERY] [--json]
hal-<pack> call TOOL [key=value ...] [--json ARGS] [--yes]
hal-<pack> doctor
hal-<pack> <extra> [ARGS...]      human-only subcommands the pack passes in `extra`
```

```python
# halhub/pack.py
def main(pack: str, modules: Sequence[str], argv: Sequence[str] | None = None, *,
         extra: Mapping[str, Callable[[list[str]], int]] | None = None) -> int:
    """Runs the pack CLI for `modules` of `pack` and returns its exit code."""
```

`extra` maps a subcommand name to a function that takes the argv after the name and returns
an exit code. It is for human entry points such as 08's `hal-organiser calendar-connect`
(a CalDAV password prompt) and `hal-comms telegram` (the long-running bridge): they run in
the pack's process with no hub pipeline, no session and no approvals, are never tools, and
never appear in `tools`, `mcp` or search. A name equal to `mcp`, `tools`, `call` or `doctor`
raises `ValueError` at start.

`pack.main(...)` loads only the manifests of the named modules
(found by the same rules as entry-point discovery, plus the built-in module), and runs the
same subcommands as `hal-hub` with `packs={pack}`. The default profile is `standalone`
(01-security: the `default` profile minus `system` tools; resolution C9). Approvals follow
the front-end table: elicitation when the harness supports it, else the control socket and a
notification; `call` uses the `tty` surface. hal-memory uses `pack.main` for its `mcp`
subcommand. hal-voice does not: it imports nothing from Hal, registers an `exec` manifest and
keeps its own MCP server (resolution A3; 08 fixes its FastMCP import).

### Audit

The hub writes no log of its own. Every front end writes 01's audit records through the
session's `EventSink` (resolution B8):

```python
# halhub/session.py
class EventSink(Protocol):
    def append(self, event: str, **fields: object) -> None: ...   # raises 01's AuditUnavailable
```

01's `halhub.audit.AuditLog` satisfies it and is the default. The record schema, the file
layout (`state_dir()/audit/<UTC date>.jsonl`), redaction, retention and `content` handling are
01-security, section "Audit log". The hub supplies the common fields from the session
(`session`, `origin`, `channel`, `profile`, and `remote_addr` for remote sessions) and the
event fields from the pipeline steps: `decision` and `self_approval_attempt` (step 5),
approval events (step 7, written by 01's router), `review` (step 7), `guard_scan` (step 11),
`result` (step 12). Parameters with `content = true` are summarised by `AuditLog` per 01
REQ-SEC-41. Tool results are summarised as `chars` and `sha256`, never logged in full unless
`[audit].content = true`.

Write failures: any record before execution failing ends the call with `audit_unavailable`;
there is no knob. A failed `result` record after the tool ran is logged to stderr and the
result is returned; `hal audit show` shows the gap in `seq` (01).

`hal-hub log` is removed; `hal audit tail` (06, 01) reads the one log.

### CLI

```text
hal-hub [--config PATH] [--policy PATH] [-v|-q] COMMAND

  tools [QUERY] [--limit N] [--all] [--json]      list tools, or the best matches for a request
  show TOOL [--json]                              schema, tier, flags, verdict per profile
  modules [--all] [--json]                        packs and modules with status, source and path
  call TOOL [key=value ...] [--json ARGS] [--yes] [--profile P] [--raw]
  TOOL [key=value ...] [--yes]                    shorthand for call (tool names contain "_")
  mcp [--transport stdio|http] [--mode meta|flat] [--profile P] [--channel C] [--origin O]
      [--approvals auto|elicit|out-of-band|deny] [--packs A,B] [--tools X,Y]
      [--host 127.0.0.1] [--port 8742] [--allow-origin ORIGIN]...
  approvals [--json]                              pending requests (same as `hal approvals`)
  approve ID                                      answer one request (same as `hal approve`)
  deny ID                                         refuse one request (same as `hal deny`)
  doctor [--json]                                 check manifests, workers, signatures, units, bwrap
  setup MODULE... [--dry-run]                     run declared install steps (typed y per step)
  new NAME [--dir DIR] [--exec]                   create a v2 pack skeleton
  token [--rotate]                                print the path of the HTTP token, or rotate it
  version
```

- `key=value` values are strings and are coerced by the declared types; `--json` takes the
  whole argument object; both may not be mixed (exit 2).
- `--yes` answers `once` for the one call, through the `tty` surface; it is refused (exit 2)
  when stdin is not a terminal or `HAL_HARNESS` is set (`halkit.harness`), so a harness that
  shells out cannot use it (resolution B4, REQ-HUB-53).
- `approvals`, `approve` and `deny` call the `halhub.approvals` client functions that 06's
  `hal approvals`, `hal approve` and `hal deny` call; they exist so a standalone pack user
  without Hal core can answer a request. Same checks, same output (REQ-HUB-22).
- `--raw` prints a string result without JSON quoting; otherwise output is JSON with indent 2.
- A word without `_` that is not a subcommand is a usage error (exit 2), never a tool lookup.
- The CLI's session is `kind = "hub"`, `origin = cli`, profile `--profile` or the policy
  default; near-miss names are mapped on this front end only.
- `HAL_HARNESS` set does not stop `call` itself: the call is decided as any other, and an
  `ask` waits for a host surface.
- Fixes for the v1 bugs (measured during development, 2026-10):

| v1 bug | v2 behaviour |
| --- | --- |
| `hal-hub call` with no tool raises `IndexError` | usage, exit 2 (REQ-HUB-52) |
| `--json` with `--yes` drops `--yes` | both honoured, `--yes` only from a person at a terminal (REQ-HUB-53) |
| tools named `tools`, `modules`, `mcp`, `doctor` are shadowed | impossible: tool names contain `_` (REQ-HUB-54) |
| broken manifest warning printed to stdout, corrupting MCP | logged to stderr (REQ-HUB-10) |
| malformed `config.toml` crashes with a traceback | exit 2 with file and line (REQ-HUB-55) |
| `CONFIG_PATH` fixed at import time | resolved per load through `halkit.paths` |
| `doctor` skips `exec` modules | starts every worker (REQ-HUB-57) |
| `doctor` has an unexplained `noqa: BLE001` | worker failures are data, not exceptions |
| exit status 1 only when `"error"` in the output | exit codes per REQ-HUB-56 |

`doctor` output per module: status, source, interpreter, version, tools, worker start time,
missing functions, signature mismatches (a manifest parameter the function does not accept; a
function parameter without a default that the manifest does not mark required; a manifest
`default` that differs from the function's), env names that look secret, declared secrets
whose file is missing or has a loose mode, unit files that do not exist, install steps not
yet run, whether bubblewrap works, and registry problems. `--json` prints the same as a list
of objects.

### Install steps and units

```python
# halhub/install.py
@dataclass(frozen=True)
class StepState:
    module: str
    step: str
    digest: str            # sha256 of the rendered argv, cwd and timeout
    status: Literal["ok", "failed"]
    at: str                # UTC Z
    exit_code: int | None

def plan(module: ModuleSpec) -> list[tuple[InstallStep, StepState | None]]: ...
def run_steps(module: ModuleSpec, *, confirm: Callable[[InstallStep], bool],
              force: bool = False) -> list[StepState]: ...
```

- `plan` shows each step with its rendered argv and whether it already succeeded with the
  same digest.
- `run_steps` asks `confirm` for each step that needs to run; a `False` stops. It runs the
  step through `halkit.proc.run(argv, timeout=step.timeout, cwd=...)` and stops at the first
  failure. State lives in `state_dir()/hub/install.json` (atomic writes).
- `hal-hub setup MODULE` refuses without a terminal and asks for a typed `y` per step; there
  is no `--yes` (resolution B4, 01 REQ-SEC-84). `--dry-run` prints the plan. 06-core-cli's
  `hal packs add` calls `run_steps` with its own typed confirmation and shows the steps as part
  of the REQ-SEC-84 summary.
- This replaces `halcore/ext.py:_install_step`, which ran the argv with no confirmation, no
  timeout and no exit check, and repeated it for packs already installed
  (measured during development, 2026-10).

Units: `ModuleSpec.units` and `ModuleSpec.desktop` give absolute paths (REQ-HUB-60).
06-core-cli renders and enables them, adds `X-HalGPU=yes` and the off-flag condition for
`gpu = true` units (a template may also carry `X-HalOff=yes` for a unit that only follows `hal off`) (compatibility contract item 4 in 00-overview), and takes the pack and
module list from `halhub.registry.packs()` instead of its own regex scan
(`halcore/units.py:module_roots`, which ignores `HAL_MODULE_PATH`).

### `hal-hub new`

`halhub/scaffold.py` replaces `hal-ecosystem/scaffold.py`. `hal-hub new weather` writes, under
`--dir` (default the current directory):

```text
hal-weather/
  pyproject.toml          hal-weather; depends on hal-kit>=0.1,<0.2 and hal-hub>=0.1,<0.2; entry point and script
  README.md               what it is, install, run, test
  LICENSE                 MIT, holder from git config user.name
  src/hal_weather/
    __init__.py           module docstring; main() calls halhub.pack.main("weather", ("weather",))
    hal-module.toml       schema = 2, pack = "weather", one example tool with tier "read"
    tools.py              the example function with a docstring
  tests/test_tools.py     behaviour-named test of the example
```

It writes no `config.py`, `notify.py` or `llm.py` copies: packs use `halkit`. It refuses to
overwrite existing files. `--exec` writes an `exec` manifest and a stdlib worker script
instead, for packs that must not import Hal (the hal-voice shape).

### Configuration

`[hub]` in `config.toml` (owner: this spec), registered as `halkit.config.Table`s:

| Key | Type | Default | Reason |
| --- | --- | --- | --- |
| `module_paths` | list of str | `[]` | Development checkouts and the transition from `hal-ecosystem`; the live config uses it today |
| `ignore_paths` | list of str | `[]` | The fix for a duplicate module without uninstalling |
| `disabled` | list of str | `[]` | v1 key, kept; `hal packs disable` writes it |
| `pinned` | list of str | `[]` | v1 key, kept: tools the user wants offered directly |
| `use_entry_points` | bool | `true` | `false` for tests and to run only on-disk packs |
| `scan_depth` | int | `4` | Covers `extensions/<repo>` and `packs/<pack>/<package>`; deeper trees are unusual and slow to walk |

`[hub.runner]`:

| Key | Type | Default | Reason |
| --- | --- | --- | --- |
| `max_workers_per_module` | int | `2` | One call can run while a second waits less; 33 modules x 2 idle workers stays within a few hundred MB, and idle workers are reaped |
| `idle_timeout` | float | `600` | A warm worker answers in milliseconds; 10 minutes covers a conversation, then the memory is returned |
| `startup_timeout` | float | `20` | Import of the slowest stdlib pack is well under a second; heavy packs set their own |
| `queue_timeout` | float | `30` | A call waiting longer than this for a worker is better reported than queued |
| `kill_grace` | float | `2` | Time for SIGTERM handlers before SIGKILL |
| `max_calls_per_worker` | int | `500` | Bounds slow leaks in long-lived workers |
| `max_timeout` | float | `7200` | The largest declared timeout today (`models_pull`) |
| `max_result_chars` | int | `20000` | About 5k tokens: enough for a page of results, small for a local model's window |
| `inprocess_modules` | list of str | `[]` | Opt-in only (see "In-process modules") |
| `bwrap` | str | `"auto"` | `"off"` only for debugging a pack that bubblewrap breaks; `doctor` warns while it is off |

`[hub.mcp]`:

| Key | Type | Default | Reason |
| --- | --- | --- | --- |
| `mode` | str | `"meta"` | v1 behaviour; small context for any client |
| `approvals` | str | `"auto"` | Elicitation where the client has it, host surfaces otherwise (resolution B4) |
| `http_host` | str | `"127.0.0.1"` | Loopback only (REQ-SYS-09) |
| `http_port` | int | `8742` | Next to the console (8740) and gateway (8741); free on the development host [verified: `ss -ltn` showed no listener on 8742 on 2026-10-23] |
| `allow_origins` | list of str | `[]` | Harness clients send no Origin; a browser client must be named explicitly |

Approval timeouts, scopes, audit content and retention are not hub keys: they are 01's
`[approval]` and `[audit]` tables in `policy.toml`, so every front end behaves the same.

Environment: `HAL_MODULE_PATH` (adds scan directories), `HAL_POLICY_PROFILE` (profile, per
00-overview precedence), `HAL_REMOTE_ADDR` (read only with `--channel remote`),
`HAL_HARNESS` (read through `halkit.harness`), plus the `halkit.paths` variables. `HAL_HUB`
and `HAL_PYTHON` are removed (00-overview).

### File layout

```text
packages/hal-hub/
  pyproject.toml                 hal-hub; deps hal-kit, mcp>=2.3,<3; script hal-hub = halhub.cli:main
  README.md
  src/halhub/
    __init__.py                  module-level API (lazy)
    __main__.py                  python -m halhub -> cli.main
    cli.py                       hal-hub CLI (the only module besides __main__ that prints)
    manifest.py                  v1 and v2 parsing, ModuleSpec, ToolSpec, ParamSpec
    discovery.py                 entry points, packs.json, path scanning
    registry.py                  Registry, requires, versions, problems, packs()
    args.py                      normalisation, coercion, aliases, near-miss (CLI)
    session.py                   HubSession, EventSink
    surfaces.py                  tty and elicitation approval surfaces
    runner.py                    Runner, worker pools, bwrap, one-shot, in-process
    _worker/hal_worker.py        the worker script (alone in its directory)
    hub.py                       Hub facade, pipeline, CallResult, nested calls, routines
    providers.py                 ToolProvider interface
    client.py                    halhub.client.call, ToolDenied, ToolFailed
    search.py                    BM25 and generic synonyms
    mcp.py                       MCP server: stdio, HTTP guard, streams, remote sessions
    pack.py                      pack CLI and MCP entry point
    install.py                   install steps
    scaffold.py                  hal-hub new
    builtin/hal-module.toml      the hub's own module (schema 2)
    builtin/tools.py             hal_find_tools, hal_modules
    policy/  approvals/  audit.py  guard/  reviewer.py   owned by 01-security (same distribution)
  tests/
    test_manifest.py test_discovery.py test_registry.py test_args.py test_session.py
    test_surfaces.py test_runner.py test_worker.py test_runner_secrets.py test_runner_sandbox.py
    test_hub.py test_routine_grants.py test_providers.py test_client.py test_search.py
    test_audit_sink.py test_mcp.py test_mcp_remote.py test_annotations.py test_pack.py
    test_install.py test_scaffold.py test_cli.py test_approval_vectors.py test_policy_meta.py
    fixtures/                    synthetic packs (v1 and v2), see "Test plan"
docs/dev/manifest.md             manifest reference (moved here from this spec, plan step 27)
docs/dev/worker-protocol.md      worker protocol reference
docs/howto/write-a-tool-pack.md
docs/howto/connect-a-harness-to-hal-tools.md
```

The approval how-to is 01's `docs/howto/approve-tool-calls.md`; this spec's harness how-to
links it instead of repeating it.

## Failure modes

"Closed" means the action does not happen; "open" means work continues. Policy, approval,
guard and audit stances are 01-security's; the rows below say how the hub applies them.

| Layer | Failure | Behaviour | Stance |
| --- | --- | --- | --- |
| Discovery | One manifest malformed | That module is skipped; error on stderr and in `doctor`; others load | Closed for that module |
| Discovery | Two modules with one name | Registry does not load; CLI and MCP exit 2 naming both paths; `doctor` reports | Closed |
| Discovery | Entry point value has `:attr` or names a missing package | That entry point skipped with an error | Closed for that pack |
| Discovery | `packs.json` lists a missing manifest or interpreter | That module is a problem; others load | Closed for that module |
| Registry | `requires` unmet or cycle | Module `unavailable`, tools hidden, calls get the reason | Closed |
| Registry | `requires_hal` unmet | Module rejected | Closed |
| Config | `config.toml` malformed or unknown `[hub]` key | Exit 2 with file, line, key | Closed |
| Policy | `policy.toml` malformed or loosely permissioned | `open_session` raises; CLI and MCP exit 2; the broker's behaviour is 01/02's | Closed |
| Policy | `prepare` or `decide` raises | Deny, `policy_error`, rule `internal-error`, audited | Closed |
| Arguments | Invalid | `invalid_arguments`, nothing runs | Closed |
| Approvals | No surface available, mode `deny` or `elicit` without elicitation | Deny `no-approver` | Closed |
| Approvals | Elicitation error, decline, cancel | `approval_declined` | Closed |
| Approvals | No answer within `approval.timeout_s` | Deny `approval-timeout` | Closed |
| Approvals | Retry with changed arguments | `approval_mismatch` audited; refused | Closed |
| Approvals | Control socket cannot be created | That surface is unavailable; others still work; with none left, `no-approver` | Closed |
| Audit | Record before execution not written | `audit_unavailable`, nothing runs | Closed |
| Audit | `result` record not written | stderr warning; result returned; gap in `seq` | Open |
| Guard | Enabled but cannot start | Session does not open (01 REQ-SEC-35) | Closed |
| Guard | Error or timeout on one result | Treated as a flag; taint `flagged` | Closed |
| Runner | Worker does not start or no `ready` | `worker_failed`; module fails fast for 30 s | Closed |
| Runner | Timeout or cancel | Process group killed; `timeout` or `cancelled`; not retried | Closed |
| Runner | Worker crash mid-call | `worker_crashed`; not retried; fresh worker next time | Closed |
| Runner | Malformed protocol line | Worker killed; `protocol_error` | Closed |
| Runner | All workers busy past `queue_timeout` | `busy` | Closed |
| Runner | In-process tool overruns | `timeout`; module tainted until restart | Closed for further calls |
| Runner | Result too large | Cut with a marker (`max_output`), or `output_too_large` over 8 MiB | Open (cut) / Closed |
| Runner | Declared secret file missing or loose mode | Call fails with `tool_error` naming the secret and the fix; no worker sees a partial set | Closed |
| Runner | bubblewrap unavailable | Workers run without the network drop; warning and `doctor` line | Open (defence in depth) |
| Providers | Upstream fails | Error results for its tools; other tools unaffected (02) | Closed for that provider |
| MCP stdio | Client disconnects | Server exits; in-flight workers killed by `Runner.close`; session grants dropped | Closed |
| MCP HTTP | Non-loopback bind requested | Refuses to start | Closed |
| MCP HTTP | Missing or wrong token; foreign Origin or Host | 401 or 403; nothing reaches the SDK | Closed |
| MCP HTTP | Token file with a loose mode | Refuses to start | Closed |
| Install | Step fails or times out | Later steps skipped; state `failed`; exit 1 | Closed |
| Search | No match | Empty list; meta mode tells the model nothing fits | Open |

## Migration

What replaces what [paths in `hal-ecosystem/core/hal-hub` at `5aede59`, `hal-ecosystem/core/hal`,
`hal-ecosystem/scaffold.py`]:

| Today | Becomes |
| --- | --- |
| `halhub/registry.py` (discovery one level deep, first name wins silently, prints to stdout, config path fixed at import) | `manifest.py`, `discovery.py`, `registry.py` |
| `halhub/runner.py` (in-process `fn(**args)` with `sys.path` injection, timeout only for subprocess tools, `_confirmed` honoured, `_alias` near-miss mapping for every caller, local-time log, top-level-only redaction, traces to clients) | `args.py`, `runner.py`, `_worker/hal_worker.py`, `hub.py`; policy and audit in 01's `halhub.policy` and `halhub.audit` |
| `state_dir()/hub-calls.jsonl` call log | not written; 01's audit log (resolution B8) |
| `halhub/search.py` (60-entry synonym table naming packs) | `search.py` with generic synonyms; pack words move to manifests |
| `halhub/mcp.py` (hand-rolled stdio loop, echoes any protocol version, `-32603` for parse errors, any tool callable) | `mcp.py` on the SDK |
| `halhub/tools.py`, `hal-module.toml` (hub module) | `builtin/tools.py`, `builtin/hal-module.toml` (schema 2) |
| `halhub/__main__.py`, `bin/hal-hub` (`PYTHONPATH` launcher) | `cli.py`, console script `hal-hub` |
| `halhub/__init__.py` `CORE` and SSL fallback | `always_offer`; the SSL fallback moves into the worker |
| `halcore/hub.py` (finds hal-hub by path) | removed by 00-overview commit 13 |
| `halcore/ext.py:_install_step` | `halhub.install.run_steps`, called by 06-core-cli |
| `halcore/units.py:module_roots` (second discovery by regex) | `halhub.registry.packs()` and `ModuleSpec.units` (06-core-cli) |
| `halcore/brain.py` `_confirmed` flow (keyed by tool name, previous turn) | host-originated approvals bound to 01's key (resolution B5) |
| `hal-ecosystem/scaffold.py` | `hal-hub new` |
| pack `_hub` / `call_tool` helpers that shell out to `hal-hub` | `halhub.client.call` (08) |
| `halvoice/mcp_server.py` (FastMCP import, broken on mcp 2.x (measured during development, 2026-10)) | fixed inside hal-voice, which imports nothing from Hal (resolution A3; 08) |

Search vocabulary moved out of the hub (the hal-tools, hal-memory and hal-voice manifests add
these words to `keywords` of the named module):

| v1 synonym keys | Module |
| --- | --- |
| `lights`, `light`, `colour` (as `rgb`) | rgb |
| `music`, `song`, `pause`, `skip` | media |
| `volume`, `window`, `open`, `app`, `clipboard`, `screenshot` | desktop |
| `screen` | screen |
| `weather`, `rain`, `temperature` (weather), `news`, `google`, `define`, `convert`, `calculate`, `internet` | web |
| `remember`, `note`, `forget`, `recall` | memory |
| `schedule`, `appointment`, `meeting`, `remind` | calendar |
| `todo`, `to-do`, `chore`, `remind` | tasks |
| `timer`, `alarm`, `wake` | clock |
| `cpu`, `gpu`, `ram`, `disk`, `running`, `temperature` (sensors) | system |
| `file`, `folder` | files |
| `email`, `inbox` | mail |
| `job`, `jobs` | jobs |
| `phone` | phone |
| `wifi`, `ip`, `internet` | network |
| `shutdown`, `reboot`, `sleep`, `lock`, `battery` | power |
| `update`, `install` | packages |
| `print`, `bluetooth`, `headphones` | devices |
| `git`, `commit` | dev |
| `habit`, `water` | habits |
| `spend`, `money` | ledger |
| `birthday`, `person`, `who` | contacts |
| `brief`, `morning` | briefing |
| `focus`, `routine` | routines |
| `model` | models |
| `translate` | lang |

How existing users keep working:

- **Config.** The live `[hub]` table (`module_paths` pointing at `hal-ecosystem/core` and
  `extensions`, `pinned` with six tools) stays valid: recursive scanning finds the same 32
  extension manifests, the old `core/hal-hub` manifest is skipped with a warning
  (REQ-HUB-12), and `pinned` keeps its meaning.
- **v1 manifests** load with warnings at tier `act` (REQ-HUB-02), so under `default` every
  unconverted tool asks. The `hal-ecosystem` v1 manifests are only read while the old runtime
  is live; the upgrade installs the v2 packs (08), and the old hub keeps serving until then.
- **MCP clients** configured with `{"command": "hal-hub", "args": ["mcp"]}` get the same
  three meta tools. A client that used to send `_confirmed: true` now gets an elicitation or a
  host prompt, and the attempt is audited; that is the intended security fix.
- **Python callers** of `halhub.call` get the dict form with a deprecation warning
  (REQ-HUB-61); `needs_confirmation` is no longer produced. Packs move to
  `halhub.client.call` (08).
- **Packs that shell out** to `hal-hub <tool> key=value` keep working; an `ask` without a
  terminal waits for `hal approve` (or is denied when no surface is available).
- **Removed:** `sys.path` injection of module roots; the `parents[2]` layout inference;
  tracebacks in results; `_confirmed` as approval; near-miss mapping outside the CLI; echoing
  the client's protocol version; `bin/hal-hub`; `HAL_HUB` and `HAL_PYTHON`; the hard-coded
  `CORE` tools; the hub's import-time `SSL_CERT_FILE` change in the caller's process;
  `hub-calls.jsonl`; `[hub].v1_default_tier`, `[hub.log]`, `[hub.approvals]` and
  `[hub.mcp].approval_timeout` (they existed only in the 2026-10-23 draft).
- **Upgrade:** in the older layout `~/.local/bin/hal-hub` points at `core/hal-hub/bin/hal-hub`. The
  installer (06-core-cli) replaces it with the console script; until then the old hub keeps
  serving.

## Test plan

Hermetic unit tests (default run, no markers, no network, no real config: every test uses
`halkit.testing.TempHalHome`; clocks are `halkit.testing.FakeClock`; 01's policy, grant store,
router and audit log are the real modules on temp paths unless a test says otherwise):

- `test_manifest.py`: every key of every table, accepted and rejected
  (`test_unknown_key_in_v2_manifest_names_file_and_key`, `test_param_named_confirmed_is_rejected`,
  `test_timeout_above_cap_is_an_error_in_v2`, `test_v1_array_param_becomes_comma_list`,
  `test_v1_object_param_becomes_json_format`, `test_pack_defaults_to_module_name`,
  `test_mcp_prefixed_pack_is_rejected`, `test_per_tool_default_verdict_key_is_rejected`,
  `test_routine_tool_must_be_act_and_not_reviewable`, `test_double_underscore_tool_name_is_rejected`).
- `test_policy_meta.py`: `test_v1_tool_without_tier_registers_as_act` (also with
  `confirm = false`), `test_tool_meta_carries_format_access_and_reversible`,
  `test_annotations_are_ignored_by_decide` (01 names it; lives here because `ToolSpec.meta`
  builds the input).
- `test_discovery.py`: fake entry points injected through `entry_points=`
  (`test_recursive_scan_skips_venv_and_hidden_dirs`, `test_hal_module_path_adds_to_config_paths`,
  `test_installed_pack_record_sets_interpreter`, `test_manifest_reached_twice_counts_once`).
- `test_registry.py`: `test_pack_name_not_matching_packs_json_is_excluded`,
  `test_path_scanned_module_uses_module_name_as_pack`, `test_discovery_imports_no_pack_module`,
  `test_duplicate_module_names_both_paths`,
  `test_unmet_requirement_hides_tools_with_reason`, `test_requires_cycle_marks_both_unavailable`,
  `test_legacy_hub_manifest_on_disk_is_skipped`, `test_packs_lists_roots_and_interpreter`.
- `test_args.py`: the coercion table row by row, bounds, lists, JSON format
  (`test_boolean_is_not_an_integer`, `test_unknown_boolean_word_is_rejected`,
  `test_declared_alias_applies_to_every_front_end`,
  `test_near_miss_name_is_mapped_on_the_cli_only`, `test_near_miss_with_target_present_is_an_error`,
  `test_underscore_arguments_reach_decide_untouched`, `test_list_value_is_normalised_to_one_string`).
- `test_approval_vectors.py`: every entry of `tests/data/approval_vectors.json` (repo root,
  shared with 01 and 06) through `normalise` and 01's key function.
- `test_session.py`: `test_hub_session_starts_untrusted`, `test_untrusted_result_raises_taint`,
  `test_human_message_resets_taint`, `test_pending_request_writes_no_files`,
  `test_session_close_drops_session_grants`.
- `test_surfaces.py`: `test_tty_surface_offers_once_only`,
  `test_elicitation_unavailable_for_long_display`, `test_content_call_never_reaches_a_surface_without_all_args`,
  `test_elicitation_unavailable_for_box_origin`.
- `test_worker.py`, `test_runner.py`: real `sys.executable` workers on fixture modules with
  timeouts of 0.2 to 1 s (`test_timeout_kills_tool_and_its_children`,
  `test_print_in_tool_does_not_corrupt_protocol`, `test_worker_env_excludes_hub_secrets`,
  `test_crash_is_reported_and_not_retried`, `test_missing_ready_backs_off`,
  `test_cancel_kills_running_worker`, `test_nested_call_to_undeclared_tool_is_denied`,
  `test_nested_ask_is_denied`, `test_oneshot_v1_exec_module_still_runs`,
  `test_tainted_inprocess_module_is_refused`).
- `test_runner_secrets.py` (integration, hermetic): `test_secret_reaches_only_declaring_module`,
  `test_secret_is_not_in_worker_environment_or_argv`, `test_secret_value_is_scrubbed_from_result`.
- `test_hub.py`: the pipeline with a spy runner (`test_ask_without_approval_never_runs_the_tool`,
  `test_executed_args_are_the_decision_args`, `test_policy_exception_denies_with_internal_error`,
  `test_http_url_is_denied_by_default`, `test_reviewer_approval_creates_once_grant`,
  `test_session_scope_grant_covers_same_args_only`, `test_unknown_tool_is_audited`).
- `test_routine_grants.py` (integration, hermetic, fake routine runner):
  `test_saving_a_routine_shows_every_step`, `test_approved_save_grants_only_ask_steps`,
  `test_edited_steps_do_not_match_grants`, `test_unattended_step_without_grant_is_denied`.
- `test_providers.py`: `test_provider_tools_are_named_and_tiered`,
  `test_provider_annotations_are_ignored`, `test_provider_calls_go_through_policy`.
- `test_client.py`: `test_client_inside_worker_uses_nested_channel`,
  `test_unattended_ask_raises_tool_denied`, `test_tool_error_raises_tool_failed`.
- `test_search.py`: ranking on synthetic manifests (`test_pack_keywords_rank_pack_tool_first`,
  `test_synonyms_name_no_pack`, `test_denied_tools_are_not_offered`).
- `test_audit_sink.py`: one case per front end (`test_cli_call_writes_decision_and_result`,
  `test_mcp_call_writes_decision_and_result`, `test_broker_session_uses_its_own_sink`),
  `test_unwritable_audit_refuses_call`, `test_failed_result_record_still_returns_result`,
  `test_no_hub_calls_log_is_written`.
- `test_mcp.py`: the SDK `Client` connected in-process to `build_server(...)` in `legacy` and
  `2026-07-28` modes, with an elicitation callback that accepts once, accepts session,
  declines or never answers (`test_flat_mode_refuses_unlisted_tool`,
  `test_elicitation_approves_exact_arguments_modern`,
  `test_elicitation_approves_exact_arguments_legacy`,
  `test_retry_with_changed_arguments_is_refused`,
  `test_no_elicitation_capability_waits_for_host_surfaces`,
  `test_callback_mode_ignores_elicitation`, `test_untrusted_output_uses_security_envelope`,
  `test_result_meta_hal_has_status_untrusted_guard`, `test_error_result_has_no_traceback`).
  If in-process `legacy` mode does not exercise `elicitation/create`, these cases move to
  stdio subprocess tests (still hermetic) [unverified: check when writing the first test].
- `test_mcp_remote.py`: `test_remote_channel_uses_auto_mode_without_notify`,
  `test_remote_client_without_elicitation_waits_for_hal_approve`,
  `test_origin_requires_remote_channel`, `test_remote_addr_is_in_every_audit_record`.
- `test_annotations.py`: `test_annotations_follow_security_table` (01's table, including
  reversible `act`).
- `test_pack.py`: `test_extra_subcommand_runs_and_is_not_a_tool`,
  `test_extra_name_clash_raises`; `test_install.py` (fake `confirm`, fake `halkit.proc.run`,
  `test_setup_refuses_without_terminal`), `test_scaffold.py` (generated pack passes its own
  test), `test_cli.py` (exit codes, `call` without a tool, `--json` with `--yes`,
  `test_yes_is_refused_without_terminal`, `test_yes_is_refused_under_a_harness`, approve
  without a terminal).

Marked integration tests (`slow` unless stated):

- `tests/test_mcp_stdio.py`: spawn `hal-hub mcp` against fixture packs, run the SDK client
  over stdio in both eras; a fixture tool prints to stdout; every stdout line must parse
  (REQ-HUB-37).
- `tests/test_mcp_http.py`: `hal-hub mcp --transport http --port 0`; token, Origin, Host cases;
  non-loopback host refused (REQ-HUB-43, REQ-HUB-44).
- `tests/test_pack_mcp.py`: a fixture pack's `mcp` subcommand over stdio.
- `tests/test_runner_sandbox.py` (marker `bwrap`): a `network = false` fixture tool cannot open
  a TCP socket; a `network = true` one can reach a local listener.

Manual (recorded in the PR):

- Claude Code 2.1.293: `claude mcp add hal -- hal-hub mcp --mode flat`, call an `act` fixture
  tool, see the elicitation dialog, approve once, approve for the session, decline; repeat
  with `MCP_PROTOCOL_NEGOTIATION=legacy`.
- `hal approve` and `hal-hub approve` in a real terminal for a pending `hal-hub mcp` request;
  refused through a pipe.

Fixtures (`packages/hal-hub/tests/fixtures/`, synthetic, no personal data): a v2 pack with
read, act, reversible and untrusted-output tools and one declared secret; a v1 pack copied in
shape from the scaffold template; a pack whose import raises; a pack with a sleeping tool that
spawns a child; a pack that prints; a pack whose tool opens a TCP socket; a routines-like
module with a `routine` tool; an `exec` one-shot pack written as a small Python script; two
packs with the same name; a stub MCP upstream for the provider tests.

## Implementation plan

Preconditions: 00-overview commits 11 to 13 (hal-hub history under `packages/hal-hub`, src
layout and console script, `halcore/hub.py` removed) and the `halkit` commits, including
`canon`, `termsafe`, `redact` and `harness`. 01-security's `halhub.policy`,
`halhub.approvals`, `halhub.audit`, `halhub.guard` and `halhub.reviewer` commits land before
step 10; until then steps 1 to 9 are tested against their published interfaces with fakes.
Each step below is one commit, leaves the tests green, and updates
`packages/hal-hub/README.md` and `CHANGELOG.md` when it changes user-visible behaviour. Steps 1
to 13 add new modules beside the v1 code; step 14 switches the public API; step 26 deletes
what is left of v1.

1. `Add manifest v2 parser with v1 compatibility` - `manifest.py`, `test_manifest.py`,
   `test_policy_meta.py`, fixtures.
2. `Add pack discovery from entry points and paths` - `discovery.py`, `test_discovery.py`.
3. `Add installed pack record as discovery source` - `packs.json` reading, interpreter rule.
4. `Add registry with requires and version checks` - `registry.py`, `packs()`,
   `test_registry.py`.
5. `Add argument normalisation and coercion` - `args.py`, `test_args.py`.
6. `Add JSON-lines worker for out-of-process tools` - `_worker/hal_worker.py`,
   `test_worker.py`.
7. `Add worker pool with enforced timeouts` - `runner.py` pool, cancel, crash handling,
   `test_runner.py`.
8. `Add secrets envelope and network drop to runner` - `test_runner_secrets.py`,
   `test_runner_sandbox.py`.
9. `Add one-shot runner for v1 exec modules` - `runner.py` one-shot and in-process paths.
10. `Add hub sessions and policy call pipeline` - `session.py`, `hub.py`, `test_session.py`,
    `test_hub.py`, `test_audit_sink.py`, `test_approval_vectors.py`.
11. `Add routine grants for routine-defining tools` - `test_routine_grants.py`.
12. `Add nested tool calls and hub client` - `client.py`, nested channel, `test_client.py`.
13. `Add external tool provider interface` - `providers.py`, `test_providers.py`.
14. `Port module-level halhub API to hub facade` - `__init__.py` lazy API, built-in module,
    old tests ported to the new names.
15. `Move pack-specific synonyms out of hub search` - `search.py`, `test_search.py`.
16. `Port hal-hub CLI to hub facade` - `cli.py`, `tty` surface, `--yes` rules, all v1 CLI bug
    fixes, `test_cli.py`, `test_surfaces.py`.
17. `Add approve, deny and approvals commands` - thin wrappers over `halhub.approvals`.
18. `Port MCP server to mcp SDK 2.3` - `mcp.py` stdio, meta mode, stdout hygiene,
    `test_mcp.py`, `test_mcp_stdio.py`.
19. `Add flat mode and profile filtering to MCP server` - listing, annotations, `_meta`,
    `test_annotations.py`.
20. `Add MCP approvals through elicitation` - both eras, host-surface fallback, callback mode.
21. `Add remote channel and origin to MCP server` - `test_mcp_remote.py`.
22. `Add streamable HTTP transport with bearer token` - guard, token, `test_mcp_http.py`.
23. `Add pack entry point for standalone packs` - `pack.py`, `test_pack.py`.
24. `Add install steps with typed confirmation` - `install.py`, `setup` command.
25. `Add hal-hub new scaffold for v2 packs` - `scaffold.py`, `test_scaffold.py`.
26. `Remove v1 registry, runner, call log and MCP loop` - delete dead code; nothing imports it.
27. `Add manifest and worker protocol references` - `docs/dev/manifest.md` (the schema tables
    move out of this spec, which then links to it), `docs/dev/worker-protocol.md`, docs index.
28. `Add how-tos for tool packs and harnesses` - the two `docs/howto/` pages, each ending with
    a symptom -> fix table; the harness page links 01's approval how-to.

Cross-repo follow-ups (not commits in Hal): hal-tools and hal-memory convert their manifests
to v2 with explicit tiers, `pack`, `format` and the keywords above, and replace `hal-hub`
subprocess calls with `halhub.client.call` (08); hal-voice converts its `exec` manifest to v2
and keeps its own MCP server.

## Decisions

- **D1 Entry-point group `hal.packs`.** 00-overview fixes it and resolution C2 confirms it
  everywhere. One constant (`ENTRY_POINT_GROUP`) holds it. The manifest file name and
  `[module]` table keep "module" so the 33 existing manifests stay valid.
- **D2 Low-level SDK `Server`, not `MCPServer`.** Tool schemas come from manifests at run
  time; `MCPServer` derives input schemas from Python signatures through `func_metadata`, so
  it would need a generated function per tool or private hooks. The low-level `Server` is the
  same official SDK, takes `on_list_tools` and `on_call_tool` handlers, and handles both
  protocol eras, elicitation and the HTTP app [verified 2026-10-23, prototype].
- **D3 A duplicate module name stops the registry.** Silent first-wins (v1) hides which code
  runs; failing loudly is the brief's rule. `doctor` still reports, and `ignore_paths` fixes it
  without uninstalling.
- **D4 v1 tools load as `act`.** resolution B6: fail closed. Every shipped pack has v2
  manifests, so the cost falls only on third-party v1 packs, whose tools ask until they ship
  a v2 manifest or the user allows them by name in `policy.toml`.
- **D5 Warm workers, one call at a time each, two per pool.** A process per call costs an
  interpreter start (tens to hundreds of ms) per tool call; one process for everything would
  share state between packs. Per-module workers isolate packs from the hub and from each other
  and make timeouts enforceable by killing a process group.
- **D6 The worker is a script run with `-P` from its own directory.** Packs in other
  environments (own-env packs, system Python with PyGObject) need no `halhub` install, and no
  hub module can shadow a pack's imports.
- **D7 Flat scalar parameters; lists as comma-separated strings.** The agent rules say string
  and integer arguments, no arrays, and 01's canonical form takes flat scalars. Five existing
  parameters are arrays; `list = true` keeps the function's `list[str]` while the model and
  the approval key see one string.
- **D8 No approval store in the hub.** resolution B1 and B3: one approval module
  (`halhub.approvals`, 01) with one key, request ids `apr_` + 16 hex, scopes `once` and
  `session`, and one control socket per session. The 2026-10-23 draft's file store, 6-hex ids
  and single-use grants are gone; `hal approvals` lists the pending ids to type.
- **D9 Elicitation for host harnesses, advisory.** resolution B4: a harness's dialog is the
  most usable surface where the user already is, but an unsandboxed harness could answer it,
  so it is documented as advisory (R-new-4). The broker never uses it. `--channel remote`
  offers it first, because the user is at the laptop, with `hal approve` as the fallback; the
  desktop notification is left out because nobody is expected at the desk.
- **D10 `--yes` only from a person at a terminal.** resolution B4. Checking stdin and
  `HAL_HARNESS` stops harnesses that shell out; a harness that allocates a pseudo-terminal
  (for example `script`) is outside what the hub can protect, which 01 records (R-new-4).
  Install steps never take `--yes`.
- **D11 One audit log for every front end.** resolution B8. The hub runs outside the broker
  too (CLI, harness MCP server, client) and writes 01's records there through the same
  `EventSink` the broker uses, so one tool call has one record format wherever it ran.
- **D12 HTTP on 127.0.0.1:8742 with a static bearer token.** The MCP auth spec is OAuth and
  optional for local servers; a token file like the gateway's is enough on loopback and works
  with `claude mcp add --header`. 00-overview's ports table carries the row (resolution D14).
- **D13 Text results only.** No tool declares an output schema; `structuredContent` without a
  schema adds nothing for clients and doubles the payload.
- **D14 Progress notifications, not the Tasks extension, for long tools.** Tasks moved out of
  core in 2026-07-28 and few clients support it; long jobs go through the `core_long_task`
  tool, which runs a sandboxed `hal -p` (resolution F2).
- **D15 Nested calls allow-only and declared.** A pack must not launder approvals (the v1
  routines pack auto-confirmed inner calls); an `ask` cannot be shown to a human from inside
  another tool's run.
- **D16 v2 working directory is the module's state directory.** Relative writes must not land
  in site-packages or the user's current directory.
- **D17 Parameter `default` is documentation, checked by `doctor`; no per-tool verdict
  default.** Filling defaults in the hub would let the manifest and the function disagree
  silently; a per-tool verdict would let a pack relax policy for itself (resolution C1).
- **D18 `format`, `access`, `content`, `reversible`, `runs_code` and `routine` are manifest
  flags.** The policy needs to know which arguments are paths, URLs and bodies, which tools
  are undoable, which run code and which define routines without parsing descriptions.
  01-security owns their policy meaning.
- **D19 src layout.** 00-overview fixes `packages/hal-hub/src/halhub/`; the imported history
  moves once in its commit 12.
- **D20 One pipeline for every front end, with injected session state.** 01 says `hal-hub mcp`
  runs the same steps as the broker; putting the steps in `Hub.call` and letting the broker
  pass its `HubSession` through `serve_streams` makes that true by construction instead of by
  two implementations.
- **D21 The hub normalises, 01 validates.** Coercion and aliases are front-end concerns (CLI
  strings, declared synonyms) and must happen before the key is computed; type, enum, length
  and NUL checks stay in 01's pure `decide`, the one validator of record. Near-miss mapping
  is CLI-only (resolution B7).
- **D22 Packs outside the hub's environment are found through `packs.json`.** Entry points are
  visible only in the hub's own environment; reading another environment's metadata would mean
  running its interpreter at every load. 06's installer already knows each module's manifest
  and interpreter (resolution C8).
- **D23 Network drop per pool, not per tool.** Workers are warm and shared by a module's
  tools; splitting a mixed module into an offline and an online pool gives the drop to every
  `network = false` tool at the cost of at most one extra worker.
- **D24 Hub sessions start `untrusted`.** `hal-hub mcp`, the CLI and the client never see the
  human's messages, so 01's taint reset never fires for them; starting at `untrusted` keeps
  the reviewer's `when_untrusted` rule and the guard meaningful there (01 REQ-SEC-32 starts
  `hub` and `remote` sessions `untrusted`).

## Open questions

These are for the owners named; the resolutions do not settle them.

1. **Origin of pack daemons (01).** 01's `Origin` enum has no value for a pack process that
   calls `halhub.client.call` outside a routine (a reminders unit, the briefing timer). This
   spec uses `cli` for them; a separate `pack` origin would make the audit log clearer. Which
   does 01 want?
