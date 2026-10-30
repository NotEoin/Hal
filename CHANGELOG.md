# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions follow semver; every package
carries the same version.

## 0.1.0 - unreleased

First release of the monorepo (uv workspace on branch `next`).

### Added

- `hal-kit` (`halkit`): paths, config, files, time, logging, subprocess, secrets, redaction,
  canonical JSON, terminal escaping, symlink-safe file access, egress URL checks, test fakes.
- `hal-hub`: tool registry, runner, MCP server, policy with tool tiers, approvals bound to exact
  arguments, risk classifier with paranoia levels, local prompt-injection guard, audit log.
- `hal-sandbox`: the box (docker with runc or runsc, bubblewrap), broker, isolated coding copy
  with `accept` and `export`, checkpoints, scratchpad, lockdown levels, `hal box`.
- `hal-agent`: boxed coding agent with providers, tool-call repair, sessions, headless mode and
  an ACP server.
- `hal-gateway`: local model gateway with presets, routing, gpu-light claims, remote backends
  and a client-token admin interface.
- `hal-remote`: using the desktop's Hal from a laptop over SSH (forced command, authorize and
  revoke, server-check, tunnel supervisor); pairing and the `up`/`down`/`status` commands are
  still to come.
- Containment test suite (`tests/e2e/test_containment*.py`): a hostile process in the real box
  under runc and runsc; every attack is contained. See [docs/user/security.md](docs/user/security.md).
- No-telemetry test (`tests/test_no_telemetry.py`) and a "No telemetry" README section.

### Security

- The model is treated as hostile: every agent turn runs in a box with no network, a copy of
  the project and a scratchpad. Lockdown and paranoia can be changed only on the host.
