# Check the agent setup

This page shows how to confirm that `hal-agent` can reach its model and its broker. Unverified
against a real gateway: the steps are covered by tests with fake servers only.

## Steps

1. Run the doctor with the gateway socket or URL you use.

   ```sh
   hal-agent doctor --endpoint unix:///run/user/1000/hal/gateway.sock --model hal-code   # one line per check
   ```

2. Read the first column: `ok`, `warn` or `fail`. Fix every `fail`; a `warn` still works.

3. Look at the resolved profile of the model.

   ```sh
   hal-agent profiles show hal-code   # each value ends with the file that set it
   ```

4. Run a one-line task and read the exit code.

   ```sh
   hal-agent -p "say done, then call finish" --json; echo $?   # 0 means finished
   ```

5. Find the transcript of that run.

   ```sh
   hal-agent sessions list   # newest first; then: hal-agent sessions show ID
   ```

## When it does not work

| Symptom | Fix |
| --- | --- |
| `fail gateway ... unreachable` | Start the gateway, or pass `--endpoint` (URL or unix socket path) |
| `warn gateway ... model is not listed` | Use a model or alias the gateway lists (`GET /v1/models`) |
| `fail config` naming `agent.json` | The file must match schema 1 exactly: every key present, none unknown |
| `warn broker not configured` | Pass `--broker SOCKET` or set `HAL_AGENT_BROKER`; without it there are no host tools |
| `warn rg` | Install ripgrep; until then `grep` uses a slower Python walk |
| `fail bash` | Install bash; the `run` tool needs it |
| Exit 4 from `-p` | A limit ended the task; raise `--max-tool-calls` or `--max-turns`, or give a smaller task |
| Exit 3 from `-p` | The model server did not answer after retries; run `doctor` |
