"""Installs the systemd user units and desktop launchers of core hal and every installed extension.

Module folders come from the pack registry (`packs.json`); `[hub].module_paths` adds only modules
no pack provides, and nothing else is scanned (an empty list adds nothing). Units live in each module's `systemd/` folder and launchers in its `desktop/` folder. They are
rendered, not linked: `@ROOT@` becomes the module's folder, `@CORE@` core hal's and
`@ECOSYSTEM@` the ecosystem folder and `@PYTHON@` the interpreter of the environment the module's
pack was installed into (core hal's own venv for core hal), so a checkout works wherever it was cloned. Every
long-running unit is part of hal.target; units marked `X-HalGPU=yes` are the ones `hal off`
stops to free the GPU.

hal.target starts at every login, not only on `hal on`, so core hal's hal-gpu-light.service
claims the GPU light (see gpulight.py) before the GPU services start: it is rendered with the
gpu-light executable and ordered before every X-HalGPU service of every module, so the
extensions' units need no change. Without gpu-light it is not installed, and one left from
before is removed.

    hal units install [--start]    render and enable everything, optionally start hal.target
    hal units remove <module>      stop and delete one module's units
    hal units list                 installed units, by module, with their state
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config, gpulight, packs

log = logging.getLogger(__name__)

UNIT_DIR = Path(os.environ.get("HAL_UNIT_DIR", "~/.config/systemd/user")).expanduser()
HELPER_TIMEOUT_S = 10.0  # desktop helpers (xdg-user-dir, gio) answer at once or are stuck
SYSTEMCTL_TIMEOUT_S = 60.0  # a stuck unit stop must not hang `hal kill`
UNIT_SUFFIXES = (".service", ".timer", ".target", ".path")
LIGHT_UNIT = "hal-gpu-light.service"
DEFAULT_PYTHON = "/usr/bin/python3"  # modules outside any pack have no environment of their own


def _record() -> Path:
    return config.state_path() / "units.json"


def installed() -> dict[str, str]:
    """Returns {unit name: module folder} for the units hal installed."""
    try:
        return json.loads(_record().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(units: dict[str, str]) -> None:
    config.state_dir()
    _record().write_text(json.dumps(units, indent=1, sort_keys=True))


def _pack_roots() -> tuple[dict[Path, str], set[str]]:
    """Returns {folder: interpreter} for every registered pack, and the module names packs provide.

    A folder is a module folder (it holds `hal-module.toml`) or a local pack's checkout when that
    carries `systemd/` (hal-memory and hal-voice keep their units there, beside the modules).
    """
    roots: dict[Path, str] = {}
    provided: set[str] = set()
    try:
        record = packs.read_record().get("packs", {})
    except packs.PackError as exc:
        log.warning("pack record unreadable, units fall back to [hub].module_paths: %s", exc)
        return roots, provided
    off = set(config.load().get("hub", {}).get("disabled", []))
    for name, entry in sorted(record.items()):
        if name in off:
            continue
        python = str(entry.get("python") or DEFAULT_PYTHON)
        checkout = Path(entry["path"]) if entry.get("path") else None
        if checkout is not None and (checkout / "systemd").is_dir():
            roots[checkout] = python
        for module, info in sorted(entry.get("modules", {}).items()):
            provided.add(module)
            if module in off:
                continue
            interp = (info.get("interpreter") or [python])[0]
            roots.setdefault(Path(info["manifest"]).parent, str(interp))
    return roots, provided


def module_roots() -> list[Path]:
    """Returns core hal, the registered packs' folders, then `[hub].module_paths` modules no pack provides."""
    roots = [config.ROOT]
    pack_roots, provided = _pack_roots()
    roots += [d for d in pack_roots if d.is_dir() and d not in roots]
    hub = config.load().get("hub", {})
    disabled = set(hub.get("disabled", []))
    # No fallback to the old ecosystem folders: hal-hub loads nothing else, so a unit from them
    # would run a module the hub never registered (and `hal setup` can leave the list empty)
    for base in (Path(p).expanduser() for p in hub.get("module_paths", [])):
        if not base.is_dir():
            continue
        for d in sorted([base] if (base / "hal-module.toml").exists() else base.iterdir()):
            if d == config.ROOT or not (d / "hal-module.toml").exists():
                continue
            name = re.search(r'^name\s*=\s*"([^"]+)"', (d / "hal-module.toml").read_text(), re.M)
            if name and (name.group(1) in disabled or name.group(1) in provided):
                continue
            if d not in roots:
                roots.append(d)
    return roots


