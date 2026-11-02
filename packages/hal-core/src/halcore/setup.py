"""`hal setup`: one idempotent command that finishes an install from a checkout.

It syncs the workspace, installs the packs found next to the checkout (hal-tools, hal-memory,
hal-voice) and the employment helper (found through `[jobs].jobhunt_root`, then the siblings) when it ships a
Hal module, installs or detects gpu-light,
builds the box image, writes the hal-* systemd user units and runs the doctor.

Design decisions:

- Every step is planned first, so `--dry-run` prints exactly what a real run would do and
  changes nothing (the doctor is read-only, so it runs in both).
- Units are written but never enabled or started unless `--enable` is given; a setup run must
  not change what starts at login by surprise.
- Unit files of another install (the old hal-ecosystem shares the unit record) are never
  replaced, stopped or deleted unless `--replace` is given: the units step fails instead and
  names them, in a dry run too.
- The config part of `hal migrate ecosystem` runs only when an older folder is named (`--migrate-from`
  or `[migrate].source`); there is no default location.
- A missing sibling or a missing gpu-light skips its step. Hal works without both.
- A failed step is reported and the rest still run, then the exit status is 1, so one broken
  step does not hide the others.
- Existing pieces are reused: `packs.add_local`, `units.install`, the box image build and
  `doctor`. Nothing here duplicates them.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from halkit import paths, proc

from . import config, doctor, gpulight, migrate, packs, units

log = logging.getLogger(__name__)

SYNC_TIMEOUT_S = 900.0  # a cold `uv sync --all-packages` downloads the whole dependency set
TOOL_INSTALL_TIMEOUT_S = 600.0
IMAGE_TIMEOUT_S = 3600.0  # building the sandbox image pulls a base image and installs packages
PACK_SIBLINGS = ("hal-tools", "hal-memory", "hal-voice")
EMPLOYMENT_SIBLINGS = ("employment-helper", "hal-employment")
GPU_LIGHT_SIBLING = "gpu-light"
PRIVATE_MODE = 0o700  # what the doctor demands of the sandbox state folder
MODULE_FILE = "hal-module.toml"
EXIT_OK, EXIT_FAILED = 0, 1

STEP_ERRORS = (packs.PackError, migrate.MigrateError, subprocess.CalledProcessError, proc.ProcTimeout, OSError)


@dataclass
class Step:
    """One setup step: `skip` is the reason it needs no work, `blocked` why it must not run, else `run` does it."""

    name: str
    detail: str
    run: Callable[[], str] | None = None
    skip: str = ""
    blocked: str = ""


def search_dirs(extra: list[Path]) -> list[Path]:
    """The folders searched for sibling repos: `--from` folders, else the checkout's parent and grandparent."""
    return [p.expanduser() for p in extra] or [config.ROOT.parent, config.ROOT.parent.parent]


def find_sibling(names: tuple[str, ...], dirs: list[Path]) -> Path | None:
    """The first folder called one of `names` inside `dirs`, or None."""
    for base in dirs:
        for name in names:
            if (base / name / "pyproject.toml").is_file():
                return base / name
    return None


def pack_dirs(repo: Path) -> list[Path]:
    """The installable pack folders of a sibling: its `packs/*` children, else the repo itself."""
    children = sorted(p for p in (repo / "packs").glob("*") if (p / "pyproject.toml").is_file())
    return children or [repo]


def has_module(repo: Path) -> bool:
    """True when `repo` carries a `hal-module.toml` at its top or one folder down."""
    return (repo / MODULE_FILE).is_file() or any(repo.glob(f"*/{MODULE_FILE}"))


def _pack_name(path: Path) -> str:
    import tomllib

    project = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
    return str(project.get("name", path.name)).removeprefix("hal-")


def _run(argv: list[str], timeout: float, cwd: Path | None = None) -> None:
    proc.run(argv, timeout=timeout, cwd=cwd)


def gateway_bin_dir() -> Path | None:
    """The folder that holds the `hal-gateway` executable: the checkout venv, else the PATH."""
    venv = config.ROOT / ".venv" / "bin"
    if (venv / "hal-gateway").exists():
        return venv
    found = shutil.which("hal-gateway")
    return Path(found).parent if found else None


