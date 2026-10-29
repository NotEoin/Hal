"""The registry: discovered manifests turned into modules with a status, tools and packs.

04-hub.md, "Discovery and registry", step by step: collect candidates, parse them, settle
each module's pack, refuse duplicate names, apply `[hub].disabled`, then check
`requires_hal` and `requires` in dependency order. Design decisions:

- A duplicate module name stops the whole registry (`DuplicateModuleError`, D3): silent
  first-wins hides which code runs. `collect_errors=True` (for `doctor`) turns it into
  problems and leaves every module of that name out.
- A module's pack is the installed-pack record's when the record lists it, and its module
  name otherwise, whatever the manifest declares (REQ-HUB-63): `pack` feeds the approval key
  and the `[packs.<p>]` policy rules, so a third-party pack must not claim another's.
- One broken manifest drops only its own module (REQ-HUB-10). Problems are logged, never
  printed: stdout belongs to the CLI and the MCP stdio stream.
- The built-in `hub` module (`halhub/builtin/hal-module.toml`) is always present and never
  discovered. Any other manifest named `hub` is skipped: the name and its `hal_` tool prefix
  belong to hub code. A built-in manifest that does not parse is a hub bug and raises.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .builtin import MANIFEST as BUILTIN_MANIFEST
from .config import HubConfig
from .discovery import Candidate, Problem, discover
from .manifest import (
    BUILTIN_MODULE,
    ManifestError,
    ModuleSpec,
    ToolSpec,
    load_manifest,
    parse_requirement,
    version_satisfies,
)

log = logging.getLogger(__name__)

HUB_DISTRIBUTION = "hal-hub"  # the built-in module's version is the installed hub's

Status = Literal["ok", "disabled", "unavailable"]

__all__ = [
    "DuplicateModuleError",
    "ModuleState",
    "PackInfo",
    "Problem",
    "Registry",
    "RegistryError",
    "hub_version",
    "packs",
]


class RegistryError(Exception):
    """The registry cannot be built."""


class DuplicateModuleError(RegistryError):
    """Two manifests declare one module name; nothing loads until one goes (D3)."""

    def __init__(self, duplicates: tuple[tuple[str, Path, str], ...]) -> None:
        self.duplicates = duplicates
        lines = [f"  {name}: {path} ({source})" for name, path, source in duplicates]
        fix = "disable one with [hub].ignore_paths or uninstall it"
        super().__init__("duplicate module names:\n" + "\n".join(lines) + f"\n{fix}")


@dataclass(frozen=True)
class ModuleState:
    """A module and whether its tools can be used."""

    spec: ModuleSpec
    status: Status
    reason: str = ""


@dataclass(frozen=True)
class PackInfo:
    """One pack as 06-core-cli needs it for units, launchers and `doctor`."""

    name: str
    modules: tuple[str, ...]
    roots: tuple[Path, ...]  # manifest directories
    interpreter: tuple[str, ...] | None  # packs.json argv; None: the hub's interpreter
    enabled: bool  # False when every module is in [hub].disabled


def hub_version() -> str:
    """The running hub's version, from the package that holds this module."""
    from . import __version__  # deferred: the package `__init__` imports this module's siblings

    return __version__


