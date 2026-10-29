"""The hub's settings: the `[hub]` table of `config.toml`, read strictly through `halkit.config`.

04-hub.md, "Configuration", is the source of every key, default and reason. Design decisions:

- `[hub.runner]` and `[hub.mcp]` are their own tables, owned by the runner and the MCP
  server; `[hub]` declares them as table-valued keys only so that `halkit.config`'s strict
  unknown-key check does not reject a file that has them.
- Approval timeouts, scopes and audit settings are not hub keys: they are 01-security's
  `[approval]` and `[audit]` tables in `policy.toml`, so every front end behaves the same.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from halkit import config, paths

from .discovery import DEFAULT_SCAN_DEPTH, module_search_paths
from .manifest import DEFAULTS, ManifestDefaults

MAX_SCAN_DEPTH = 8  # deeper trees are unusual and slow to walk (04-hub "Configuration")
PACKS_RECORD = "packs.json"  # under state_dir(), written by 06-core-cli's installer (RESOLUTIONS C8)


def _string_list(value: object) -> str | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return "expected a list of strings"
    return None


def _scan_depth(value: object) -> str | None:
    if not isinstance(value, int) or not 1 <= value <= MAX_SCAN_DEPTH:
        return f"must be 1 to {MAX_SCAN_DEPTH}"
    return None


HUB_TABLE = config.register(
    config.Table(
        name="hub",
        owner="hal-hub",
        keys=(
            config.Key("module_paths", list, [], check=_string_list, doc="Directories scanned for modules"),
            config.Key("ignore_paths", list, [], check=_string_list, doc="Directories never scanned"),
            config.Key("disabled", list, [], check=_string_list, doc="Modules to leave out"),
            config.Key("pinned", list, [], check=_string_list, doc="Tools always offered to clients"),
            config.Key("use_entry_points", bool, True, doc="Discover installed packs by entry point"),
            config.Key("scan_depth", int, DEFAULT_SCAN_DEPTH, check=_scan_depth, doc="How deep to scan"),
            config.Key("runner", dict, {}, doc="The [hub.runner] table"),
            config.Key("mcp", dict, {}, doc="The [hub.mcp] table"),
        ),
    )
)


@dataclass(frozen=True)
class HubConfig:
    """Resolved `[hub]` settings plus the search paths and limits derived from them."""

    module_paths: tuple[Path, ...] = ()  # `[hub].module_paths` then `HAL_MODULE_PATH`
    ignore_paths: tuple[Path, ...] = ()
    disabled: frozenset[str] = frozenset()
    pinned: tuple[str, ...] = ()
    use_entry_points: bool = True
    scan_depth: int = DEFAULT_SCAN_DEPTH
    installed: Path | None = None  # the packs.json path; None skips that source
    manifest_defaults: ManifestDefaults = field(default=DEFAULTS)

    @classmethod
    def load(cls, path: Path | None = None, *, environ: Mapping[str, str] | None = None) -> HubConfig:
        """Reads `config.toml` (default `halkit.paths.config_file()`); raises `halkit.config.ConfigError`."""
        env = os.environ if environ is None else environ
        values = config.load(HUB_TABLE, path=path, environ=env)
        return cls(
            module_paths=tuple(module_search_paths(values["module_paths"], env)),
            ignore_paths=tuple(Path(p).expanduser() for p in values["ignore_paths"]),
            disabled=frozenset(values["disabled"]),
            pinned=tuple(values["pinned"]),
            use_entry_points=bool(values["use_entry_points"]),
            scan_depth=int(values["scan_depth"]),
            installed=paths.state_dir() / PACKS_RECORD,
        )
