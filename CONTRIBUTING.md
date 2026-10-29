# Contributing

Thanks for helping. This page is the short version; [AGENTS.md](AGENTS.md) has the full rules
for code, tests and commits, and [docs/README.md](docs/README.md) indexes the design specs.

## Set up

Hal needs Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/NotEoin/Hal.git
cd Hal
uv sync --all-packages    # install every package into .venv
uv run pytest             # hermetic tests: no GPU, no network, no model
```

## Before you open a pull request

Run what CI runs:

```sh
uv run ruff check .                         # lint
uv run ruff format --check packages tests   # formatting; drop --check to fix
scripts/check-ascii.sh                      # all text is ASCII
uv run pytest                               # tests
```

Then check the list below. The pull request description has a **Summary** of what changed and
why, and this checklist:

- [ ] tests and lint are green
- [ ] docs are updated (the README, `docs/`, the CHANGELOG when it exists)
- [ ] no secrets, no generated files, no build output

## What a good change looks like

- **One logical change** per commit, with its tests and docs. Every commit passes the tests.
- **Subject** `<Verb> <object>`, such as `Add retry to the model client` or `Fix empty config
  crash`: 60 characters or fewer, no trailing period, no body.
- **A bug fix** adds a test that fails without the fix, and the test says which bug it guards.
- **Design changes** start from the spec in `docs/design/`. If code and spec disagree, fix the
  wrong one in the same pull request.
- **New dependencies** need a reason in the spec and an entry in `tests/test_dependencies.py`.
  The standard library is the default.

## Security-sensitive changes

Anything touching the sandbox, the broker, approvals, policy, the audit log or a listener needs
a test for the case that must be refused, not only the one that works. Read
[SECURITY.md](SECURITY.md) and the threat model first.

## Tests that need real infrastructure

Tests marked `gpu`, `docker`, `network`, `slow`, `bwrap`, `systemd` or `live` are skipped by
default. Run one group on purpose, for example `uv run pytest -m docker`.

| Symptom | Fix |
| --- | --- |
| `uv: command not found` | Install uv from its documentation, then open a new shell |
| `ModuleNotFoundError: halkit` | Run `uv sync --all-packages` and use `uv run pytest` |
| ASCII gate names a file | Replace the character, or write an escape in code; see `scripts/check-ascii.sh` |
| ruff reports `T201` | A library module printed; log instead, or move the output to `cli.py` |
| A test passes alone and fails in the suite | It depends on environment variables; use `TempHalHome` |
