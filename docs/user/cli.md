# The hal command

`hal --help` lists every command. A bare `hal` runs `hal code` in a project folder and `hal chat`
elsewhere. Exit codes: 0 ok, 1 failed, 2 usage error, 3 prerequisite missing, 130 interrupted.

## hal doctor

`hal doctor` (also `hal status`) prints one read-only report and starts nothing. It checks the
runtime directory, docker, runsc, the box images, the model server and the lockdown and
paranoia lines. Add `--json` for machine output. It exits 1 when a line says FAIL.

```
[FAIL] runtime dir  /run/user/1000/hal has mode 0755, it must be 0700: run `chmod 700 /run/user/1000/hal`
[warn] runtime      runc shares the host kernel; install gVisor (docs/howto/install-gvisor.md)
[ok  ] model server 2 model(s): hal-fast, hal-code
```

Verified by `packages/hal-core/tests/test_doctor.py`.

## hal migrate

`hal migrate ecosystem [--from DIR] [--yes]` plans the move from the old layout and applies it
only with `--yes`. `hal migrate undo JOURNAL` reverts it. See
[move-from-hal-ecosystem](../howto/move-from-hal-ecosystem.md).

## Installing

`scripts/install.sh` creates `~/.local/share/hal/venv` with uv, installs the packages of this
checkout editable and links `~/.local/bin/hal`. It never uses sudo. `--dry-run` prints every
step and changes nothing; a second run changes nothing. Options: `--core-only`, `--no-link`.
Then run `hal doctor`. Verified by `packages/hal-core/tests/test_install_script.py`.