class Registry:
    """Every discovered module with its status; only `ok` modules offer tools."""

    def __init__(self, modules: Mapping[str, ModuleState], problems: Iterable[Problem]) -> None:
        self.modules: Mapping[str, ModuleState] = dict(sorted(modules.items()))
        self.problems: tuple[Problem, ...] = tuple(problems)
        self._tools: dict[str, tuple[ToolSpec, ModuleState]] = {}
        for state in self.modules.values():
            for tool in state.spec.tools:
                self._tools[tool.name] = (tool, state)

    @classmethod
    def load(
        cls,
        config: HubConfig | None = None,
        *,
        collect_errors: bool = False,
        entry_points: Callable[[], Iterable[importlib.metadata.EntryPoint]] | None = None,
        version: str | None = None,
    ) -> Registry:
        """Discovers and checks every module; raises `DuplicateModuleError` unless `collect_errors`.

        `entry_points` and `version` replace the real entry-point reader and hub version in tests.
        """
        config = config if config is not None else HubConfig.load()
        problems: list[Problem] = []
        candidates = discover(
            paths=config.module_paths,
            use_entry_points=config.use_entry_points,
            installed=config.installed,
            ignore=config.ignore_paths,
            depth=config.scan_depth,
            entry_points=entry_points,
            problems=problems,
        )
        builtin = load_manifest(
            BUILTIN_MANIFEST,
            source="builtin",
            distribution=HUB_DISTRIBUTION,
            defaults=config.manifest_defaults,
        )
        specs = [builtin] + [spec for c in candidates if (spec := _parse(c, config, problems)) is not None]
        specs = _without_duplicates(specs, problems, collect_errors)
        states = _check(specs, config.disabled, version or hub_version(), problems)
        return cls(states, problems)

    def tool(self, name: str) -> ToolSpec | None:
        """The tool called `name` when its module is `ok`, else None."""
        found = self._tools.get(name)
        return found[0] if found and found[1].status == "ok" else None

    def lookup(self, name: str) -> tuple[ToolSpec, ModuleState] | None:
        """The tool and its module state whatever the status, so a call can say why it is unavailable."""
        return self._tools.get(name)

    def module_of(self, tool: ToolSpec) -> ModuleSpec:
        """The module that declares `tool`; raises `KeyError` for a tool from elsewhere."""
        return self.modules[tool.module].spec

    def tools(self) -> list[ToolSpec]:
        """Tools of `ok` modules, sorted by name."""
        return sorted((t for t, s in self._tools.values() if s.status == "ok"), key=lambda t: t.name)

    def packs(self) -> list[PackInfo]:
        """Every pack with a module in the registry, sorted by name."""
        grouped: dict[str, list[ModuleState]] = {}
        for state in self.modules.values():
            grouped.setdefault(state.spec.pack, []).append(state)
        result = []
        for name, states in sorted(grouped.items()):
            interpreters = [s.spec.interpreter for s in states if s.spec.interpreter]
            result.append(
                PackInfo(
                    name=name,
                    modules=tuple(s.spec.name for s in states),
                    roots=tuple(s.spec.root for s in states),
                    interpreter=interpreters[0] if interpreters else None,
                    enabled=any(s.status != "disabled" for s in states),
                )
            )
        return result


def packs() -> list[PackInfo]:
    """`Registry.load().packs()`: what 06-core-cli calls for units and `doctor`."""
    return Registry.load().packs()


def _problem(problems: list[Problem], path: Path, message: str) -> None:
    log.warning("%s: %s", path, message)
    problems.append(Problem(path, message))


def _parse(candidate: Candidate, config: HubConfig, problems: list[Problem]) -> ModuleSpec | None:
    try:
        spec = load_manifest(
            candidate.manifest,
            source=candidate.source,
            distribution=candidate.distribution,
            interpreter=candidate.interpreter,
            defaults=config.manifest_defaults,
        )
    except ManifestError as exc:
        _problem(problems, exc.path, f"skipped: {exc.key + ': ' if exc.key else ''}{exc.message}")
        return None
    if spec.name == BUILTIN_MODULE and spec.schema == 1 and candidate.source == "path":
        log.warning("%s: skipped the v1 hub module (the old core/hal-hub checkout)", spec.manifest)
        return None
    if spec.name == BUILTIN_MODULE:
        _problem(problems, spec.manifest, f"skipped: the module name {BUILTIN_MODULE!r} is the hub's own")
        return None
    return _settle_pack(spec, candidate, problems)