def core_python() -> str:
    """The interpreter core hal's units run on: the checkout venv, else the one running hal now."""
    venv = config.ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def python_for(root: Path) -> str:
    """The interpreter `@PYTHON@` becomes for `root`: core hal's, its pack's environment, else the system python.

    Core hal needs its own environment: the system python has none of its dependencies.
    """
    if root == config.ROOT:
        return core_python()
    return _pack_roots()[0].get(root, DEFAULT_PYTHON)


def render(text: str, root: Path, python: str | None = None) -> str:
    """Fills the `@...@` placeholders of a unit or launcher template for the module at `root`."""
    return (text.replace("@ROOT@", str(root)).replace("@CORE@", str(config.ROOT))
            .replace("@PYTHON@", python or python_for(root))
            .replace("@ECOSYSTEM@", str(config.ECOSYSTEM)).replace("@HOME@", str(Path.home())))


def exec_starts() -> list[str]:
    """One `unit: ExecStart` line per unit `install` would write, rendered, so a person sees where it runs from."""
    lines = []
    for root in module_roots():
        python = python_for(root)
        for src in sorted((root / "systemd").glob("*.service")):
            for line in render(src.read_text(), root, python).splitlines():
                if line.startswith("ExecStart="):
                    lines.append(f"{src.name}: {line.removeprefix('ExecStart=')}")
    return lines


def missing_exec_paths() -> list[str]:
    """Rendered `ExecStart` programs that do not exist, as `unit: path` lines (absolute paths only)."""
    found = []
    for root in module_roots():
        python = python_for(root)
        for src in sorted((root / "systemd").glob("*.service")):
            for line in render(src.read_text(), root, python).splitlines():
                if not line.startswith("ExecStart="):
                    continue
                words = line.removeprefix("ExecStart=").lstrip("-@:+!").split()
                if words and words[0].startswith("/") and not Path(words[0]).exists():
                    found.append(f"{src.name}: {words[0]}")
    return found


def outside_packs() -> list[str]:
    """Units that would render from a folder no registered pack provides (other than core Hal)."""
    pack_roots, _ = _pack_roots()
    return [f"{root}" for root in module_roots() if root != config.ROOT and root not in pack_roots
            and (root / "systemd").is_dir()]  # fmt: skip


def is_gpu(unit_file: Path) -> bool:
    try:
        return bool(re.search(r"^X-HalGPU\s*=\s*(yes|true|1)\s*$", unit_file.read_text(), re.M | re.I))
    except OSError:
        return False


def gpu_services(roots: list[Path]) -> list[str]:
    """Returns the X-HalGPU services in the modules' systemd folders, except the light unit."""
    names = set()
    for root in roots:
        for src in (root / "systemd").glob("*.service"):
            if src.name != LIGHT_UNIT and "@." not in src.name and is_gpu(src):
                names.add(src.name)
    return sorted(names)


def fill_light_unit(text: str, gpu_light: str, services: list[str]) -> str:
    """Fills in the light unit: the gpu-light executable and the services it starts before."""
    text = text.replace("@GPU_LIGHT@", gpu_light)
    if services:
        return text.replace("@HAL_GPU_UNITS@", " ".join(services))
    return re.sub(r"^Before=@HAL_GPU_UNITS@\n", "", text, flags=re.M)


def gpu_units() -> list[str]:
    return sorted(name for name in installed() if is_gpu(UNIT_DIR / name))


def systemctl(*args: str, check: bool = False, timeout: float = SYSTEMCTL_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=check, timeout=timeout
    )


