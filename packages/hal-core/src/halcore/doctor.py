"""`hal doctor` and `hal status`: one read-only health report for the whole install.

It gathers the sandbox checks (docker, runsc, box images), the runtime directory permissions,
the model server and the security lines. It never starts or stops anything; the only repair is
`--fix`, which narrows the runtime directory to 0700 and nothing else. Every
failing line carries its fix, because a person reading a red line needs the next command.
Design decisions:

- A probe that raises becomes a `fail` or `warn` line instead of an exception, so one broken
  part never hides the rest of the report.
- The runtime directory is inspected with `stat`, not `halkit.paths.runtime_dir()`, because that
  function raises on the very condition (a directory that is not 0700) we want to report.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from dataclasses import asdict

from halkit import llm, paths
from halsandbox import doctor as sandbox_doctor
from halsandbox.doctor import FAIL, OK, WARN, Check

from . import security, units

PROBE_TIMEOUT_S = 3.0
PRIVATE_MODE = 0o700


def runtime_dir_check() -> Check:
    """Reports the runtime directory, and the `chmod` fix when it is not owned by you or not 0700."""
    try:
        path = paths._runtime_candidate()  # noqa: SLF001 - the public accessor raises on the case we report
    except paths.RuntimeDirMissing as exc:
        return Check("runtime dir", FAIL, str(exc))
    if not path.exists():
        return Check("runtime dir", OK, f"{path} (created on first use)")
    info = path.stat()
    if info.st_uid != os.getuid():
        return Check("runtime dir", FAIL, f"{path} is owned by uid {info.st_uid}; set HAL_RUNTIME_DIR to a directory you own")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        return Check("runtime dir", FAIL, f"{path} has mode {mode:04o}, it must be {PRIVATE_MODE:04o}: run `chmod 700 {path}`")
    return Check("runtime dir", OK, f"{path} (mode {mode:04o})")


def docker_check() -> Check:
    """Reports whether the `docker` command is on the PATH."""
    found = shutil.which("docker")
    if found:
        return Check("docker", OK, found)
    return Check("docker", FAIL, "docker is not on the PATH; install Docker Engine or run `hal install`")


def model_check() -> Check:
    """Reports whether the model gateway answers, without loading a model."""
    try:
        aliases = llm.ping(timeout=PROBE_TIMEOUT_S)
    except (llm.GatewayError, llm.ModelUnavailable, OSError) as exc:
        return Check("model server", WARN, f"not reachable ({exc}); start llama-server or Ollama, or hal-gateway")
    return Check("model server", OK, f"{len(aliases)} model(s): {', '.join(aliases[:4])}")


def units_check() -> Check:
    """Warns when a unit would render outside the registered packs or run a program that is missing."""
    outside = units.outside_packs()
    missing = units.missing_exec_paths()
    if not outside and not missing:
        return Check("units", OK, "every unit renders from core Hal or a registered pack")
    parts = []
    if outside:
        parts.append(f"render from outside the registered packs: {', '.join(outside)}")
    if missing:
        parts.append(f"run a missing program: {', '.join(missing)}")
    return Check("units", WARN, "; ".join(parts) + "; run `hal setup --dry-run` to see them, `hal setup --replace` to fix")


def security_checks() -> list[Check]:
    """The lockdown and classifier lines as checks."""
    report = security.collect(check_runtime=False)
    out = [Check("security", FAIL, p) for p in report.problems]
    out += [Check("security", WARN, w) for w in report.warnings]
    out += [Check("security", OK, line) for line in report.lines]
    return out


def collect() -> list[Check]:
    """Every check, in the order they are shown; the runtime directory comes first."""
    checks = [runtime_dir_check(), docker_check()]
    try:
        checks += sandbox_doctor.run_checks()
    except Exception as exc:  # noqa: BLE001 - a broken probe must not hide the other lines
        checks.append(Check("sandbox", FAIL, f"probe failed: {exc}"))
    checks.append(model_check())
    try:
        checks.append(units_check())
    except Exception as exc:  # noqa: BLE001 - a broken probe must not hide the other lines
        checks.append(Check("units", WARN, f"probe failed: {exc}"))
    try:
        checks += security_checks()
    except Exception as exc:  # noqa: BLE001 - same reason
        checks.append(Check("security", FAIL, f"probe failed: {exc}"))
    return checks


def select(checks: list[Check], only: list[str]) -> list[Check]:
    """The checks whose name starts with one of `only` (case-insensitive); all when `only` is empty."""
    if not only:
        return checks
    wanted = tuple(o.lower() for o in only)
    return [c for c in checks if c.name.lower().startswith(wanted)]


def fix_runtime_dir() -> str:
    """Sets your own runtime directory to 0700 and returns what happened; refuses any other owner."""
    try:
        path = paths._runtime_candidate()  # noqa: SLF001 - same reason as `runtime_dir_check`
    except paths.RuntimeDirMissing as exc:
        return f"runtime dir: cannot fix: {exc}"
    if not path.exists():
        return f"runtime dir: {path} does not exist yet; nothing to fix"
    info = path.stat()
    if info.st_uid != os.getuid():
        return f"runtime dir: {path} is owned by uid {info.st_uid}; not touching it"
    if stat.S_IMODE(info.st_mode) == PRIVATE_MODE:
        return f"runtime dir: {path} is already {PRIVATE_MODE:04o}"
    path.chmod(PRIVATE_MODE)
    return f"runtime dir: set {path} to {PRIVATE_MODE:04o}"


def render(checks: list[Check]) -> str:
    """The checks as aligned text, problems marked so they stand out."""
    width = max(len(c.name) for c in checks)
    marks = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    return "\n".join(f"[{marks[c.status]}] {c.name:<{width}}  {c.detail}" for c in checks)


def main(argv: list[str]) -> int:
    """Runs `hal doctor` (or `hal status`); exits 1 when any line failed."""
    parser = argparse.ArgumentParser(prog="hal doctor", description="check the Hal install (read-only unless --fix)")
    parser.add_argument("--json", action="store_true", help="print the checks as JSON")
    parser.add_argument("--only", action="append", default=[], metavar="NAME", help="show only checks starting with NAME (repeatable)")
    parser.add_argument("--fix", action="store_true", help="set the runtime directory to 0700 if it is yours, then re-check")
    args = parser.parse_args(argv)
    if args.fix:
        print(fix_runtime_dir())
    checks = select(collect(), args.only)
    if not checks:
        print(f"hal doctor: no check matches {', '.join(args.only)}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([asdict(c) for c in checks]))
    else:
        print(render(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