def plan(dirs: list[Path], *, enable: bool, image: bool, replace: bool = False,
         migrate_from: Path | None = None) -> list[Step]:
    """The steps a run would take, in order, each with the reason it can be skipped."""
    steps = [Step("workspace", "uv sync --all-packages", lambda: _sync())]
    have = packs.read_record().get("packs", {})
    for name in PACK_SIBLINGS:
        repo = find_sibling((name,), dirs)
        if repo is None:
            steps.append(Step(f"pack {name}", "no sibling checkout found", skip="not found, skipped"))
            continue
        for path in pack_dirs(repo):
            pack = _pack_name(path)
            if pack in have:
                steps.append(Step(f"pack {pack}", str(path), skip="already installed"))
            else:
                steps.append(Step(f"pack {pack}", f"hal packs add --local {path}", lambda p=path: packs.add_local(p)))
    steps.append(_employment_step(dirs, have))
    steps.append(_gpu_light_step(dirs))
    steps.append(_config_step(replace, migrate_from))
    steps.append(_sandbox_step())
    steps.append(_image_step() if image else Step("box image", "--no-image", skip="skipped by option"))
    steps.append(_units_step(enable, replace))
    return steps


def migrate_source(given: Path | None) -> Path | None:
    """The older hal-ecosystem folder: `--migrate-from`, else `[migrate].source`, else None."""
    if given is not None:
        return given.expanduser()
    configured = config.load().get("migrate", {}).get("source")
    return Path(configured).expanduser() if configured else None


def _config_step(replace: bool, source: Path | None = None) -> Step:
    """The config part of `hal migrate ecosystem`; entries that point into the old tree need `--replace`."""
    source = migrate_source(source)
    cfg = paths.config_file()
    if source is None:
        # A fresh install must not get the old layout's tables; the person names the old folder to opt in
        return Step("config migrate", str(cfg), skip="no older hal-ecosystem folder given (--migrate-from PATH or [migrate].source)")
    if not source.is_dir():
        return Step("config migrate", str(cfg), skip=f"no hal-ecosystem install at {source}")
    try:
        plan = migrate.config_plan(source, cfg)
    except migrate.MigrateError as exc:
        return Step("config migrate", str(exc), blocked=str(exc))
    if not plan.steps:
        return Step("config migrate", str(cfg), skip="nothing to add or rewrite")
    detail = "; ".join(s.detail for s in plan.steps)
    # `apply` skips manual steps, so a real run would print only the journal and hide them
    by_hand = "; ".join(s.detail for s in plan.steps if s.kind == "manual")
    plan.steps = [s for s in plan.steps if s.kind != "manual"]
    if not plan.steps:
        return Step("config migrate", detail, skip=f"edit by hand: {by_hand}")
    if any(s.replace for s in plan.steps) and not replace:
        return Step("config migrate", detail, skip="module_paths still point into hal-ecosystem; rerun with --replace to rewrite (journaled)")
    suffix = f"; edit by hand: {by_hand}" if by_hand else ""
    return Step("config migrate", detail, lambda: f"journal {migrate.apply(plan)}{suffix}")


def sandbox_dir() -> Path:
    """The sandbox state folder; the doctor fails when it is open to group or others."""
    return paths.state_dir() / "sandbox"


def _sandbox_step() -> Step:
    path = sandbox_dir()
    if path.is_dir() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == PRIVATE_MODE:
        return Step("sandbox state", str(path), skip="already private")
    return Step("sandbox state", f"create or chmod {PRIVATE_MODE:04o} {path}", _tighten_sandbox)


def _tighten_sandbox() -> str:
    """Creates the sandbox state folder or narrows it to 0700; refuses a symlink or another owner."""
    path = sandbox_dir()
    if path.is_symlink():
        raise OSError(f"{path} is a symlink; remove it and rerun")
    if not path.exists():
        path.mkdir(mode=PRIVATE_MODE, parents=True)
    elif path.stat().st_uid != os.getuid():
        raise OSError(f"{path} belongs to another user; not touching it")
    path.chmod(PRIVATE_MODE)
    return f"{path} is {PRIVATE_MODE:04o}"


def _sync() -> str:
    _run(["uv", "sync", "--all-packages", "--frozen"], SYNC_TIMEOUT_S, cwd=config.ROOT)
    return "synced"


def jobhunt_root() -> Path | None:
    """`[jobs].jobhunt_root` when it names a checkout with a `pyproject.toml`, else None."""
    configured = config.load().get("jobs", {}).get("jobhunt_root")
    if not configured:
        return None
    root = Path(str(configured)).expanduser()
    return root if (root / "pyproject.toml").is_file() else None


def _employment_step(dirs: list[Path], have: dict) -> Step:
    repo = jobhunt_root() or find_sibling(EMPLOYMENT_SIBLINGS, dirs)
    if repo is None:
        return Step("employment helper", "no checkout found", skip="not found, skipped")
    if not has_module(repo):
        return Step("employment helper", str(repo), skip="no Hal module in it yet; it still runs on its own")
    if _pack_name(repo) in have:
        return Step("employment helper", str(repo), skip="already installed")
    return Step("employment helper", f"hal packs add --local {repo}", lambda: packs.add_local(repo))


