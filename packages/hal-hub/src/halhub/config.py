"""The hub's settings: the `[hub]` table of `config.toml`, read strictly through `halkit.config`.

04-hub.md, "Configuration", is the source of every key, default and reason. Design decisions:

- `[hub.runner]` and `[hub.mcp]` are their own tables (the runner's and the MCP server's
  settings); `[hub]` declares them as table-valued keys only so that `halkit.config`'s strict
  unknown-key check does not reject a file that has them.
- The runner's limits are also the manifest defaults (`max_timeout`, `max_result_chars`,
  `startup_timeout`, `max_workers_per_module`), so a manifest is checked against the caps the
  runner enforces.
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
from .manifest import DEFAULTS, MAX_WORKERS_LIMIT, ManifestDefaults
from .runner import RunnerConfig

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


def _positive(value: object) -> str | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        return "must be above 0"
    return None


def _workers(value: object) -> str | None:
    if not isinstance(value, int) or not 1 <= value <= MAX_WORKERS_LIMIT:
        return f"must be 1 to {MAX_WORKERS_LIMIT}"
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


_RUNNER = RunnerConfig()
RUNNER_TABLE = config.register(
    config.Table(
        name="hub.runner",
        owner="hal-hub",
        keys=(
            config.Key("max_workers_per_module", int, _RUNNER.max_workers_per_module, check=_workers),
            config.Key("idle_timeout", float, _RUNNER.idle_timeout, check=_positive),
            config.Key("startup_timeout", float, _RUNNER.startup_timeout, check=_positive),
            config.Key("queue_timeout", float, _RUNNER.queue_timeout, check=_positive),
            config.Key("kill_grace", float, _RUNNER.kill_grace, check=_positive),
            config.Key("max_calls_per_worker", int, _RUNNER.max_calls_per_worker, check=_positive),
            config.Key("max_timeout", float, _RUNNER.max_timeout, check=_positive),
            config.Key("max_result_chars", int, _RUNNER.max_result_chars, check=_positive),
            config.Key("inprocess_modules", list, [], check=_string_list, doc="Opt-in in-process modules"),
            config.Key("bwrap", str, _RUNNER.bwrap, choices=("auto", "off"), doc="Network drop for workers"),
        ),
    )
)


MCP_MODES = ("meta", "flat")
MCP_APPROVALS = ("auto", "elicit", "out-of-band", "deny")  # "callback" is the broker's, never configured
MCP_TABLE = config.register(
    config.Table(
        name="hub.mcp",
        owner="hal-hub",
        keys=(
            config.Key("mode", str, "meta", choices=MCP_MODES, doc="List the meta tools or every tool"),
            config.Key("approvals", str, "auto", choices=MCP_APPROVALS, doc="Where MCP approvals come from"),
        ),
    )
)


@dataclass(frozen=True)
class McpConfig:
    """`[hub.mcp]`: the MCP server's defaults, which `hal-hub mcp` options override."""

    mode: str = "meta"
    approvals: str = "auto"


def load_mcp_config(path: Path | None = None, *, environ: Mapping[str, str] | None = None) -> McpConfig:
    """Reads `[hub.mcp]`; raises `halkit.config.ConfigError`."""
    return McpConfig(**config.load(MCP_TABLE, path=path, environ=environ))


def load_runner_config(path: Path | None = None, *, environ: Mapping[str, str] | None = None) -> RunnerConfig:
    """Reads `[hub.runner]`; raises `halkit.config.ConfigError`."""
    values = dict(config.load(RUNNER_TABLE, path=path, environ=environ))
    values["inprocess_modules"] = frozenset(values["inprocess_modules"])
    return RunnerConfig(**values)


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
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    manifest_defaults: ManifestDefaults = field(default=DEFAULTS)

    @classmethod
    def load(cls, path: Path | None = None, *, environ: Mapping[str, str] | None = None) -> HubConfig:
        """Reads `config.toml` (default `halkit.paths.config_file()`); raises `halkit.config.ConfigError`."""
        env = os.environ if environ is None else environ
        values = config.load(HUB_TABLE, path=path, environ=env)
        runner = load_runner_config(path, environ=env)
        return cls(
            module_paths=tuple(module_search_paths(values["module_paths"], env)),
            ignore_paths=tuple(Path(p).expanduser() for p in values["ignore_paths"]),
            disabled=frozenset(values["disabled"]),
            pinned=tuple(values["pinned"]),
            use_entry_points=bool(values["use_entry_points"]),
            scan_depth=int(values["scan_depth"]),
            installed=paths.state_dir() / PACKS_RECORD,
            runner=runner,
            mcp=load_mcp_config(path, environ=env),
            manifest_defaults=ManifestDefaults(
                max_timeout=runner.max_timeout,
                max_result_chars=runner.max_result_chars,
                startup_timeout=runner.startup_timeout,
                max_workers=runner.max_workers_per_module,
            ),
        )
