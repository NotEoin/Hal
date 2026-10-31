# Move from hal-ecosystem

`hal migrate ecosystem` moves a machine from the old `hal-ecosystem` layout to this one. It is a
dry run unless you pass `--yes`, journals every change, and never writes inside the old folder,
the vault or any hal-employment folder.

1. Make sure no old Hal is running.

   ```
   pgrep -af halcore   # prints nothing
   ```

2. Install the new Hal first, so `~/.local/share/hal/venv/bin/hal` exists.

   ```
   scripts/install.sh --dry-run   # shows what it would do
   ```

3. Print the plan. Nothing is changed.

   ```
   hal migrate ecosystem --from ~/src/hal-ecosystem   # dry run
   ```

4. Apply it. The journal path is printed.

   ```
   hal migrate ecosystem --yes   # appends [compat] and [jobs] to config.toml, relinks ~/.local/bin
   ```

5. Run the printed manual steps yourself: `hal units install`, `hal packs add --local PATH`, and
   the vault `.mcp.json` edit (it needs your approval, so the migrator only reports it).

6. To go back, undo the journal.

   ```
   hal migrate undo ~/.local/state/hal/migrate/<UTC>/journal.json   # restores config.toml and links
   ```

| Symptom | Fix |
| --- | --- |
| `link ...: .../venv/bin/hal is missing` | Run the installer (step 2), then redo step 3 |
| `... is a regular file; move it away` | Move or delete that file yourself; the migrator never replaces real files |
| Config step says `add ... to [compat].hal_conf` | `[compat]` already exists; add the path by hand |
| `refusing to write inside the source folder` | Point `--from` at the old tree only; your paths overlap it |
| Undo says `cannot read journal` | Pass the `journal.json` file, not its folder |

Verified by `packages/hal-core/tests/test_migrate.py` on a synthetic layout. The real cutover on
your machine is unverified: run step 3 and read the plan first.
