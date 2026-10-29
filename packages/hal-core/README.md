# hal-core

The `hal` command and the code behind it. Import name: `halcore`. The `hal` bash launcher at
the repository root runs it with `PYTHONPATH` pointing at `packages/hal-core/src` and `packages/hal-hub/src`.

Run the tests from the repository root:

```sh
uv sync --all-packages
uv run pytest packages/hal-core
```
