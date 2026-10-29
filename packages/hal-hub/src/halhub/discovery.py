"""Find `hal-module.toml` manifests without importing any pack code.

Sources, in order (04-hub.md, "Discovery and registry"): entry points in the group
`hal.packs`, then the installed-pack record `state_dir()/packs.json` (06-core-cli owns its
schema), then directories scanned recursively (`[hub].module_paths`, then
`HAL_MODULE_PATH`). A manifest reached twice counts once, under its first source; when the
record also lists it, the record's pack name is kept, because REQ-HUB-63 checks the declared
pack against it.

Design decisions:

- An entry point's package is located with `importlib.util.find_spec` on its top-level name
  only, which finds the package directory without running its `__init__.py` (REQ-HUB-07,
  01 REQ-SEC-57); the rest of the dotted value is a plain directory path below it.
- An entry point value with `:attr`, or one naming a missing package, is a problem for that
  entry point only; the others still load.
- Scanning never follows symlinked directories, so a link loop or a link into `$HOME`
  cannot make discovery walk the whole disk.
- Unlike v1, `HAL_MODULE_PATH` adds to `module_paths` instead of replacing them.
- A broken `packs.json`, or an entry in it whose manifest or interpreter is missing, is a
  problem for what it covers, never a crash (REQ-HUB-65).
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import importlib.util
import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .manifest import MANIFEST_NAME

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "hal.packs"  # RESOLUTIONS C2
MODULE_PATH_ENV = "HAL_MODULE_PATH"
DEFAULT_SCAN_DEPTH = 4  # covers extensions/<repo> and packs/<pack>/<package> (04-hub "Path scanning")
ENTRY_POINT_DEPTH = 2  # manifests may sit up to two directories below an entry point's package
PACKS_RECORD_VERSION = 2  # the packs.json version that records modules (06-core-cli "Packs")
SKIP_DIRS = frozenset({"__pycache__", ".venv", "venv", "node_modules", "build", "dist", "site-packages"})

CandidateSource = Literal["entry_point", "installed", "path"]


@dataclass(frozen=True)
class Candidate:
    """A manifest found by one source, before it is parsed."""

    manifest: Path
    source: CandidateSource
    distribution: str | None = None
    entry_point: str | None = None
    interpreter: tuple[str, ...] | None = None  # from packs.json
    pack: str | None = None  # the pack name packs.json files the module under


@dataclass(frozen=True)
class Problem:
    """Something discovery or the registry skipped, with the file it concerns."""

    path: Path
    message: str


def module_search_paths(
    configured: Sequence[str | Path], environ: Mapping[str, str] | None = None
) -> list[Path]:
    """`[hub].module_paths` followed by the `HAL_MODULE_PATH` entries, with `~` expanded."""
    env = os.environ if environ is None else environ
    extra = [part for part in env.get(MODULE_PATH_ENV, "").split(os.pathsep) if part.strip()]
    return [Path(p).expanduser() for p in [*configured, *extra]]


def discover(
    *,
    paths: Sequence[Path],
    use_entry_points: bool = True,
    installed: Path | None = None,
    ignore: Sequence[Path] = (),
    depth: int = DEFAULT_SCAN_DEPTH,
    entry_points: Callable[[], Iterable[importlib.metadata.EntryPoint]] | None = None,
    problems: list[Problem] | None = None,
) -> list[Candidate]:
    """Returns every manifest the sources reach, deduplicated by resolved path, in source order.

    `installed` is the `packs.json` path; None skips that source. Problems (a bad entry
    point, a broken record, an unreadable directory) are logged and appended to `problems`
    when given; they never stop discovery.
    """
    sink = problems if problems is not None else []
    found: list[Candidate] = []
    seen: dict[Path, int] = {}

    def add(candidate: Candidate) -> None:
        key = candidate.manifest.resolve()
        if key in seen:
            first = found[seen[key]]
            if candidate.source == "installed" and first.pack is None:
                found[seen[key]] = dataclasses.replace(first, pack=candidate.pack)
            return
        seen[key] = len(found)
        found.append(candidate)

    if use_entry_points:
        for candidate in _from_entry_points(entry_points, sink):
            add(candidate)
    if installed is not None:
        for candidate in read_installed(Path(installed), sink):
            add(candidate)
    ignored = {Path(p).expanduser().resolve() for p in ignore}
    for base in paths:
        for manifest in scan(Path(base), depth=depth, ignore=ignored, problems=sink):
            add(Candidate(manifest=manifest, source="path"))
    return found


def _report(problems: list[Problem], path: Path, message: str) -> None:
    log.warning("%s: %s", path, message)
    problems.append(Problem(path, message))


def read_installed(path: Path, problems: list[Problem]) -> list[Candidate]:
    """Candidates from the installed-pack record: `packs.<p>.modules.<m>.{manifest,interpreter}`.

    A missing file means no installed packs. Anything malformed is a problem for the part it
    covers: the whole file, one pack or one module.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _report(problems, path, f"cannot read the installed-pack record: {exc}")
        return []
    if not isinstance(data, dict) or not isinstance(data.get("packs"), dict):
        _report(problems, path, "the installed-pack record has no packs table")
        return []
    if data.get("version") != PACKS_RECORD_VERSION:
        _report(
            problems,
            path,
            f"installed-pack record version {data.get('version')!r} is not {PACKS_RECORD_VERSION}; "
            "run `hal packs doctor` to upgrade it",
        )
        return []
    found = []
    for pack, record in sorted(data["packs"].items()):
        modules = record.get("modules") if isinstance(record, dict) else None
        if not isinstance(modules, dict):
            _report(problems, path, f"packs.{pack}.modules is missing or not a table")
            continue
        distribution = record.get("distribution")
        distribution = distribution if isinstance(distribution, str) else None
        for module, entry in sorted(modules.items()):
            candidate = _installed_module(path, pack, module, entry, distribution, problems)
            if candidate is not None:
                found.append(candidate)
    return found