def _settle_pack(spec: ModuleSpec, candidate: Candidate, problems: list[Problem]) -> ModuleSpec | None:
    """Applies REQ-HUB-63: the record's pack, else the module name."""
    declared = spec.declared_pack or spec.name
    if candidate.pack is not None:
        if declared != candidate.pack:
            _problem(
                problems,
                spec.manifest,
                f"excluded: declares pack {declared!r} but the installed-pack record files it under "
                f"{candidate.pack!r}",
            )
            return None
        pack = candidate.pack
    else:
        pack = spec.name
        if declared != pack:
            _problem(
                problems,
                spec.manifest,
                f"declares pack {declared!r}, which only an installed pack may claim; using {pack!r}",
            )
    if pack == spec.pack:
        return spec
    tools = tuple(dataclasses.replace(t, pack=pack) for t in spec.tools)
    return dataclasses.replace(spec, pack=pack, tools=tools)


def _without_duplicates(
    specs: list[ModuleSpec], problems: list[Problem], collect_errors: bool
) -> list[ModuleSpec]:
    by_name: dict[str, list[ModuleSpec]] = {}
    for spec in specs:
        by_name.setdefault(spec.name, []).append(spec)
    clashes = {name: group for name, group in by_name.items() if len(group) > 1}
    if not clashes:
        return specs
    duplicates = tuple((s.name, s.manifest, s.source) for group in clashes.values() for s in group)
    if not collect_errors:
        raise DuplicateModuleError(duplicates)
    for name, path, source in duplicates:
        _problem(problems, path, f"duplicate module name {name!r} ({source}); every copy is left out")
    return [s for s in specs if s.name not in clashes]


def _check(
    specs: list[ModuleSpec], disabled: frozenset[str], version: str, problems: list[Problem]
) -> dict[str, ModuleState]:
    states: dict[str, ModuleState] = {}
    for spec in specs:
        if spec.requires_hal and not version_satisfies(version, spec.requires_hal):
            _problem(
                problems,
                spec.manifest,
                f"rejected: module {spec.name} needs hal-hub {spec.requires_hal}, this is hal-hub {version}",
            )
            continue
        if spec.name in disabled:
            states[spec.name] = ModuleState(spec, "disabled", "listed in [hub].disabled")
        else:
            states[spec.name] = ModuleState(spec, "ok")
    _resolve_requires(states)
    return states


def _resolve_requires(states: dict[str, ModuleState]) -> None:
    """Marks modules whose `requires` are unmet, including every module in a cycle, `unavailable`."""
    done: set[str] = set()

    def visit(name: str, stack: list[str]) -> None:
        if name in done:
            return
        if name in stack:
            cycle = stack[stack.index(name) :] + [name]
            for member in cycle[:-1]:
                state = states[member]
                states[member] = dataclasses.replace(
                    state, status="unavailable", reason=f"requires cycle: {' -> '.join(cycle)}"
                )
                done.add(member)
            return
        state = states[name]
        stack.append(name)
        reason = ""
        for requirement in state.spec.requires:
            target, specifier = parse_requirement(requirement)
            if target in states:
                visit(target, stack)
            if name in done:  # marked by a cycle found below
                stack.pop()
                return
            reason = reason or _unmet(requirement, target, specifier, states)
        stack.pop()
        if reason and state.status == "ok":
            states[name] = dataclasses.replace(state, status="unavailable", reason=reason)
        done.add(name)

    for name in sorted(states):
        visit(name, [])


def _unmet(requirement: str, target: str, specifier: str, states: Mapping[str, ModuleState]) -> str:
    other = states.get(target)
    if other is None:
        return f"requires {requirement}, which is not installed"
    if other.status == "disabled":
        return f"requires {requirement}, which is disabled"
    if other.status == "unavailable":
        return f"requires {requirement}, which is unavailable ({other.reason})"
    if not version_satisfies(other.spec.version, specifier):
        return f"requires {requirement}, found {target} {other.spec.version}"
    return ""
