# Security guide

Hal treats its own model as hostile. A model that has been modified to cause damage must be
contained by the default settings. This page says what that protects you from, what is not
protected, and which two settings you control: the lockdown level and the paranoia level.
The numbered rules and tests behind it are in [the security spec](../design/01-security.md)
and [the threat model](../dev/threat-model.md).

## What Hal protects you from

| Risk | What stops it |
| --- | --- |
| A tool call that deletes, sends or runs something you did not ask for | Tool tiers and the policy: `act`, `send` and `system` tools ask first, and an approval is bound to the exact arguments you saw |
| The model approving itself | An approval is a structured answer from a surface (terminal, phone, editor), never text the model wrote; a tool call cannot carry one |
| Instructions hidden in a web page, file or tool result | Results of untrusted tools come back in a marked envelope, and the session's taint raises what needs approval |
| The agent reading your home directory or secrets | The agent runs in a box with a copy of the project at `/work` and a scratchpad at `/scratch`; nothing else of the host is mounted |
| The agent reaching the network | The box has no network; its only channels out are the tool broker and the model socket |
| The agent changing your real repository | Changes leave the box only through `hal code accept` (diff report, flags, checkpoint) or `hal code export` |
| A risky call that policy allows | The risk classifier rates every boxed call and can only make the decision stricter (see below) |
| Changing its own limits | Lockdown and paranoia are read from host files and the host environment; no tool argument or box setting can change them |

## Lockdown levels

Set `[sandbox].lockdown` in the config file on the host. `hal status` and `hal doctor` show the
level. There is no flag or environment variable on purpose: only you change it.

| Level | Meaning |
| --- | --- |
| `strict` | runsc (gVisor) required, Hal refuses to start without it; isolated copy only; resource limits; no host tool upstreams except `hal-hub`; paranoia floor `high` |
| `standard` (default) | runsc when installed, else runc with a doctor warning; isolated copy by default; limits on; host upstreams allowed by policy |
| `relaxed` | also allows `--direct` (the real project mounted read-write) and the dependency-install box with network |
| `off` | allows running without a box after a per-project consent; `hal doctor` reports a problem on every run; never a default |

## Paranoia levels

Set `[policy].paranoia` in the config file. The classifier rates each boxed call `low`,
`medium`, `high` or `critical` from fixed rules (destructive shell patterns, paths outside the
allowed roots, secrets in arguments, mass actions, send and system tools) and a local model
(`[policy].model`, through the gateway). The rules can only raise the risk. An answer that is
not the expected JSON, a timeout or an error counts as `high`. The table gives the effect:
`policy` keeps the policy's verdict, `ask` and `deny` can only tighten it, and `(+review)` means
a low-risk call that the policy would ask about may be approved once by the reviewer.

| Paranoia | low risk | medium | high | critical |
| --- | --- | --- | --- | --- |
| `off` | policy | policy | policy | policy |
| `low` | policy (+review) | policy | ask | ask |
| `balanced` (default) | policy (+review) | ask | ask | deny |
| `high` | policy | ask | deny | deny |
| `max` | ask | ask | deny | deny |

The effective level is the configured one raised to the lockdown floor (`strict` gives `high`).
With no classifier model reachable, `balanced` and above treat every call as `high`, so you are
asked more often; `hal doctor` and `hal status` print `classifier: UNREACHABLE` when that is so.
Every verdict is written to the audit log as a `classification` record.

A call to a read-only tool that no rule flagged is `low` at once, without asking the model (the
audit reason is `read tool, no rule matched; model skipped`). This is safe because a read tool
cannot change or send anything, and the risky reads (sensitive paths, paths outside the allowed
roots, secrets, mass actions) are caught by the rules, which still send those to the model. Every
other kind of tool always goes to the model. Set `[policy].classify_reads = true` to have the
model rate every read as well; the default is `false`.

### A purpose-built classifier: IBM Granite Guardian

By default the classifier asks a general model (`hal-fast`) for a JSON answer, which is slow on
a CPU. `[policy].model_format = "guardian"` instead asks IBM Granite Guardian 4.1 8B on Ollama,
a model built to judge whether a tool call is dangerous. It answers `<score> yes </score>` or
`<score> no </score>`, and Hal turns the probability of `yes` (read from the token logprobs) into
a risk:

| P(yes) at or above | Risk | Setting (default) |
| --- | --- | --- |
| 0.9 | `critical` | `guardian_critical` (0.9) |
| 0.5 | `high` | `guardian_high` (0.5) |
| 0.2 | `medium` | `guardian_medium` (0.2) |
| below 0.2 | `low` | |

