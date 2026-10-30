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

## Changing a level

Run `hal lockdown` or `hal paranoia` to see the current level and what each level means. Add a
level to change it (`hal lockdown strict`). Only a person at a terminal can do this: the command
refuses when `HAL_HARNESS` is set or stdin is not a terminal, so a tool call cannot raise its own
privileges. Lowering a level asks you to type the new level back. Checked by
`packages/hal-core/tests/test_levels.py`.

## Paths that are not private or not boxed

| Path | Why |
| --- | --- |
| `hal claude`, `hal here` | Claude Code runs on the host against your files and talks to Anthropic when you choose a hosted model; not private, not sandboxed |
| `hal connect` | An outside harness gets Hal's tools without Hal's box |
| Lockdown `off` | No box at all |
| Settings that name Claude (`[brain].deep_work`, `[memory].answerer`) | Off by default; setting them sends work to Claude Code |

## Check it yourself

```
hal status    # lockdown, paranoia, classifier reachability
hal doctor    # also checks the container runtime; exit 1 on a problem
```

Verified by the tests in `packages/hal-hub/tests/test_classifier.py`,
`packages/hal-sandbox/tests/test_lockdown.py` and `packages/hal-core/tests/test_security.py`.
Not yet verified end to end: the containment suite that drives a real box with a hostile fake
model (RESOLUTIONS I6) is not written. Run `hal doctor` after any change to the container
runtime.