def _gpu_light_step(dirs: list[Path]) -> Step:
    found = gpulight.binary()
    if found:
        return Step("gpu-light", found, skip="found, kept as it is")
    repo = find_sibling((GPU_LIGHT_SIBLING,), dirs)
    if repo is None:
        return Step("gpu-light", "not installed", skip="not installed; Hal works without it")
    return Step("gpu-light", f"uv tool install {repo}", lambda: _tool_install(repo))


def _tool_install(repo: Path) -> str:
    _run(["uv", "tool", "install", str(repo)], TOOL_INSTALL_TIMEOUT_S)
    return "installed"


def _image_step() -> Step:
    from halsandbox import images

    if images.status():
        return Step("box image", "sandbox image", skip="already built")
    if not shutil.which("docker"):
        return Step("box image", "docker not found", skip="docker is not installed; build it later with `hal box image build`")
    argv = [sys.executable, "-m", "halcore", "box", "image", "build"]
    return Step("box image", "hal box image build", lambda: (_run(argv, IMAGE_TIMEOUT_S), "built")[1])


def _units_step(enable: bool, replace: bool) -> Step:
    if not shutil.which("systemctl"):
        return Step("units", "systemctl not found", skip="systemd is not available; skipped")
    verb = "write and enable" if enable else "write (enable with --enable)"
    detail = f"{verb} the hal-* user units"
    shown = units.exec_starts()
    if shown:
        detail += ", running from:\n" + "\n".join(f"         {line}" for line in shown)
    foreign = units.foreign_units(gateway_bin_dir())
    if foreign and not replace:
        return Step("units", detail, blocked=(
            f"{len(foreign)} unit(s) belong to another install: {', '.join(foreign)}; "
            "stop and back them up, then rerun with --replace"))
    if foreign:
        detail += f", replacing (and stopping) {len(foreign)} of another install: {', '.join(foreign)}"
    return Step("units", detail, lambda: _install_units(enable))


def _install_units(enable: bool) -> str:
    report = units.install(start=False, desktop=False, enable=enable, bin_dir=gateway_bin_dir())
    names = sorted(n for group in report.values() for n in group if n.startswith("hal"))
    return ", ".join(names) or "no units found"


def execute(steps: list[Step], *, dry_run: bool, out: Callable[[str], None] = print) -> bool:
    """Runs or prints `steps`; returns True when none failed."""
    ok = True
    for step in steps:
        label = f"{step.name:<18}"
        if step.skip:
            out(f"[skip] {label} {step.skip}")
        elif step.blocked:
            out(f"[FAIL] {label} {step.blocked}")
            ok = False
        elif dry_run:
            out(f"[plan] {label} {step.detail}")
        else:
            try:
                result = step.run() if step.run else ""
                out(f"[ ok ] {label} {result or step.detail}")
            except STEP_ERRORS as exc:
                log.warning("setup step %s failed: %s", step.name, exc)
                out(f"[FAIL] {label} {exc}")
                ok = False
    return ok


def main(argv: list[str]) -> int:
    """Runs `hal setup`; exits 1 when a step or the doctor failed."""
    ap = argparse.ArgumentParser(prog="hal setup", description="finish a Hal install from this checkout (idempotent)")
    ap.add_argument("--dry-run", action="store_true", help="print the steps and change nothing")
    ap.add_argument("--enable", action="store_true", help="also enable the units (they are only written otherwise)")
    ap.add_argument("--from", dest="dirs", action="append", default=[], type=Path, metavar="DIR",
                    help="folder that holds the sibling repos (repeatable; default: the checkout's parent folders)")
    ap.add_argument("--no-image", action="store_true", help="do not build the box image")
    ap.add_argument("--migrate-from", dest="migrate_from", type=Path, metavar="PATH",
                    help="older hal-ecosystem folder (Hal before 0.1) whose config to carry over; default: [migrate].source")
    ap.add_argument("--replace", action="store_true",
                    help="replace, stop and delete unit files of another install (back them up first)")
    args = ap.parse_args(argv)
    steps = plan(search_dirs(args.dirs), enable=args.enable, image=not args.no_image, replace=args.replace,
                 migrate_from=args.migrate_from)
    ok = execute(steps, dry_run=args.dry_run)
    checks = doctor.collect()
    print(doctor.render(checks))
    failed = any(c.status == doctor.FAIL for c in checks)
    print("setup: dry run, nothing changed" if args.dry_run else "setup: done" if ok and not failed else "setup: finished with problems; see above")
    return EXIT_OK if ok and not failed else EXIT_FAILED
