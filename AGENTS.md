# AGENTS.md

Instructions for people and coding agents working in this repository. This file is the
canonical copy; `CLAUDE.md` only points here.

## What this repo is

Hal is a private, secure framework for getting the most out of local models: an agent, a
sandbox and broker, a tool hub and a model gateway behind one `hal` command. The local model is
untrusted; its only contact with the outside is locked-down tool calls. The release is being
assembled on branch `next` as a uv workspace; the old bash launchers at the root (`hal`,
`hal-serve`, `setup.sh`) keep working until the `hal` command replaces them.

Read first: [docs/README.md](docs/README.md), then [docs/design/00-overview.md](docs/design/00-overview.md)
(packages, allowed imports, `halkit`, flows, release plan). Every behaviour has a numbered
requirement in `docs/design/`; the code and its tests cite them.

## Layout

| Path | What it holds |
| --- | --- |
| `packages/hal-kit` | `halkit`: stdlib-only paths, config, files, time, logging, subprocess, secrets, redaction, canonical JSON, terminal escaping, symlink-safe file access, egress URL checks, test fakes |
| `packages/hal-hub` | `halhub`: tool registry, runner, MCP server |
| `packages/hal-core` | `halcore`: the code behind the `hal` command |
| `packages/hal-agent`, `hal-sandbox`, `hal-gateway`, `hal-remote` | skeletons, filled in by later phases |
| `tests/` | cross-package tests: layering, dependencies, versions, style, docs, CI |
| `scripts/` | `check-ascii.sh` (the ASCII gate) and its exemption list |

## Commands

```sh
uv sync --all-packages                      # install the workspace
uv run pytest                               # hermetic tests (marked tests are deselected)
uv run pytest -m docker                     # a marked group, when you have what it needs
uv run ruff check .                         # lint
uv run ruff format --check packages tests   # formatting
scripts/check-ascii.sh                      # ASCII gate
```

Markers: `gpu docker network slow bwrap systemd live`. CI runs the first four commands on
Python 3.11, 3.12 and 3.13, plus a gitleaks scan. Run them all before you commit.

## Rules the tests enforce

- Imports follow the table in `docs/design/00-overview.md` ("Allowed imports"); `halkit`
  imports nothing from Hal, and `halkit.testing` is for test files only.
- Runtime dependencies stay inside the allowlist in `tests/test_dependencies.py`; `hal-kit`
  has none.
- No `print` in library modules (only `__main__.py` and `cli.py`). Every subprocess call has a
  timeout: use `halkit.proc.run`. Timestamps come from `halkit.timeutil` (UTC, trailing `Z`).
- Every file is ASCII (exemptions are listed with reasons in `scripts/ascii-exempt.txt`).
  Write non-ASCII characters in code as escapes.
- All packages carry the same version.

## Code and docs

- Each source file opens with a header that says what it is responsible for. Public functions,
  classes and types have a doc comment; identifiers in comments go in backticks.
- Comments say why, never what. Name constants and say where a value comes from. Suppress a
  lint rule narrowly, with the rule id and a reason.
- Fail loudly with a specific error. Libraries log; only entry points print.
- Tests are named as behaviour sentences, live in `packages/<p>/tests/test_<module>.py`, and are
  fast and deterministic. Use the fakes in `halkit.testing`; fixtures are synthetic.
- Change behaviour and its docs in the same commit. State each fact once and link to it.

## Commits and branches

- Subject: `<Verb> <object>` (`Add`, `Fix`, `Update`, `Remove`, `Move`, `Split`, `Port`), no
  article after the verb, 60 characters or fewer, no trailing period, no body, no trailers.
- One logical change per commit, with its tests and docs; every commit passes the tests.
  Mechanical changes (moves, formatting, regenerated files) get their own commit.
- `main` advances only by a merged pull request. Do not rewrite pushed history.

## Security

Do not weaken the sandbox, the broker policy or the audit log to make something work. Never
commit secrets, tokens, `.env` files, model weights or build output. Report vulnerabilities as
described in [SECURITY.md](SECURITY.md).
