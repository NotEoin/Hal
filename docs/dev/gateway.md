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

## Listeners and authentication (`server`, `auth`, `handler`)

- `Gateway(cfg).start()` binds an owner-only unix socket (dir 0700, socket 0600) and, with
  `[gateway].tcp = true`, a loopback TCP listener. `stop()` closes both.
- Unix peers are checked with `SO_PEERCRED`; another uid gets 403 before any body is read.
  Tests inject the uid lookup (`Gateway(cfg, peer_uid=..., own_uid=...)`).
- TCP needs the owner token (`Authorization: Bearer` or `X-Api-Key`), a Host in the loopback
  set plus `allow_hosts`, and no Origin unless listed in `allow_origins`. `GET /health` needs no token.
- The owner token file is created 0600 at the first start. TCP refuses to start if it is
  readable by group or others.
- A stale socket is replaced after a refused connect; a live one stops the start.
- Connections beyond `max_connections` get 503 with `Retry-After: 1`.
- Session and client tokens (`hal-sess-`, `hal-gwc-`) are refused with 401 until their registries
  are built (plan steps 19 and 21). The handler serves `/health` only; routes come with step 3.
