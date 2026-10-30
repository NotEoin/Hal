# Add a cloud backend

This page shows how to let the gateway reach an OpenAI-compatible cloud service. Your prompts
leave the machine on this path, so it is off until you opt in. Unverified against a live
service: the opt-in rules are covered by tests with a fake remote.

## Steps

1. Store the key in a file only you can read.

   ```sh
   install -m 600 /dev/null ~/.config/hal/openrouter-key   # create empty, mode 0600
   $EDITOR ~/.config/hal/openrouter-key                     # paste the key
   ```

2. Add the backend and an alias to `config.toml`.

   ```toml
   [gateway.backends.openrouter]
   type = "openai"
   url = "https://openrouter.ai/api"      # https is required for a remote host
   allow_remote = true                    # the explicit opt-in
   api_key_file = "~/.config/hal/openrouter-key"
   apis = ["chat", "models"]

   [gateway.aliases.cloud-coder]
   backend = "openrouter"
   model = "vendor/model-name"
   ```

3. Check and restart.

   ```sh
   hal-gateway check-config
   hal-gateway status      # the backend is listed under remote warnings
   ```

4. Give a client access only if you mean to. Box sessions never reach it; a client token needs
   a flag.

   ```sh
   hal-gateway token issue --name my-harness --allow-remote
   ```

| Symptom | Fix |
| --- | --- |
| Start fails naming `allow_remote` | The URL is not loopback. Set `allow_remote = true` or remove the backend |
| Start fails naming the URL | A remote host needs `https` |
| 403 from a client | The token has no `--allow-remote`. Issue one with it |
| The model is missing from `/v1/models` | `expose_models` is false, or `apis` lacks `models` |
