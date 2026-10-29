# hal-kit

The shared library under every Hal package and pack. Python standard library only, so it
installs anywhere. Import name: `halkit`.

Design and API: [docs/design/00-overview.md](../../docs/design/00-overview.md), section
"halkit API".

Run the tests from the repository root:

```sh
uv sync --all-packages   # install the workspace
uv run pytest packages/hal-kit
```