The bounds must be above 0, at most 1 and ordered (`guardian_medium <= guardian_high <=
guardian_critical`). The 0.2 floor comes from a local trial on 2026-11-02: two dangerous calls
scored 0.25 and 0.45, and safe calls scored at most 0.004. If the backend sends no logprobs, the
score text decides (`yes` is `high`, `no` is `low`). Anything else, or a timeout, counts as
`high`, as with the JSON format. The audit reason shows the probability, for example
`guardian p(yes)=0.83`.

```toml
[policy]
model = "ollama/granite4.1-guardian:8b"  # backend/model: the gateway sends it to the "ollama" backend
model_format = "guardian"
gpu = false                              # always run on the CPU, so it never takes VRAM
```

- `model` must name the gateway backend and the model, as `ollama/granite4.1-guardian:8b`
  (`backend/model` is resolved by the gateway, so the model need not be an alias). A bare
  `granite4.1-guardian:8b` also works when exactly one backend serves it. Pull the model into
  Ollama yourself; Hal does not.
- `gpu = false` sends `num_gpu` 0 on every classifier call, whatever `gpu-light` says. The default
  is `true`: use the GPU while the light is green. It applies to the classifier only; the
  reviewer keeps following `gpu-light`.
- On the CPU it takes about 5 to 6 s per call (measured on 2026-11-02 for a 230-token prompt), so
  the read fast path above matters: most calls skip the model.
- It is English only. Arguments and messages in other languages are rated less reliably.
- Ollama needs logprobs support on `/api/chat` (0.12.11 or later); older versions fall back to
  the score text.

## Changing a level

Run `hal lockdown` or `hal paranoia` to see the current level and what each level means. Add a
level to change it (`hal lockdown strict`). Only a person at a terminal can do this: the command
refuses when `HAL_HARNESS` is set or stdin is not a terminal, so a tool call cannot raise its own
privileges. Lowering a level asks you to type the new level back. Checked by
`packages/hal-core/tests/test_levels.py`.

## Paths that are not private or not boxed

| Path | Why |
| --- | --- |
| `hal claude` | Claude Code runs on the host against your files and talks to Anthropic when you choose a hosted model; not private, not sandboxed |
| `hal console` | The local web console runs on the host, outside any box, against your real calendar, tasks and vault. It listens on loopback only and needs a token, but nothing contains it; the help text and the start-up banner say UNBOXED |
| `hal connect` | An outside harness gets Hal's tools without Hal's box |
| Lockdown `off` | No box at all. `hal chat` shows an UNSANDBOXED banner and asks for a typed `y` once per channel (kept in `unboxed-ok.json` in the state dir); without a terminal and without that record it exits 3. If docker answered before and is down now, `hal code`, `hal chat` and `hal acp` exit 3 instead of falling back |
| Host MCP upstreams | An upstream MCP server runs on the host, outside the box, with your user's rights; the box reaches it only through the broker, and the policy and classifier see each call, but the server itself is not contained. Add only servers you trust, as you would install a program; `strict` lockdown allows none besides `hal-hub` |
| Settings that name Claude (`[brain].deep_work`, `[memory].answerer`) | Off by default; setting them sends work to Claude Code |

## Check it yourself

```
hal status    # lockdown, paranoia, classifier reachability
hal doctor    # also checks the container runtime; exit 1 on a problem
```

Verified by the tests in `packages/hal-hub/tests/test_classifier.py`,
`packages/hal-sandbox/tests/test_lockdown.py` and `packages/hal-core/tests/test_security.py`.
Containment, verified end to end: `tests/e2e/test_containment*.py` drives the real box with a
scripted hostile process. It tries to write outside `/work` and `/scratch`, read host paths and
`~/.ssh`, follow symlink and `..` escapes, reach the network (DNS, raw IP, proxy), reach the docker
socket, escalate privileges, fork-bomb, exhaust memory and disk, flood and malform broker frames,
forge an approval, raise its own lockdown or paranoia, call tools that do not exist, and plant git
hooks and `core.fsmonitor` config in the copy. Every attempt failed; broker-side attempts are
audited. Measured 2026-10-31 on docker 29.2.1: 26 cases passed, 13 under `runc` and 13 under `runsc`
(`uv run pytest -q -m docker tests/e2e/test_containment*.py`, about 40 s; needs `alpine:3.20` and
`hal-agent:local`, see [dev/sandbox.md](../dev/sandbox.md)). Not covered: a kernel or runtime
escape, and results that smuggle instructions into the model's next turn (that is the taint and
guard path, tested in `packages/hal-hub`). Run `hal doctor` after any change to the container
runtime.