def _installed_module(
    path: Path, pack: str, module: str, entry: object, distribution: str | None, problems: list[Problem]
) -> Candidate | None:
    where = f"packs.{pack}.modules.{module}"
    if not isinstance(entry, dict):
        _report(problems, path, f"{where} is not a table")
        return None
    manifest = entry.get("manifest")
    if not isinstance(manifest, str) or not Path(manifest).is_absolute():
        _report(problems, path, f"{where}.manifest must be an absolute path")
        return None
    if not Path(manifest).is_file():
        _report(problems, Path(manifest), f"listed in {path} ({where}) but missing")
        return None
    interpreter = entry.get("interpreter")
    if (
        not isinstance(interpreter, list)
        or not interpreter
        or not all(isinstance(a, str) for a in interpreter)
    ):
        _report(problems, path, f"{where}.interpreter must be a non-empty argv list")
        return None
    if Path(interpreter[0]).is_absolute() and not Path(interpreter[0]).exists():
        _report(problems, Path(interpreter[0]), f"interpreter of {where} in {path} is missing")
        return None
    return Candidate(
        manifest=Path(manifest),
        source="installed",
        distribution=distribution,
        interpreter=tuple(interpreter),
        pack=pack,
    )


def _default_entry_points() -> Iterable[importlib.metadata.EntryPoint]:
    return importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)


def _from_entry_points(
    entry_points: Callable[[], Iterable[importlib.metadata.EntryPoint]] | None, problems: list[Problem]
) -> list[Candidate]:
    found = []
    for ep in sorted((entry_points or _default_entry_points)(), key=lambda e: (e.name, e.value)):
        label = Path(f"<entry point {ep.name} = {ep.value}>")
        distribution = ep.dist.name if getattr(ep, "dist", None) is not None else None
        if ":" in ep.value:
            _report(problems, label, "the value must be a package name, not module:attribute")
            continue
        manifests = locate_package_manifests(ep.value)
        if manifests is None:
            _report(problems, label, f"package {ep.value!r} is not installed (or is a zip import)")
            continue
        if not manifests:
            _report(problems, label, f"no {MANIFEST_NAME} in package {ep.value!r} or two levels below it")
            continue
        for manifest in manifests:
            found.append(
                Candidate(
                    manifest=manifest, source="entry_point", distribution=distribution, entry_point=ep.name
                )
            )
    return found


def locate_package_manifests(value: str) -> list[Path] | None:
    """Manifests in package `value` (dotted) and two directory levels below, without importing it.

    Returns None when the top-level package cannot be found on the filesystem.
    """
    top, _, rest = value.partition(".")
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    manifests: list[Path] = []
    for location in spec.submodule_search_locations:
        directory = Path(location).joinpath(*rest.split(".")) if rest else Path(location)
        if directory.is_dir():
            manifests.extend(scan(directory, depth=ENTRY_POINT_DEPTH, ignore=set(), problems=[]))
    return manifests


def scan(base: Path, *, depth: int, ignore: set[Path], problems: list[Problem]) -> list[Path]:
    """Manifest paths under `base` down to `depth` levels, sorted; see the module header for the rules."""
    base = base.expanduser()
    if not base.is_dir():
        if base.exists():
            _report(problems, base, "module path is not a directory")
        else:
            log.info("module path %s does not exist; skipped", base)
        return []
    found: list[Path] = []
    _walk(base.resolve(), 0, depth, ignore, problems, found)
    return sorted(found)


def _walk(
    directory: Path, level: int, depth: int, ignore: set[Path], problems: list[Problem], out: list[Path]
) -> None:
    if directory in ignore:
        return
    manifest = directory / MANIFEST_NAME
    if manifest.is_file():
        out.append(manifest)
        return  # a module directory is not searched further
    if level >= depth:
        return
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError as exc:
        _report(problems, directory, f"cannot list: {exc.strerror or exc}")
        return
    for entry in entries:
        if entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        _walk(Path(entry.path), level + 1, depth, ignore, problems, out)
