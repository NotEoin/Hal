"""Finds hal-hub, the extension loader, so the brain can use the tools of installed extensions.

hal works without it: with no hal-hub, the brain is a plain local chat model.
hal-hub is looked for at HAL_HUB, then beside this checkout (core/hal-hub), then
in each [hub].module_paths folder.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import config

_hub = None
_looked = False


def _candidates() -> list[Path]:
    out = []
    if os.environ.get("HAL_HUB"):
        out.append(Path(os.environ["HAL_HUB"]).expanduser())
    out.append(config.ROOT.parent / "hal-hub")
    for p in config.load().get("hub", {}).get("module_paths", []):
        out.append(Path(p).expanduser() / "hal-hub")
    return out


def load():
    """Returns the halhub package, or None when hal-hub is not installed."""
    global _hub, _looked
    if _looked:
        return _hub
    _looked = True
    for path in _candidates():
        if (path / "halhub").is_dir():
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
            break
    try:
        import halhub
        _hub = halhub
    except ImportError:
        _hub = None
    return _hub


def has(name: str) -> bool:
    hub = load()
    return bool(hub and hub.get(name))
