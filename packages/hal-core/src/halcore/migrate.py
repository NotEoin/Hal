"""`hal migrate ecosystem` and `hal migrate undo`: move from the old hal-ecosystem layout.

The default is a read-only dry run that prints every change. With `--yes` it applies them, and
each change is written to a journal (`state_dir()/migrate/<UTC>/journal.json`, with `backup/`)
before it is made, so `hal migrate undo JOURNAL` can restore the previous state.
Design decisions:

- Nothing is ever written inside the `--from` folder or any hal-employment folder
  (REQ-CLI-88); `guard_target` checks every path before the plan accepts it.
- The old `hal.conf` is only listed in `[compat].hal_conf`, never rewritten.
- `hal setup` applies only the config part (`config_plan`): the added tables and the dropped
  hal-ecosystem entries of `[hub].module_paths`, journaled like the rest.
- Pack registration, unit rendering and the vault `.mcp.json` edit are not done here: they are
  printed as commands for the person to run (the vault edit needs their explicit approval).
- A config table that already exists is not edited by text append (that would write a second
  header); the plan reports it as manual instead of guessing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from halkit import config as kitconfig
from halkit import files, paths, timeutil

LINK_NAMES = ("hal", "hal-hub", "hal-gateway", "hal-remote")  # the console scripts the old install linked
DEFAULT_SOURCE = Path.home() / "src/hal-ecosystem"  # where the old install lives, read-only
DEFAULT_JOBHUNT_ROOT = "~/src/employment-helper"  # the old jobs default, written explicitly (H1)
ECOSYSTEM_NAME = "hal-ecosystem"  # the old install's folder; its modules are replaced by packs
MODULE_PATHS_RE = re.compile(r"^module_paths\s*=\s*\[[^\]]*\]", re.M)
PROTECTED_PREFIXES = ("employment-helper", "wt", "hal-employment")  # never written (REQ-CLI-88)
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2

# Every old hal-ecosystem unit and where its new form comes from. The unit names did not change:
# the file is now rendered from a pack (or core Hal) by `hal units install`, not from the old tree.
UNIT_MAP: dict[str, str] = {
    "hal-console.service": "core Hal",
    "hal-clock.service": "pack organiser",
    "hal-calendar-reminders.service": "pack organiser",
    "hal-tasks-reminders.service": "pack organiser",
    "hal-routines.service": "pack organiser",
    "hal-briefing@.service": "pack organiser",
    "hal-briefing-morning.timer": "pack organiser",
    "hal-briefing-midday.timer": "pack organiser",
    "hal-briefing-evening.timer": "pack organiser",
    "hal-rgb-server.service": "pack desktop",
    "hal-telegram.service": "pack comms",
    "hal-employment.service": "pack jobs",
    "hal-habits-nudge.service": "pack life",
    "hal-habits-nudge.timer": "pack life",
    "hal-ledger-remind.service": "pack life",
    "hal-ledger-remind.timer": "pack life",
    "hal-memory-idle.service": "pack memory",
    "hal-voice.service": "pack voice",
    "hal-tts.service": "pack voice",
}
RETIRED_UNITS: dict[str, str] = {"hal-brief.service": "hal-briefing@.service", "hal-brief.timer": "hal-briefing-morning.timer"}


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
    replace: bool = False  # config: `new` is the whole file, not text to append


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


def config_plan(source: Path, cfg_path: Path) -> Plan:
    """The config-only part of the migration: what `hal setup` applies without the links and notes."""
    added, rewritten = _config_step(source, cfg_path), module_paths_step(source, cfg_path)
    steps = [s for s in (added, rewritten) if s is not None]
    if added and rewritten and added.kind == rewritten.kind == "config":
        # One step, because two whole-file writes from the same original text would undo each other
        merged = rewritten.new.rstrip("\n") + "\n" + added.new
        steps = [Step("config", cfg_path, f"{added.detail}; {rewritten.detail}", new=merged, replace=True)]
    for step in steps:
        if step.target is not None:
            guard_target(step.target, source)
    return Plan(steps)


def _under(path: Path, source: Path) -> bool:
    expanded = path.expanduser()
    return source in (expanded, *expanded.parents) or ECOSYSTEM_NAME in expanded.parts


def _only_module_paths_changed(before: str, after: str, keep: list) -> bool:
    """True when `after` parses and differs from `before` only in `[hub].module_paths`, now `keep`.

    The line regex can match a `module_paths` key of another table, or stop at a `]` inside a
    string; either would write a wrong or broken config, so the text edit is checked by parsing.
    """
    try:
        old, new = tomllib.loads(before), tomllib.loads(after)
    except tomllib.TOMLDecodeError:
        return False
    old.setdefault("hub", {})["module_paths"] = keep
    return old == new


def module_paths_step(source: Path, cfg_path: Path) -> Step | None:
    """A step that drops `[hub].module_paths` entries inside the old ecosystem, or None.

    The registered packs provide those modules now, and a left-over entry makes units render
    from the old tree. Entries elsewhere are kept; only the `module_paths` line is rewritten.
    """
    if not cfg_path.exists():
        return None
    text = cfg_path.read_text(encoding="utf-8")
    listed = kitconfig.read_toml(cfg_path).get("hub", {}).get("module_paths")
    if not isinstance(listed, list):
        return None
    keep = [p for p in listed if not _under(Path(str(p)), source)]
    if len(keep) == len(listed):
        return None
    line = "module_paths = [" + ", ".join('"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"' for p in keep) + "]"
    new, count = MODULE_PATHS_RE.subn(lambda _m: line, text, count=1)
    if count != 1 or not _only_module_paths_changed(text, new, keep):
        return Step("manual", None, f"remove the hal-ecosystem entries from [hub].module_paths in {cfg_path}")
    dropped = ", ".join(str(p) for p in listed if p not in keep)
    return Step("config", cfg_path, f"drop from [hub].module_paths: {dropped}", new=new, replace=True)


def unit_steps() -> list[Step]:
    """One manual step per old unit: stop it, then let `hal units install` render the new form."""
    steps = [Step("manual", None, f"unit {n}: stop and disable it, then it comes back from {src} via `hal units install`")
             for n, src in UNIT_MAP.items()]  # fmt: skip
    steps += [Step("manual", None, f"unit {n}: retired, replaced by {new}") for n, new in RETIRED_UNITS.items()]
    return steps


def build_plan(source: Path, *, home: Path) -> Plan:
    """Reads the current machine (never writes) and returns the changes the migration would make."""
    if not source.is_dir():
        raise MigrateError(f"--from {source} is not a folder")
    plan = Plan()
    plan.steps += config_plan(source, paths.config_file()).steps
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
    plan.steps += unit_steps()
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
            entry: dict[str, str | None] = {"kind": "config", "target": str(step.target),
                                            "backup": str(saved) if existed else None}
            if step.target.is_symlink():
                # The write replaces the link with a file and leaves the linked file alone, so
                # undo puts the link back instead of writing the backup over it
                entry["link"] = os.readlink(step.target)
            entries.append(entry)
            save()
            before = step.target.read_text() if existed else ""
            files.write_atomic(step.target, step.new if step.replace else before.rstrip("\n") + "\n" + step.new)
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
            if entry.get("link"):
                target.unlink(missing_ok=True)
                target.symlink_to(entry["link"])
            elif entry["backup"]:
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
    eco.add_argument("--from", dest="source", type=Path, default=DEFAULT_SOURCE)
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
