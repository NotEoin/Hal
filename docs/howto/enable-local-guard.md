# Enable the local guard

The guard scans what untrusted tools return (web pages, mail, files) for prompt injection with
Meta's Prompt Guard 2, on your CPU, in a box with no network. It is off by default. The design
is in [01-security](../design/01-security.md) (REQ-SEC-35, 36, 38).

## Steps

1. Accept the Prompt Guard 2 licence on Hugging Face and download `model.safetensors` and
   `tokenizer.json` of `prompt-guard-2-86m` yourself. Hal never sees your token.

   ```sh
   mkdir -p ~/.local/share/hal/guard/prompt-guard-2-86m   # default weights folder; HAL_DATA_DIR moves it
   ```

   Copy the two files into that folder. A folder with `.bin`, `.pt` or `.pkl` files is refused,
   because loading one can run code.

2. Build the worker image and record the worker command. Docker is needed, and the build
   downloads about 1 GB of hash-pinned wheels once.

   ```sh
   hal guard install --runtime runsc   # asks for a typed y; use --runtime runc when runsc is not installed
   ```

3. Start the relay unit, then measure the cost on this machine.

   ```sh
   systemctl --user start hal-guard    # the relay owns the guard socket and the worker
   hal guard bench                     # p50 and p95 per 512-token window and per tool result
   ```

4. Turn it on in `policy.toml`, then check it.

   ```sh
   hal policy edit                     # add:  [guard]  enabled = true
   hal guard status                    # backend, weights hash, relay and latency
   ```

With the guard on, a flagged result asks you (`on_flag = "ask"`) or is refused (`"deny"`). If the
guard cannot run, results count as flagged and `hal-hub mcp` will not start.

## Symptoms and fixes

| Symptom | Fix |
| --- | --- |
| `hal guard: ... is missing` | Put `model.safetensors` and `tokenizer.json` in the weights folder |
| `hal guard: ... holds pickle files` | Remove the `.bin`, `.pt`, `.pkl` files; only safetensors are loaded |
| `hal guard install needs a terminal` | Run it in a terminal; the build needs a typed `y` |
| `relay: NOT reachable` in `status` | `systemctl --user start hal-guard`, then `hal guard status` |
| `hal guard bench` says a scan failed | The worker cannot load the weights; check the two files are whole, then run `hal guard install` again |
| Scans are slow | Close other CPU jobs, raise `[guard].threads`, or set `model = "prompt-guard-2-22m"` and install its weights |
