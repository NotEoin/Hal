# Run a vLLM server under Hal

This page hands an OpenAI-compatible server, such as vLLM, to the gateway: the gateway starts
it at the first request, waits until it is ready, restarts it after a crash, frees the GPU for
it and stops it again. The rules are in [the gateway guide](../user/gateway.md#a-server-the-gateway-runs).
The tests run against a fake server (`packages/hal-gateway/tests/test_backends_managed.py`).

## Steps

1. Write a launch script that takes its address from `HOST` and `PORT` and runs the server in
   the foreground. The server gets only `PATH`, `HOME`, `LANG`, `LC_ALL`,
   `CUDA_VISIBLE_DEVICES`, your `env` table and the key, so the script sets everything else
   itself (virtualenv, caches).

   ```sh
   cat > ~/bin/start-vllm.sh <<'EOF'
   #!/usr/bin/env bash
   set -euo pipefail
   export HF_HOME=/data/hf                                  # caches the server needs
   exec /opt/vllm/venv/bin/vllm serve /models/Qwen3-8B \
     --served-model-name qwen3-8b --host "$HOST" --port "$PORT"   # vLLM reads VLLM_API_KEY itself
   EOF
   chmod 755 ~/bin/start-vllm.sh                            # the gateway runs it directly, no shell
   ```

2. Add the backend to `~/.config/hal/config.toml`. The name (`qwen` here) is yours.

   ```toml
   [gateway.backends.qwen]
   type = "managed-openai"
   command = ["/home/me/bin/start-vllm.sh"]   # absolute path, then any arguments
   port = 18020                               # passed as PORT; HOST is 127.0.0.1
   key_env = "VLLM_API_KEY"                   # gets the generated key
   served_models = ["qwen3-8b"]               # what --served-model-name says
   startup_timeout_s = 300.0                  # a cold start of a large model takes minutes
   idle_stop = false                          # keep it through the idle unload
   ```

3. Check the config and restart the gateway.

   ```sh
   hal-gateway check-config                   # prints ok: N backend(s), ...
   systemctl --user restart hal-gateway.service
   ```

4. Start the server and watch it come up.

   ```sh
   hal-gateway backend start qwen                 # returns when ready, or names the log
   tail -f ~/.local/state/hal/gateway/qwen.log    # the server's own output
   hal-gateway status                             # qwen  managed-openai  running
   ```

5. Give a role the model. Add a source with the same name as the backend, then assign it.

   ```toml
   [models.sources.qwen]
   kind = "openai"
   url = "http://127.0.0.1:18020/v1"
   api_key_file = "~/.local/state/hal/gateway/qwen.key"   # the key the gateway generated
   ```

   ```sh
   hal models assign writer qwen qwen3-8b     # the server must be running: assign lists its models
   ```

6. Stop it when you want the card back. The next request does not start it again until you do.

   ```sh
   hal-gateway backend stop qwen              # stops the server and every process it started
   hal-gateway backend start qwen             # or restart
   ```

| Symptom | Fix |
| --- | --- |
| `check-config` says `command` must be an absolute path | Give the full path of the script; the gateway does no `PATH` lookup |
| `check-config` says an `env` name looks like a secret | Keep secrets out of the config: read them from a 0600 file in the script, or use `key_env` for the API key |
| `exited during startup; see .../qwen.log` | Read the log. Often a variable the server needs is missing; set it in the script or in `env` |
| `not ready after 300 s` | Raise `startup_timeout_s`, and check that `served_models` matches the name the server lists in `/v1/models` |
| Requests get 503 `was stopped` | Run `hal-gateway backend start qwen`; a stop holds until a start |
| 503 `request(s) running and holds the GPU` | The router or another server is answering a request; retry when it ends |
| The server answers 401 | The script does not pass the key; vLLM reads `VLLM_API_KEY`, other servers need a flag |
| `hal models assign` says the model is not available | Start the server first: assign reads the model list from it |
| The GPU is still full after a stop | A process left the server's process group (for example with `setsid`); find it with `nvidia-smi` |
