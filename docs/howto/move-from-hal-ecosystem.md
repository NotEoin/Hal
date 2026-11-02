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

6. Point the vault's MCP client (the `hal-hub` entry of the vault `.mcp.json`) at the new hub. The
   command is `hal-hub mcp`, run from the Hal venv, which sees the registered packs.

   ```
   ~/.local/share/hal/venv/bin/hal-hub mcp   # command for .mcp.json; args: ["mcp"] on the hal-hub binary
   ```

7. Check where the units run from. `hal setup --dry-run` prints each unit's `ExecStart`, and
   `hal doctor --only units` warns when a unit renders outside the registered packs.

   ```
   hal setup --dry-run --replace   # plan only; --replace also drops hal-ecosystem module_paths entries
   hal doctor --only units         # warns about units outside the packs
   ```

8. To go back, undo the journal.

   ```
   hal migrate undo ~/.local/state/hal/migrate/<UTC>/journal.json   # restores config.toml and links
   ```

## Where each old unit went

The unit names did not change. Each file is now rendered from a pack (or core Hal) by
`hal units install`, not from the old tree, so stop the old unit first and let the new one replace it.
`hal migrate ecosystem` prints one line per row.

| Old unit | New form |
| --- | --- |
| `hal-console.service` | core Hal |
| `hal-clock.service` | pack `organiser` |
| `hal-calendar-reminders.service` | pack `organiser` |
| `hal-tasks-reminders.service` | pack `organiser` |
| `hal-routines.service` | pack `organiser` |
| `hal-briefing@.service`, `hal-briefing-morning\|midday\|evening.timer` | pack `organiser` |
| `hal-rgb-server.service` | pack `desktop` |
| `hal-telegram.service` | pack `comms` |
| `hal-employment.service` | pack `jobs` |
| `hal-habits-nudge`, `hal-ledger-remind` (service and timer) | pack `life` |
| `hal-memory-idle.service` | pack `memory` |
| `hal-voice.service`, `hal-tts.service` | pack `voice` |
| `hal-brief.service`, `hal-brief.timer` (archive) | retired: use `hal-briefing@.service` and its timers |

New in this layout: `hal-gateway.service` and `hal-jobs.service`/`.timer`. `hal setup` writes them.

| Symptom | Fix |
| --- | --- |
| `link ...: .../venv/bin/hal is missing` | Run the installer (step 2), then redo step 3 |
| `... is a regular file; move it away` | Move or delete that file yourself; the migrator never replaces real files |
| Config step says `add ... to [compat].hal_conf` | `[compat]` already exists; add the path by hand |
| `refusing to write inside the source folder` | Point `--from` at the old tree only; your paths overlap it |
| Undo says `cannot read journal` | Pass the `journal.json` file, not its folder |
| `hal doctor` warns `render from outside the registered packs` | Run `hal setup --replace`; it drops the `hal-ecosystem` entries of `[hub].module_paths` and rewrites the units from the packs |
| `hal setup` says `unit(s) belong to another install` | Stop the old units and back them up, then rerun `hal setup --replace` |

Verified by `packages/hal-core/tests/test_migrate.py` on a synthetic layout. The real cutover on
your machine is unverified: run step 3 and read the plan first.
