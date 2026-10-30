# Connect a harness to the gateway

This page shows how to point a harness that speaks the OpenAI or Anthropic API at the local
gateway. Unverified against a live harness: the routes are covered by tests with fake backends.

## Steps

1. Start the gateway and check it.

   ```sh
   hal-gateway serve &     # or let the launcher start it
   hal-gateway status      # backends, mode, claims
   ```

2. Turn on the loopback listener in `~/.config/hal/config.toml`, then restart the gateway.

   ```toml
   [gateway]
   tcp = true              # off by default
   tcp_port = 8741
   ```

3. Mint a client token for the harness. It is printed once.

   ```sh
   hal-gateway token issue --name my-harness   # copy the secret now
   ```

4. Set the harness base URL and key. Use the model names from `hal-gateway models`.

   ```sh
   export OPENAI_BASE_URL=http://127.0.0.1:8741/v1   # Anthropic clients: http://127.0.0.1:8741
   export OPENAI_API_KEY=<the token from step 3>
   ```

5. Check one request.

   ```sh
   curl -s -H "Authorization: Bearer $OPENAI_API_KEY" http://127.0.0.1:8741/v1/models | head -c 300
   ```

6. Revoke the token when you stop using the harness.

   ```sh
   hal-gateway token revoke --name my-harness
   ```

| Symptom | Fix |
| --- | --- |
| Connection refused on 8741 | `tcp = false`, or the gateway is down. Set it, restart, run `hal-gateway status` |
| 401 | Wrong or revoked token. Issue a new one |
| 403 | The `Host` or `Origin` header is not allowed. Use `127.0.0.1`, or add the name to `allow_hosts` |
| 404 `model_not_found` | The name is not in `hal-gateway models`. Use a listed name or set `default_model` |
| 503 `model_unverified` | An Ollama model is not pinned. See [pin a model](pin-a-model.md) |
