"""Installs the systemd user units and desktop launchers of core hal and every installed extension.

Units live in each module's `systemd/` folder and launchers in its `desktop/` folder. They are
rendered, not linked: `@ROOT@` becomes the module's folder, `@CORE@` core hal's and
`@ECOSYSTEM@` the ecosystem folder, so a checkout works wherever it was cloned. Every
long-running unit is part of hal.target; units marked `X-HalGPU=yes` are the ones `hal off`
stops to free the GPU.

    hal units install [--start]    render and enable everything, optionally start hal.target
    hal units remove <module>      stop and delete one module's units
    hal units list                 installed units, by module, with their state
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config

UNIT_DIR = Path(os.environ.get("HAL_UNIT_DIR", "~/.config/systemd/user")).expanduser()
UNIT_SUFFIXES = (".service", ".timer", ".target", ".path")


def _record() -> Path:
    return config.state_dir() / "units.json"


def installed() -> dict[str, str]:
    """Returns {unit name: module folder} for the units hal installed."""
    try:
        return json.loads(_record().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(units: dict[str, str]) -> None:
    _record().write_text(json.dumps(units, indent=1, sort_keys=True))


def module_roots() -> list[Path]:
    """Returns core hal plus every module folder hal-hub would load, minus disabled ones."""
    roots = [config.ROOT]
    hub = config.load().get("hub", {})
    disabled = set(hub.get("disabled", []))
    paths = [Path(p).expanduser() for p in hub.get("module_paths", [])] or [config.ECOSYSTEM / "core",
                                                                            config.extensions_dir()]
    for base in paths:
        if not base.is_dir():
            continue
        for d in sorted([base] if (base / "hal-module.toml").exists() else base.iterdir()):
            if d == config.ROOT or not (d / "hal-module.toml").exists():
                continue
            name = re.search(r'^name\s*=\s*"([^"]+)"', (d / "hal-module.toml").read_text(), re.M)
            if name and name.group(1) in disabled:
                continue
            if d not in roots:
                roots.append(d)
    return roots


def render(text: str, root: Path) -> str:
    return (text.replace("@ROOT@", str(root)).replace("@CORE@", str(config.ROOT))
            .replace("@ECOSYSTEM@", str(config.ECOSYSTEM)).replace("@HOME@", str(Path.home())))


def is_gpu(unit_file: Path) -> bool:
    try:
        return bool(re.search(r"^X-HalGPU\s*=\s*(yes|true|1)\s*$", unit_file.read_text(), re.M | re.I))
    except OSError:
        return False


def gpu_units() -> list[str]:
    return sorted(name for name in installed() if is_gpu(UNIT_DIR / name))


def systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=check)


def install_module(root: Path, units: dict[str, str]) -> list[str]:
    """Renders one module's units into the user unit folder; returns their names."""
    names = []
    folder = root / "systemd"
    if not folder.is_dir():
        return names
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    for src in sorted(folder.iterdir()):
        if src.suffix not in UNIT_SUFFIXES:
            continue
        dest = UNIT_DIR / src.name
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        text = render(src.read_text(), root)
        if is_gpu(src) and src.suffix == ".service":
            # While Hal is off, a timer or a login must not bring a GPU service back.
            off_flag = config.state_dir() / "off"
            text = text.replace("[Unit]\n", f"[Unit]\nConditionPathExists=!{off_flag}\n", 1)
        dest.write_text(text)
        units[src.name] = str(root)
        names.append(src.name)
    return names


def install_desktop(root: Path) -> list[str]:
    """Copies a module's desktop launchers to the Desktop, rendered and marked trusted."""
    folder = root / "desktop"
    if not folder.is_dir():
        return []
    desktop = Path(subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True).stdout.strip()
                   or Path.home() / "Desktop")
    desktop.mkdir(parents=True, exist_ok=True)
    out = []
    for src in sorted(folder.glob("*.desktop")):
        dest = desktop / src.name
        dest.write_text(render(src.read_text(), root))
        dest.chmod(0o755)
        subprocess.run(["gio", "set", str(dest), "metadata::trusted", "true"], capture_output=True)
        out.append(src.name)
    return out


def _enable(names: list[str]) -> None:
    for name in names:
        text = (UNIT_DIR / name).read_text()
        if "[Install]" in text and "@." not in name:
            systemctl("enable", name)


def install(start: bool = False, desktop: bool = True) -> dict:
    """Renders every module's units, enables them and, if asked, starts hal.target."""
    units = installed()
    report = {}
    for root in module_roots():
        names = install_module(root, units)
        launchers = install_desktop(root) if desktop else []
        if names or launchers:
            report[root.name] = names + launchers
    # Units from modules that are gone or disabled
    live = {str(r) for r in module_roots()}
    for name, root in list(units.items()):
        if root not in live:
            systemctl("disable", "--now", name)
            (UNIT_DIR / name).unlink(missing_ok=True)
            del units[name]
    _save(units)
    systemctl("daemon-reload")
    _enable(sorted(units))
    if start:
        systemctl("start", "hal.target")
    return report


def remove(module_root: Path) -> list[str]:
    units = installed()
    gone = [n for n, r in units.items() if Path(r) == module_root]
    for name in gone:
        systemctl("disable", "--now", name)
        (UNIT_DIR / name).unlink(missing_ok=True)
        del units[name]
    _save(units)
    systemctl("daemon-reload")
    return gone


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="hal units")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--start", action="store_true")
    i.add_argument("--no-desktop", action="store_true")
    r = sub.add_parser("remove")
    r.add_argument("module")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    if not shutil.which("systemctl"):
        print("hal: systemd is needed for Hal's background services")
        return 1
    if args.cmd == "install":
        for module, names in install(args.start, not args.no_desktop).items():
            print(f"  {module:16} {' '.join(names)}")
    elif args.cmd == "remove":
        root = next((r for r in module_roots() if r.name in (args.module, f"hal-{args.module}")), None)
        print("removed:", " ".join(remove(root)) if root else "nothing")
    else:
        for name, root in sorted(installed().items(), key=lambda x: (x[1], x[0])):
            state = systemctl("is-active", name).stdout.strip()
            print(f"  {Path(root).name:16} {name:36} {state:10} {'gpu' if is_gpu(UNIT_DIR / name) else ''}")
    return 0
