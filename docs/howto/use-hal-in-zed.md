# Use Hal in Zed or another ACP editor

This page shows how to run the boxed Hal agent from an editor with `hal acp`. Unverified against
a real editor: the proxy is covered by tests with a scripted client, and a docker run is still
to be measured.

## Steps

1. Build the agent image once.

   ```sh
   hal code --build   # creates hal-agent:local from this checkout
   ```

2. Point the editor at the command. The editor starts it and speaks JSON-RPC on stdin and stdout.

   ```json
   {"agent_servers": {"Hal": {"command": "hal", "args": ["acp"]}}}
   ```

3. Open a project. The agent works on an isolated copy of it inside a box; your repository is not
   mounted. When the session ends, `hal` prints the isolated session id.

   ```sh
   hal code diff SID     # what the agent changed, with risk flags
   hal code accept SID   # apply it to the real repository
   ```

4. Answer approvals in the editor. A broker approval arrives as a permission request whose title
   comes from Hal, not from the agent. A request whose title starts with `[box]` came from the
   agent itself and never grants anything. You can also answer from another terminal.

   ```sh
   hal approvals          # list pending requests
   hal approve APR_ID     # show the full call, then confirm
   ```

5. For a channel that is not an editor (voice, Telegram), use the assistant toolset.

   ```sh
   hal acp --channel voice --toolset assistant   # a box with /scratch only, never your vault
   ```

## When it does not work

| Symptom | Fix |
| --- | --- |
| `image hal-agent:local not found` | Run step 1 |
| Exit 3 with a lockdown message | Raise nothing from a tool; edit `[sandbox].lockdown` yourself, or install runsc for `strict` |
| The editor shows "cwd is outside the project" | Open a folder inside the project that `hal acp` was started in |
| File and terminal requests fail | By design: the agent never uses the editor's `fs` or `terminal`; it works in the box |
| MCP servers from the editor are ignored | By design: host tools reach the box only through the broker |
