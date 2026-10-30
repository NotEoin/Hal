# hal-remote

Hal remote: optional SSH add-on for Linux and Windows clients. Import name: `halremote`.

Status: CLI skeleton; every subcommand exits 2 "not implemented" until its step lands in the
implementation plan of
[docs/design/07-remote.md](../../docs/design/07-remote.md).

Run the tests from the repository root:

```sh
uv sync --all-packages
uv run pytest packages/hal-remote
```
