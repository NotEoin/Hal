# Point Hal at your models

Hal uses models you already have. This page adds a source, lets Hal tune it to your hardware
and checks the result. Nothing is downloaded.

## Steps

1. Make sure a model server is running, or that you have GGUF files in a folder.

   ```sh
   curl -s http://127.0.0.1:11434/api/tags | head -c 200   # Ollama: should print JSON
   ls ~/models/*.gguf                                      # GGUF: your files
   ```

2. Add the source. Use the kind that matches: `ollama`, `openai` (LM Studio, llama.cpp server,
   vLLM), `gguf`, `anthropic` or `openai-cloud`.

   ```sh
   hal models add-source ollama http://127.0.0.1:11434   # Ollama
   hal models add-source gguf ~/models                   # a folder of GGUF files
   hal models add-source openai http://127.0.0.1:1234/v1 --key-file ~/.config/hal/lms.key   # LM Studio
   ```

   A cloud key goes in a file (`chmod 600`), never in the config:
   `hal models add-source anthropic --key-file ~/.config/hal/claude.key`.

3. List what Hal can see.

   ```sh
   hal models list   # every source, every model, size, quantisation, capabilities
   ```

4. Look at the plan. This changes nothing.

   ```sh
   hal tune --auto   # hardware, the model per role, context, KV cache type, GPU layers
   ```

5. Save it. This pins the chosen models, writes the roles and the gateway settings, and
   generates the `tuned` llama.cpp mode for GGUF roles. Your config is backed up first.

   ```sh
   hal tune --auto --write   # a large GGUF file takes a while to hash
   ```

6. To choose a model yourself instead, assign it. It is pinned and wired the same way.

   ```sh
   hal models assign chat local gemma3:12b   # role, source, model
   ```

7. Restart the gateway so it reads the new config, then check.

   ```sh
   hal doctor   # the model server and role checks
   ```

To do all of this in one screen, run `hal tune` on a terminal. It lets you pick models, set the
context and see the fit before anything is written (see
[../user/models.md](../user/models.md#the-tune-screen)).

To stop `hal tune` changing the numbers of a role, set `auto = false` in its
`[models.roles.NAME]` table. Settings are described in [../user/models.md](../user/models.md).

## Symptoms and fixes

| Symptom | Fix |
| --- | --- |
| `hal tune` shows no models in the picker | Add a source with `source` (or step 2) and check that its server runs. |
| `context` refuses a number | It must be at least 2048 and at most the model's own limit or `[models] context_cap`. |
| `effort` says the model has no thinking control | Only models in the effort table can think; pick another model. |
| `hal tune` says "Nothing to tune yet" | Add a source (step 2). |
| A source shows `unreachable` | Start the server, or check the URL with `hal models list --source NAME`. |
| `no key` or `chmod 600` error | The key file must be yours with mode 0600: `chmod 600 FILE`. |
| `not loopback` when adding a URL | A server on another machine needs `--allow-remote` and an `https` URL. |
| A role shows `no model` | No local model suits it; assign one with `hal models assign`. |
| Plan says `hybrid` or `cpu` | The model is larger than your VRAM. Use a smaller model or quantisation, or accept the slower speed. |
| Plan says `does-not-fit` | The model does not fit in VRAM plus free RAM. Pick a smaller one. |
| Plan says "architecture unknown" | The server does not expose layers and heads. The context is a cautious estimate. |
| GPU not detected on WSL2 or AMD | Run `nvidia-smi` or `rocm-smi` yourself; `hal tune` prints a note saying what it could not read. |
| Requests fail with `model_unverified` | The model is not pinned. Run `hal tune --auto --write` or `hal models assign`. |
| Another program uses the GPU | Free memory is read when you run `hal tune`. Close it and run again. |
