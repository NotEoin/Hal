# Gateway developer notes

How `packages/hal-gateway` works and how to change it. The design is
[05-gateway.md](../design/05-gateway.md); this page says what is built.

## Configuration (`halgateway.config`)

- `load_config(path=None)` returns a `GatewayConfig`: the `[gateway]`, `[gateway.log]`,
  `[gateway.sessions]` and `[gateway.gpu]` tables, plus validated `backends` and `aliases` by name.
- Unknown keys, wrong types, a non-loopback `tcp_host`, a remote backend without `allow_remote`
  or without https, a managed router without `models_dir` and an alias that names a missing backend
  all raise `halkit.config.ConfigError`. There is no fallback to defaults.
- With no `[gateway.backends]` table, only an `ollama` backend exists.
- Check a file: `hal-gateway check-config [--config FILE]` (exit 0 ok, 3 invalid).

## Errors (`halgateway.errors`)

Each `GatewayError` subclass owns one HTTP status. `error_body(err, fmt)` renders it for the
`openai`, `anthropic` or `ollama` format.
