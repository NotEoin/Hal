"""`hal migrate ecosystem` and `hal migrate undo`: move from the old hal-ecosystem layout.

The default is a read-only dry run that prints every change. With `--yes` it applies them, and
each change is written to a journal (`state_dir()/migrate/<UTC>/journal.json`, with `backup/`)
before it is made, so `hal migrate undo JOURNAL` can restore the previous state.
Design decisions:

- Nothing is ever written inside the `--from` folder or any hal-employment folder
  (REQ-CLI-88); `guard_target` checks every path before the plan accepts it.
- The old `hal.conf` is only listed in `[compat].hal_conf`, never rewritten.
- Pack registration, unit rendering and the vault `.mcp.json` edit are not done here: they are
  printed as commands for the person to run (the vault edit needs their explicit approval).
- A config table that already exists is not edited by text append (that would write a second
  header); the plan reports it as manual instead of guessing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from halkit import config as kitconfig
from halkit import files, paths, timeutil

LINK_NAMES = ("hal", "hal-hub", "hal-gateway", "hal-remote")  # the console scripts the old install linked
DEFAULT_JOBHUNT_ROOT = "~/src/employment-helper"  # the old jobs default, written explicitly (H1)
PROTECTED_PREFIXES = ("employment-helper", "wt", "hal-employment")  # never written (REQ-CLI-88)
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


class MigrateError(Exception):
    """The migration cannot proceed; the message says why."""


@dataclass
class Step:
    """One planned change: `config` appends text, `link` points a symlink, `manual` is only printed."""

    kind: str
    target: Path | None
    detail: str
    new: str = ""  # config: text to append; link: the new link target
    old: str | None = None  # link: the previous target, None when the link did not exist


@dataclass
class Plan:
    """The ordered steps and the notes that follow them."""

    steps: list[Step] = field(default_factory=list)


def guard_target(path: Path, source: Path) -> None:
    """Raises `MigrateError` when writing `path` would touch `source` or a protected folder."""
    resolved = path.parent.resolve() / path.name  # a symlink is replaced, not followed
    src = source.resolve()
    if resolved == src or src in resolved.parents:
        raise MigrateError(f"refusing to write inside the source folder: {path}")
    for part in resolved.parts:
        if part.startswith(PROTECTED_PREFIXES):
            raise MigrateError(f"refusing to write inside a protected folder: {path}")


def _config_step(source: Path, cfg_path: Path) -> Step | None:
    data = kitconfig.read_toml(cfg_path) if cfg_path.exists() else {}
    hal_conf = source / "core" / "hal" / "hal.conf"
    chunks: list[str] = []
    manual: list[str] = []
    if hal_conf.is_file():
        if "compat" in data:
            manual.append(f"add {hal_conf} to [compat].hal_conf")
        else:
            chunks.append(f'[compat]\nhal_conf = ["{hal_conf}"]\n')
    if "jobs" not in data:
        chunks.append(f'[jobs]\njobhunt_root = "{DEFAULT_JOBHUNT_ROOT}"\n')
    if not chunks and not manual:
        return None
    detail = "append to config.toml: " + "; ".join([c.strip().replace("\n", " ") for c in chunks] + manual)
    if not chunks:
        return Step("manual", None, detail)
    return Step("config", cfg_path, detail, new="\n" + "\n".join(chunks))


def build_plan(source: Path, *, home: Path) -> Plan:
    """Reads the current machine (never writes) and returns the changes the migration would make."""
    if not source.is_dir():
        raise MigrateError(f"--from {source} is not a folder")
    plan = Plan()
    config_step = _config_step(source, paths.config_file())
    if config_step:
        plan.steps.append(config_step)
    venv_bin = paths.data_dir() / "venv" / "bin"
    bin_dir = home / ".local" / "bin"
    for name in LINK_NAMES:
        want = venv_bin / name
        link = bin_dir / name
        if not want.exists():
            plan.steps.append(Step("manual", None, f"link {link}: {want} is missing, run the installer first"))
            continue
        old = os.readlink(link) if link.is_symlink() else None
        if old == str(want):
            continue
        if link.exists() and not link.is_symlink():
            plan.steps.append(Step("manual", None, f"{link} is a regular file; move it away, then rerun"))
            continue
        plan.steps.append(Step("link", link, f"link {link} -> {want} (was {old or 'absent'})", new=str(want), old=old))
    plan.steps += [
        Step("manual", None, "register packs: hal packs add --local PATH (once pack commands exist)"),
        Step("manual", None, "render units: hal units install (does not start anything)"),
        Step("manual", None, "vault .mcp.json: needs your approval, run `hal packs setup memory` yourself"),
    ]
    for step in plan.steps:
        if step.target is not None:
            guard_target(step.target, source)
    return plan


def render_plan(plan: Plan, *, applying: bool) -> str:
    """The plan as text; a dry run says so and how to apply."""
    lines = [f"{i}. [{s.kind}] {s.detail}" for i, s in enumerate(plan.steps, 1)]
    if not applying:
        lines.append("dry run: nothing was changed; rerun with --yes to apply and journal these changes")
    return "\n".join(lines)


def apply(plan: Plan, *, stamp: str | None = None) -> Path:
    """Applies the plan, journaling each change first; returns the journal path."""
    folder = paths.state_dir() / "migrate" / (stamp or timeutil.iso_z().replace(":", ""))
    backup = folder / "backup"
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = folder / "journal.json"
    entries: list[dict[str, str | None]] = []

    def save() -> None:
        files.write_atomic(journal, json.dumps({"entries": entries}, indent=2) + "\n")

    for step in plan.steps:
        if step.kind == "config" and step.target is not None and step.new:
            saved = backup / "config.toml"
            existed = step.target.exists()
            if existed:
                shutil.copy2(step.target, saved)
            entries.append({"kind": "config", "target": str(step.target), "backup": str(saved) if existed else None})
            save()
            before = step.target.read_text() if existed else ""
            files.write_atomic(step.target, before.rstrip("\n") + "\n" + step.new)
        elif step.kind == "link" and step.target is not None:
            entries.append({"kind": "link", "target": str(step.target), "old": step.old})
            save()
            step.target.parent.mkdir(parents=True, exist_ok=True)
            if step.target.is_symlink():
                step.target.unlink()
            step.target.symlink_to(step.new)
    save()
    return journal


def undo(journal: Path) -> list[str]:
    """Restores the state recorded in `journal`, newest change first; returns what it did."""
    try:
        entries = json.loads(journal.read_text())["entries"]
    except (OSError, ValueError, KeyError) as exc:
        raise MigrateError(f"cannot read journal {journal}: {exc}") from exc
    done = []
    for entry in reversed(entries):
        target = Path(entry["target"])
        if entry["kind"] == "config":
            if entry["backup"]:
                files.write_atomic(target, Path(entry["backup"]).read_bytes())
            else:
                target.unlink(missing_ok=True)
        elif entry["kind"] == "link":
            if target.is_symlink():
                target.unlink()
            if entry["old"]:
                target.symlink_to(entry["old"])
        done.append(f"restored {target}")
    return done


def main(argv: list[str]) -> int:
    """Runs `hal migrate`; a dry run is the default and `--yes` applies."""
    parser = argparse.ArgumentParser(prog="hal migrate", description="move from the old hal-ecosystem layout")
    sub = parser.add_subparsers(dest="cmd", required=True)
    eco = sub.add_parser("ecosystem", help="plan or apply the move; dry run unless --yes")
    eco.add_argument("--from", dest="source", type=Path, default=Path.home() / "src/hal-ecosystem")
    eco.add_argument("--dry-run", action="store_true", help="print the plan only (the default)")
    eco.add_argument("--yes", action="store_true", help="apply the plan and write a journal")
    und = sub.add_parser("undo", help="restore the state recorded in a journal")
    und.add_argument("journal", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "undo":
            print("\n".join(undo(args.journal)) or "journal is empty")
            return EXIT_OK
        if args.yes and args.dry_run:
            print("hal migrate: --yes and --dry-run exclude each other", file=sys.stderr)
            return EXIT_USAGE
        plan = build_plan(args.source.expanduser(), home=Path.home())
        applying = args.yes
        print(render_plan(plan, applying=applying))
        if applying:
            print(f"journal: {apply(plan)}")
    except MigrateError as exc:
        print(f"hal migrate: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK
