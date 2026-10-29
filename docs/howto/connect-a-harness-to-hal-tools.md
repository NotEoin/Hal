# Connect a harness to Hal's tools

`hal-hub mcp` serves Hal's tools to any MCP client: Claude Code, Qwen Code, opencode, Codex,
Goose, Cline and others. The hub decides every call with your policy, asks you when a tool
needs approval, and writes the audit log. A harness connected this way runs on your desktop
as you, so approvals here guard against a model's mistakes, not against a hostile harness;
for an untrusted agent use `hal box`, which keeps the agent in a sandbox. The server side of
each step below is covered by the hub's tests (`test_mcp.py`, `test_mcp_stdio.py`,
`test_mcp_http.py`); the harness commands are unverified until the manual check in
04-hub's test plan is recorded.

## Steps

1. Check that the hub works and sees your packs.

   ```bash
   hal-hub doctor                                # every module ok, workers start
   hal-hub tools                                 # what a harness will be able to use
   ```

2. Choose a mode. `meta` (the default) lists three tools, `hal_find_tools`, `hal_call` and
   `hal_modules`, so the client's context stays small. `flat` lists every tool you may use,
   so the harness can apply its own per-tool rules (for Claude Code,
   `mcp__hal__calendar_add`).

3. Add the server to the harness, on stdio.

   ```bash
   claude mcp add hal -- hal-hub mcp --mode flat                 # Claude Code, flat mode
   ```

   Harnesses configured with JSON take the same command:

   ```json
   {"mcpServers": {"hal": {"command": "hal-hub", "args": ["mcp"]}}}
   ```

4. Or serve over HTTP on loopback, for a harness that prefers a URL.

   ```bash
   hal-hub mcp --transport http                  # http://127.0.0.1:8742/mcp
   hal-hub token                                 # prints the token file's path
   claude mcp add --transport http hal http://127.0.0.1:8742/mcp \
     --header "Authorization: Bearer $(cat ~/.config/hal/hub-token)"   # Claude Code
   ```

5. Answer approvals. A tool at tier `act`, `send` or `system` asks. A client that supports
   MCP forms shows a dialog with the exact call (`Allow once` or `Deny`). Otherwise, and in
   parallel, the request waits for you in any terminal:

   ```bash
   hal-hub approvals                             # pending requests
   hal-hub approve apr_5f2c9a0d1e7b4c33          # shows the exact call, asks y/N
   ```

   How approvals work, and how to allow a tool for good in `policy.toml`, is in
   [01-security](../design/01-security.md) ("Approval surfaces and protocol").

6. Raise the client's tool timeout if you use long tools (some run for up to 30 minutes).
   For Claude Code the variable is `MCP_TOOL_TIMEOUT` (the name is in the 2.1.293 binary;
   its unit and default are unverified, so check Claude Code's documentation). The hub sends
   progress notifications every 15 s to clients that ask for them, which some clients use to
   extend the timeout.

   ```bash
   MCP_TOOL_TIMEOUT=1800000 claude               # 30 minutes if the unit is milliseconds
   ```

## Symptoms and fixes

| Symptom | Fix |
| --- | --- |
| A tool you expect is not listed | Your policy denies it or `--packs`/`--tools` leave it out; `hal-hub show TOOL` gives the verdict per profile |
| `unknown_tool` for a tool that exists, in flat mode | Flat mode runs only what it listed; restart the client after installing a pack, or use meta mode |
| Calls to `act` tools end in `approval-timeout` | Nobody answered within `[approval].timeout_s`; answer with `hal-hub approve ID`, or use a client that shows MCP forms |
| `no-approver` with `--approvals elicit` | The client has no form support; use the default `--approvals auto` |
| HTTP requests get 401 | Send `Authorization: Bearer` with the content of `~/.config/hal/hub-token`; after `hal-hub token --rotate` update the client |
| HTTP requests get 403 | A browser `Origin` or a non-loopback `Host`; add the origin with `--allow-origin` only if you trust the page |
| `hal-hub mcp` exits 2 at once | A bad `config.toml`, `policy.toml`, profile or `--host`; the message names the file and key |
| The client gives up on a long tool | Raise the client's tool timeout (step 6) |
