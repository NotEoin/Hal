# Use Hal code in Zed (and other ACP editors)

This page shows how to run the boxed Hal agent from Zed with `hal acp`. The same command serves
JetBrains, Neovim and Emacs; see the other `use-hal-code-in-*` pages and `hal ide setup`.
Unverified against a real editor: the proxy is covered by tests with a scripted client
(`packages/hal-core/tests/test_acp_proxy.py`, `test_acp_hostfs.py`). Check by running step 3.

## What the editor gets

- The agent works on an isolated copy of your project inside a box. Your repository is not
  mounted and changes reach it only through `hal code accept`.
- Edits arrive as ACP `diff` content with your real file paths, so Zed shows them as native diffs.
- The agent's file reads and writes (`fs/read_text_file`, `fs/write_text_file`) are answered by
  Hal from the isolated copy, never from the repository. Terminal requests are refused: the box
  has its own shell.
- `session/load` resumes a saved session.

## Steps

1. Build the agent image once.

   ```sh
   hal code --build   # creates hal-agent:local from this checkout
   ```

2. Print the Zed configuration, then merge it into Zed's `settings.json`.

   ```sh
   hal ide setup zed                          # prints the snippet, writes nothing
   hal ide setup zed --write ./zed-snippet.json   # writes only this file; --force keeps a .bak
   ```

   ```json
   {"agent_servers": {"Hal": {"command": "hal", "args": ["acp"]}}}
   ```

3. Open a project in Zed and start a "Hal" thread from the agent panel. Ask for a small edit and
   check that the diff appears in the panel.

4. Review what the agent changed, then apply it from a terminal.

   ```sh
   hal code diff SID     # what the agent changed, with risk flags
   hal code accept SID   # apply it to the real repository
   ```

5. Answer approvals in the editor. A broker approval arrives as a permission request whose title
   comes from Hal, not from the agent. A request whose title starts with `[box]` came from the
   agent itself and never grants anything. You can also answer from another terminal.

   ```sh
   hal approvals          # list pending requests
   hal approve APR_ID     # show the full call, then confirm
   ```

6. For a channel that is not an editor (voice, Telegram), use the assistant toolset.

   ```sh
   hal acp --channel voice --toolset assistant   # a box with /scratch only, never your vault
   ```

## When it does not work

| Symptom | Fix |
| --- | --- |
| `image hal-agent:local not found` | Run step 1 |
| Exit 3 with a lockdown message | Raise nothing from a tool; edit `[sandbox].lockdown` yourself, or install runsc for `strict` |
| The editor shows "cwd is outside the project" | Open a folder inside the project that `hal acp` was started in |
| A terminal request fails | By design: the agent runs commands in the box, not in the editor |
| A file request says "outside the project" | The path must be below the project or `/work`; `..` and symlinks are refused |
| MCP servers from the editor are ignored | By design: host tools reach the box only through the broker |
| `hal ide setup` says the file exists | Pass `--force`; the old file is kept as `NAME.bak` |
