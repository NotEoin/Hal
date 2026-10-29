"""`hal ext`: add, remove, enable and update extensions.

An extension is a git repository with a hal-module.toml, cloned into the extensions
folder (ecosystem/extensions, or [hal].extensions_dir). hal-hub finds it there, and
`hal units install` starts its services. extensions.toml lists the ones `add` knows.

    hal ext list                      installed and available extensions
    hal ext add calendar tasks        clone, run each one's install step, start its services
    hal ext add --pack assistant      a set: the everyday desktop assistant
    hal ext disable phone             keep it installed but unused; `enable` undoes it
    hal ext remove phone              stop its services and move it to ~/.local/share/hal/removed
    hal ext setup [name ...]          run install steps again (all extensions when none named)
    hal ext update                    git pull every extension
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

from . import config, hub as hubmod, units

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


def disabled() -> list[str]:
    return list(config.load().get("hub", {}).get("disabled", []))


def set_disabled(names: list[str]) -> None:
    """Rewrites [hub].disabled in the config file, leaving everything else as it was."""
    path = config.config_path()
    text = path.read_text() if path.exists() else ""
    line = "disabled = [" + ", ".join(f'"{n}"' for n in sorted(set(names))) + "]"
    if re.search(r"^disabled\s*=.*$", text, re.M):
        text = re.sub(r"^disabled\s*=.*$", line, text, count=1, flags=re.M)
    elif re.search(r"^\[hub\]\s*$", text, re.M):
        text = re.sub(r"^\[hub\]\s*$", "[hub]\n" + line, text, count=1, flags=re.M)
    else:
        text += f"\n[hub]\n{line}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _install_step(root: Path) -> None:
    """Runs the [module].install command an extension declares, if any."""
    info = tomllib.loads((root / "hal-module.toml").read_text()).get("module", {})
    cmd = info.get("install")
    if cmd:
        print(f"  running {root.name}'s install step", flush=True)
        subprocess.run([a.replace("{root}", str(root)) for a in cmd], cwd=root)


def add(names: list[str]) -> int:
    known, have = catalog(), installed()
    base = config.extensions_dir()
    base.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name in names:
        name = name.removeprefix("hal-")
        if name in have:
            print(f"  {name}: already installed", flush=True)
            _install_step(have[name])          # safe to repeat; picks up anything new
            continue
        if name not in known:
            print(f"  {name}: not in the catalogue; clone it into {base} by hand")
            failed += 1
            continue
        repo = known[name]["repo"]
        if os.environ.get("HAL_GITHUB"):          # a fork or a local mirror
            repo = os.environ["HAL_GITHUB"].rstrip("/") + "/" + repo.rsplit("/", 1)[-1]
        dest = base / Path(repo).stem
        print(f"  {name}: cloning {repo}", flush=True)
        if subprocess.run(["git", "clone", "--quiet", repo, str(dest)]).returncode != 0:
            failed += 1
            continue
        _install_step(dest)
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
    hub = hubmod.load()
    counts = {}
    if hub:
        for t in hub.all_tools().values():
            counts[t.module.name] = counts.get(t.module.name, 0) + 1
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
        have = installed()
        for name in args.names or list(have):
            if name.removeprefix("hal-") in have:
                _install_step(have[name.removeprefix("hal-")])
        return 0
    if args.cmd == "update":
        for name, root in installed().items():
            r = subprocess.run(["git", "-C", str(root), "pull", "--ff-only", "--quiet"], capture_output=True, text=True)
            print(f"  {name:12} {'ok' if r.returncode == 0 else r.stderr.strip()[:100]}")
        return 0
    if args.cmd == "doctor":
        hub = hubmod.load()
        if not hub:
            print("hal-hub is not installed, so no extension can be used")
            return 1
        from halhub.__main__ import main as hub_main  # noqa: PLC0415
        return hub_main(["doctor"]) or 0
    list_all()
    return 0
