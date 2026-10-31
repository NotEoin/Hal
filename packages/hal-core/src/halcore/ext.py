"""`hal ext`: add, remove, enable and update extensions.

An extension is a git repository with a hal-module.toml, cloned into the extensions
folder (ecosystem/extensions, or [hal].extensions_dir). hal-hub finds it there, and
`hal units install` starts its services. extensions.toml lists the ones `add` knows.

    hal ext list                      installed and available extensions
    hal ext add calendar tasks        fetch the pinned commit, show what it can do, install after a typed y
    hal ext add --pack assistant      a set: the everyday desktop assistant
    hal ext disable phone             keep it installed but unused; `enable` undoes it
    hal ext remove phone              stop its services and move it to ~/.local/share/hal/removed
    hal ext setup [name ...]          run install steps again, one typed y each
    hal ext update                    move installed extensions to the catalogue's pin, reviewed
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

from halkit import proc

from . import config, packreview, units

PACKS = {
    "assistant": ["memory", "calendar", "tasks", "notify", "clock", "web", "briefing", "voice"],
    "computer": ["system", "desktop", "files", "power", "packages", "devices", "network", "shell", "screen"],
    "life": ["habits", "ledger", "contacts", "feeds", "mail", "phone", "telegram"],
}


def catalog() -> dict[str, dict]:
    try:
        return tomllib.loads((config.ROOT / "extensions.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def installed() -> dict[str, Path]:
    """Returns {module name: folder} for every extension on disk, disabled ones included."""
    out = {}
    base = config.extensions_dir()
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        manifest = d / "hal-module.toml"
        if manifest.exists():
            try:
                out[tomllib.loads(manifest.read_text())["module"]["name"]] = d
            except (OSError, KeyError, tomllib.TOMLDecodeError):
                continue
    return out


from .packs import disabled, set_disabled  # noqa: E402,F401 - moved; ext.py is retired next


def add(names: list[str]) -> int:
    """Installs catalogue entries after a pinned fetch and a reviewed, typed confirmation."""
    known = catalog()
    base = config.extensions_dir()
    failed = 0
    for name in names:
        name = name.removeprefix("hal-")
        if name not in known:
            print(f"  {name}: not in the catalogue; see extensions.toml")
            failed += 1
            continue
        entry = dict(known[name])
        if os.environ.get("HAL_GITHUB"):          # a fork or a local mirror; the pin still has to match
            entry["repo"] = os.environ["HAL_GITHUB"].rstrip("/") + "/" + str(entry["repo"]).rsplit("/", 1)[-1]
        try:
            if not packreview.install(name, entry, base):
                failed += 1
        except (packreview.PackRefused, proc.ProcTimeout, subprocess.CalledProcessError, OSError) as exc:
            print(f"  {name}: {exc}")
            failed += 1
    if shutil.which("systemctl"):
        units.install(start=not _hal_off())
    return 1 if failed else 0


def _hal_off() -> bool:
    return (config.state_dir() / "off").exists()


def remove(name: str) -> int:
    root = installed().get(name.removeprefix("hal-"))
    if not root:
        print(f"{name} is not installed")
        return 1
    units.remove(root)
    dest = Path("~/.local/share/hal/removed").expanduser() / f"{root.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root), dest)
    print(f"Removed {name}; its folder is kept at {dest} (delete it when you are sure).")
    return 0


def list_all() -> None:
    have, known, off = installed(), catalog(), set(disabled())
    counts = {}
    for t in config.hub().all_tools().values():
        counts[t.module] = counts.get(t.module, 0) + 1
    print("Installed:")
    for name, root in have.items():
        state = "disabled" if name in off else f"{counts.get(name, 0)} tools"
        print(f"  {name:12} {state:10} {root}")
    missing = [n for n in known if n not in have]
    if missing:
        print("\nAvailable (hal ext add <name>):")
        for n in missing:
            print(f"  {n:12} {known[n].get('summary', '')}")
    print(f"\nPacks: {', '.join(f'{k} ({len(v)})' for k, v in PACKS.items())}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="hal ext")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.add_argument("names", nargs="*")
    a.add_argument("--pack", choices=sorted(PACKS))
    for cmd in ("remove", "enable", "disable"):
        sub.add_parser(cmd).add_argument("name")
    s = sub.add_parser("setup")
    s.add_argument("names", nargs="*")
    sub.add_parser("list")
    sub.add_parser("update")
    sub.add_parser("doctor")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        return add(args.names + (PACKS[args.pack] if args.pack else []))
    if args.cmd == "remove":
        return remove(args.name)
    if args.cmd in ("enable", "disable"):
        name = args.name.removeprefix("hal-")
        names = [n for n in disabled() if n != name] + ([name] if args.cmd == "disable" else [])
        set_disabled(names)
        units.install(start=not _hal_off(), desktop=False)
        print(f"{name} {args.cmd}d")
        return 0
    if args.cmd == "setup":
        from halhub.cli import main as hub_main  # noqa: PLC0415 - loaded only for this command

        return hub_main(["setup", *[n.removeprefix("hal-") for n in args.names]]) or 0
    if args.cmd == "update":
        # an update is an install of the catalogue's pin: new facts are shown and confirmed (REQ-SEC-85)
        return add([n for n in installed() if n in catalog()])
    if args.cmd == "doctor":
        from halhub.__main__ import main as hub_main  # noqa: PLC0415
        return hub_main(["doctor"]) or 0
    list_all()
    return 0