def install_module(root: Path, units: dict[str, str], skipped: list[str] | None = None) -> list[str]:
    """Renders one module's units into the user unit folder; returns their names.

    The light unit is left out, and named in `skipped`, when gpu-light is not installed.
    """
    names = []
    folder = root / "systemd"
    if not folder.is_dir():
        return names
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    for src in sorted(folder.iterdir()):
        if src.suffix not in UNIT_SUFFIXES:
            continue
        dest = UNIT_DIR / src.name
        gpu_light = gpulight.binary() if src.name == LIGHT_UNIT else None
        if src.name == LIGHT_UNIT and gpu_light is None:
            # Disabled while its file still exists, so systemd finds and drops the login link
            if units.pop(src.name, None) is not None:
                systemctl("disable", src.name)
            dest.unlink(missing_ok=True)
            if skipped is not None:
                skipped.append(f"{src.name} (gpu-light is not installed)")
            continue
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        text = render(src.read_text(), root)
        if gpu_light:
            text = fill_light_unit(text, gpu_light, gpu_services(module_roots()))
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
    desktop = Path(subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True,
                                  timeout=HELPER_TIMEOUT_S).stdout.strip()
                   or Path.home() / "Desktop")
    desktop.mkdir(parents=True, exist_ok=True)
    out = []
    for src in sorted(folder.glob("*.desktop")):
        dest = desktop / src.name
        dest.write_text(render(src.read_text(), root))
        dest.chmod(0o755)
        subprocess.run(["gio", "set", str(dest), "metadata::trusted", "true"], capture_output=True,
                       timeout=HELPER_TIMEOUT_S)
        out.append(src.name)
    return out


def _enable(names: list[str]) -> None:
    for name in names:
        text = (UNIT_DIR / name).read_text()
        if "[Install]" in text and "@." not in name:
            systemctl("enable", name)


GATEWAY_UNIT = "hal-gateway.service"


def render_gateway_unit(bin_dir: Path) -> str | None:
    """The gateway unit text with `@BIN@` filled in, or None when hal-gateway is not installed."""
    try:
        from importlib import resources

        template = (resources.files("halgateway") / "data" / GATEWAY_UNIT).read_text(encoding="utf-8")
    except (ImportError, OSError):
        return None
    return template.replace("@BIN@", str(bin_dir))


def install_gateway(bin_dir: Path, units: dict[str, str]) -> list[str]:
    """Writes `hal-gateway.service` into the user unit folder; returns its name, or nothing."""
    text = render_gateway_unit(bin_dir)
    if text is None:
        return []
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    dest = UNIT_DIR / GATEWAY_UNIT
    dest.unlink(missing_ok=True)
    dest.write_text(text)
    units[GATEWAY_UNIT] = str(config.ROOT)
    return [GATEWAY_UNIT]


def foreign_units(bin_dir: Path | None = None) -> list[str]:
    """Unit files `install` would overwrite, or stop and delete, that this checkout does not own.

    The unit record is shared with any older install (hal-ecosystem used the same state folder),
    so a unit recorded for a folder this checkout does not load, or an unrecorded file with a
    name this checkout writes, belongs to someone else.
    """
    roots = module_roots()
    live = {str(r) for r in roots}
    record = installed()
    found = {name for name, root in record.items() if root not in live}
    names = {src.name for r in roots if (r / "systemd").is_dir()
             for src in (r / "systemd").iterdir() if src.suffix in UNIT_SUFFIXES}  # fmt: skip
    if bin_dir is not None:
        names.add(GATEWAY_UNIT)
    for name in names:
        dest = UNIT_DIR / name
        if (dest.exists() or dest.is_symlink()) and record.get(name) not in live:
            found.add(name)
    return sorted(found)


def install(start: bool = False, desktop: bool = True, enable: bool = True, bin_dir: Path | None = None) -> dict:
    """Renders every module's units and, by default, enables them; `start` also starts hal.target.

    With `enable=False` the files are written and the daemon reloaded, but nothing is enabled or
    started, so a setup run never changes what runs at login. `bin_dir` adds the gateway unit.
    """
    units = installed()
    report = {}
    skipped: list[str] = []
    for root in module_roots():
        names = install_module(root, units, skipped)
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
    if bin_dir is not None:
        names = install_gateway(bin_dir, units)
        if names:
            report["hal-gateway"] = names
    if skipped:
        report["skipped"] = skipped
    _save(units)
    systemctl("daemon-reload")
    if enable:
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
