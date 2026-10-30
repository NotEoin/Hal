# Use the agent from an editor over ACP

This page shows how to run `hal-agent` as an Agent Client Protocol server for an editor, and how
to watch its edits with review mode. Unverified against a real editor: the protocol is covered by
tests with a scripted client only.

## Steps

1. Check that the agent reaches its model.

   ```sh
   hal-agent doctor --endpoint unix:///run/user/1000/hal/gateway.sock --model hal-code   # every line ok or warn
   ```

2. Point the editor at the command. The editor starts it and speaks JSON-RPC on stdin and stdout.

   ```sh
   hal-agent --acp --endpoint unix:///run/user/1000/hal/gateway.sock --model hal-code   # the command to configure
   ```

3. Open a session in the editor. The agent works in the directory the editor sends as `cwd`.

4. Switch to review mode when you want to approve each edit and command. The editor sends
   `session/set_mode` with `modeId` `review`; `auto` is the default.

   ```text
   allow_once / allow_always / reject_once / reject_always   # the four choices the editor shows
   ```

5. Cancel a running turn from the editor. The agent stops the model call and any running command
   and answers the prompt with stop reason `cancelled`.

6. Reload an earlier session from the editor's history. The agent rebuilds it from its store, or
   from the records the host sends when `hal` runs it in a box.

## When it does not work

| Symptom | Fix |
| --- | --- |
| The editor shows no agent output | Run the step 2 command in a terminal; any text on stdout other than JSON lines is a bug, report it |
| `fail gateway` in the doctor | Start the gateway or fix `--endpoint` (see "Check the agent setup") |
| Every edit asks for approval | The session is in review mode; send `session/set_mode` with `auto` |
| Review mode does not stop a bad command | Review mode is a way to watch the agent, not a boundary; the box and the broker are the boundaries |
| `session/load` returns an empty session | The id is unknown to the store; list ids with `hal-agent sessions list --all` |
| Host tools are missing | Pass `--broker SOCKET` or set `HAL_AGENT_BROKER`; without it the agent has file and shell tools only |
| MCP servers from the editor are ignored | Only stdio servers are accepted; HTTP and SSE servers are refused (the box has no network) |
