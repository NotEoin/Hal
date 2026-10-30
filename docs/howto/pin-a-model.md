# Pin a model

This page shows how to pin a model in `models.toml` so the gateway will serve it. Ollama models
are pinned by digest, router weight files by sha256. The `hal models` command is not wired in
yet, so these steps use the Python helpers; sha256 values are never filled in by Hal's source
tree and are yours to check against the publisher.

## Steps

1. See what is pinned.

   ```sh
   hal-gateway models --json | head -c 400   # what the gateway serves now
   ```

2. Pin an Ollama model you already pulled. The helper shows the digest it records.

   ```sh
   python -c "from halgateway.models import record_ollama; help(record_ollama)"   # read the contract first
   ```

3. Pin a local weight file the same way with `add_local(store, path)`. It hashes the file and
   writes a `[[model]]` entry with `source = "local"`.

4. For a download, write the entry by hand with a full 40-hex commit and the sha256 from the
   publisher. `@main` and other moving refs are refused.

   ```toml
   [[model]]
   name = "example-q4"
   source = "hf:owner/repo@0123456789abcdef0123456789abcdef01234567"   # a full commit
   hf_file = "example-q4.gguf"
   file = "example-q4.gguf"
   format = "gguf"
   sha256 = "<64 hex characters from the publisher>"
   size = 123456789
   ```

5. Restart the gateway, then request the model.

   ```sh
   hal-gateway serve
   ```

| Symptom | Fix |
| --- | --- |
| 503 `model_unverified` | Not pinned, or the tag moved. Record it again after checking the new digest |
| Start fails on `models.toml` | The entry is invalid. The error names the model and key |
| A weight file is refused at load | The sha256 does not match. Re-download, or re-pin only if you trust the new file |
| Download refused | The source is not pinned to a 40-hex commit |
