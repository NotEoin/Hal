# hal-remote

Hal remote: optional SSH add-on for Linux and Windows clients. Import name: `halremote`.

Status: skeleton. The package installs and carries its version; behaviour lands in later
phases of the implementation plan in
[docs/design/00-overview.md](../../docs/design/00-overview.md).

Run the tests from the repository root:

```sh
uv sync --all-packages
uv run pytest packages/hal-remote
```
