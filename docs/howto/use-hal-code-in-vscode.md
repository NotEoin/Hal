# Use Hal code in VS Code, VSCodium and Cursor

This page shows how to build and load the Hal extension from `ide/vscode/` and connect it to a
running `hal code`. The extension is never published; you build it yourself. Unverified in a real
VS Code: it compiles with `npx tsc -p ide/vscode` and its message types are covered by
`packages/hal-core/tests/test_ide_vscode_source.py`.

## How it is kept safe

- `hal code` creates a socket `$XDG_RUNTIME_DIR/hal/ide/<session>.sock` (directory 0700, socket
  0600) on the host. The box never sees it.
- Only your user can connect (peer uid check). Messages are one JSON line, at most 1 MiB, with a
  read timeout.
- The terminal accepts six message types and nothing else: `hello`, `add_selection`, `context`,
  `diagnostics`, `show_changes`, `accept`. Paths must stay inside the workspace. Nothing the
  editor sends is executed; `accept` only prints a notice and you confirm with `/accept`.
- The agent sees editor input only as prompt text.

## Steps

1. Build the extension (needs Node.js; `npm ci` fetches the pinned compiler from the npm registry).

   ```sh
   cd ide/vscode
   npm ci --ignore-scripts   # installs typescript and the type packages only
   npx tsc -p .              # writes out/extension.js
   ```

2. Load it without publishing: in VS Code run "Developer: Install Extension from Location..." and
   pick `ide/vscode`, or start a development host.

   ```sh
   code --extensionDevelopmentPath "$PWD"   # from ide/vscode; use codium or cursor for the forks
   ```

3. Run "Hal: Open in terminal". It starts `hal code` for the workspace folder in the integrated
   terminal.

4. Select lines and run "Hal: Add selection to context". The terminal prints `ide: added
   @path#L10-20` and the lines join your next prompt.

5. Optional: set `hal.shareActiveFile` to true, then turn it on in the terminal with `/ide on`.
   Each prompt then names the open file and selection. Both switches default to off.

6. After the agent edits, run "Hal: Show changes" to open one diff per file (repository against
   the isolated copy). Run "Hal: Accept changes", then type `/accept` in the terminal and confirm
   there.

   ```text
   /ide        # connection state and ambient sharing
   /accept     # shows the diff report and asks before applying
   ```

## When it does not work

| Symptom | Fix |
| --- | --- |
| "No Hal session for this folder" | Run `hal code` from the same folder; the lock file names the folder, symlinks are resolved |
| `tsc` is not found | Run `npm ci --ignore-scripts` in `ide/vscode` first |
| The terminal says "channel not opened" | The runtime directory is missing or not private; set `HAL_RUNTIME_DIR` to a directory you own (mode 0700) |
| "Show changes" opens nothing | The agent has not changed anything yet; or the session has no isolated copy (`--direct`) |
| Remote or WSL window | Install the extension in the remote side (it is a workspace extension) and run `hal code` there |
