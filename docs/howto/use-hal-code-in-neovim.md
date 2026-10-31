# Use Hal code in Neovim (and Emacs)

This page shows how to connect CodeCompanion or avante.nvim to `hal acp`, and gives the Emacs
`agent-shell` line. Unverified against the plugins' current releases: plugin option names change,
so the command line (`hal acp`) is the stable part. Check by running step 3.

## Steps

1. Build the agent image once.

   ```sh
   hal code --build   # creates hal-agent:local from this checkout
   ```

2. Print the snippet for your editor and paste the block for your plugin into its `setup()` call.

   ```sh
   hal ide setup neovim   # CodeCompanion and avante.nvim blocks
   hal ide setup emacs    # agent-shell
   ```

3. Open a project, start a chat with the "hal" adapter and ask for a small edit. Edits go to an
   isolated copy; the plugin shows them as diffs where it supports ACP diff content.

4. Apply the result when you are happy.

   ```sh
   hal code diff SID     # what changed, with risk flags
   hal code accept SID   # apply it to the real repository
   ```

## When it does not work

| Symptom | Fix |
| --- | --- |
| The adapter is unknown | Your plugin version names ACP adapters differently; keep `commands = { default = { "hal", "acp" } }` and adapt the wrapper |
| The plugin cannot start `hal` | Start Neovim from a shell where `hal` is on the PATH, or put the full path in the snippet |
| Permission prompts never appear | Answer from a terminal: `hal approvals`, then `hal approve APR_ID` |
| Edits do not reach the repository | By design: run `hal code accept SID` |
