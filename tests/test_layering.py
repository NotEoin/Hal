"""Package layering: which Hal package may import which (REQ-SYS-04, REQ-SYS-05).

`ALLOWED` encodes the "Allowed imports" table of docs/design/00-overview.md. The scan reads
every `src/` file of every package with `ast`, so imports inside functions count too, and fails
with the file and line of each violation. The same scan finds import cycles.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# importer -> the Hal packages it may import. Single source of truth: 00-overview "Allowed imports".
ALLOWED: dict[str, frozenset[str]] = {
    "halkit": frozenset(),
    "halhub": frozenset({"halkit"}),
    "halagent": frozenset({"halkit"}),
    "halsandbox": frozenset({"halkit", "halhub"}),
    "halgateway": frozenset({"halkit"}),
    "halremote": frozenset({"halkit"}),
    "halcore": frozenset({"halkit", "halhub", "halagent", "halsandbox", "halgateway", "halremote"}),
}

# Nothing in the monorepo may import these: gpu-light is reached by socket or CLI only (A6), packs
# are reached only through the hub's out-of-process runner (A3). The `hal_<pack>` names are the
# pack shims of 08-packs-and-flagships; the rest are the module names of today's extensions.
PACKS = ("organiser", "life", "comms", "online", "desktop", "system", "work", "jobs")
FORBIDDEN = frozenset(
    {"gpu_light"}
    | {f"hal_{pack}" for pack in PACKS}
    | {
        "halbrief", "halcal", "halclock", "halcontacts", "haldev", "haldevices", "halfeeds", "halfiles",
        "halhabits", "haljobs", "hallang", "halledger", "halmail", "halmem", "halmodels", "halnet",
        "halnotify", "halpkgs", "halphone", "halpower", "halrgb", "halroutines", "halshell", "halsys",
        "haltasks", "haltelegram", "halweb", "halvoice",
    }
)  # fmt: skip

TESTING_MODULE = "halkit.testing"


def source_files(repo: Path) -> Iterator[tuple[str, Path]]:
    """Yields `(import_name, path)` for each `.py` file under `packages/*/src/<import_name>/`."""
    for src in sorted(repo.glob("packages/*/src")):
        for package_dir in sorted(p for p in src.iterdir() if p.is_dir() and not p.name.startswith(".")):
            for path in sorted(package_dir.rglob("*.py")):
                yield package_dir.name, path


def module_parts(path: Path, package: str) -> list[str]:
    """The dotted module path of `path` as a list, for resolving relative imports."""
    src = next(p for p in path.parents if p.name == "src")
    parts = list(path.relative_to(src).with_suffix("").parts)
    assert parts[0] == package
    return parts[:-1] if parts[-1] == "__init__" else parts


def imported_names(path: Path, package: str) -> list[tuple[int, str]]:
    """Every absolute module name imported anywhere in `path`, with its line number."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    here = module_parts(path, package)
    is_init = path.name == "__init__.py"
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = here if is_init else here[:-1]
                base_parts = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*base_parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            found.append((node.lineno, base))
            found.extend((node.lineno, f"{base}.{alias.name}") for alias in node.names)
    return found


def scan(repo: Path) -> tuple[dict[str, set[str]], list[str]]:
    """Returns the observed `{importer: {imported}}` edges and a list of violation messages."""
    edges: dict[str, set[str]] = {}
    problems: list[str] = []
    for package, path in source_files(repo):
        where = path.relative_to(repo)
        edges.setdefault(package, set())
        if package not in ALLOWED:
            problems.append(f"{where}: package {package} is not in the allowed-imports table")
            continue
        in_testing = package == "halkit" and "testing" in module_parts(path, package)[:2]
        for line, name in imported_names(path, package):
            top = name.split(".")[0]
            if top in ALLOWED and top != package:
                edges[package].add(top)
                if top not in ALLOWED[package]:
                    problems.append(f"{where}:{line}: {package} may not import {top}")
            if top in FORBIDDEN:
                problems.append(f"{where}:{line}: nothing in the monorepo may import {top}")
            if (name == TESTING_MODULE or name.startswith(TESTING_MODULE + ".")) and not in_testing:
                problems.append(f"{where}:{line}: {TESTING_MODULE} is for test files only")
    return edges, list(dict.fromkeys(problems))  # `from x import y` reports x and x.y: keep one


def find_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    """Returns one import cycle as `[a, b, ..., a]`, or None when the graph is acyclic."""
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt) == 1:
                return [*stack[stack.index(nxt) :], nxt]
            if nxt not in state:
                cycle = visit(nxt)
                if cycle:
                    return cycle
        stack.pop()
        state[node] = 2
        return None

    for start in sorted(edges):
        if start not in state:
            cycle = visit(start)
            if cycle:
                return cycle
    return None


def write_module(repo: Path, package: str, relative: str, text: str) -> None:
    path = repo / "packages" / f"hal-{package}" / "src" / package / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_the_monorepo_obeys_the_allowed_imports_table():
    _, problems = scan(REPO)
    assert problems == []


def test_the_real_import_graph_is_acyclic():
    edges, _ = scan(REPO)
    assert find_cycle(edges) is None


def test_the_allowed_table_itself_is_acyclic():
    assert find_cycle({k: set(v) for k, v in ALLOWED.items()}) is None


def test_every_package_in_the_workspace_is_in_the_table():
    on_disk = {name for name, _ in source_files(REPO)}
    assert on_disk <= set(ALLOWED)


def test_a_forbidden_package_edge_is_reported_with_file_and_line(tmp_path):
    write_module(tmp_path, "halkit", "x.py", "import os\nfrom halcore import cli\n")
    _, problems = scan(tmp_path)
    assert problems == ["packages/hal-halkit/src/halkit/x.py:2: halkit may not import halcore"]


def test_an_import_inside_a_function_is_still_caught(tmp_path):
    write_module(tmp_path, "halagent", "loop.py", "def run():\n    import halhub\n    return halhub\n")
    _, problems = scan(tmp_path)
    assert any(":2: halagent may not import halhub" in p for p in problems)


def test_an_allowed_edge_is_recorded_and_not_reported(tmp_path):
    write_module(tmp_path, "halsandbox", "broker.py", "from halhub import client\nimport halkit.paths\n")
    edges, problems = scan(tmp_path)
    assert problems == [] and edges["halsandbox"] == {"halhub", "halkit"}


def test_gpu_light_and_pack_imports_are_reported(tmp_path):
    write_module(tmp_path, "halgateway", "gpu.py", "import gpu_light\nfrom hal_organiser import PACK\n")
    _, problems = scan(tmp_path)
    assert len(problems) == 2 and all("nothing in the monorepo may import" in p for p in problems)


def test_testing_fakes_are_refused_in_production_code(tmp_path):
    write_module(tmp_path, "halhub", "x.py", "from halkit.testing import FakeClock\n")
    write_module(tmp_path, "halhub", "y.py", "from halkit import testing\n")
    _, problems = scan(tmp_path)
    assert len(problems) == 2 and all("test files only" in p for p in problems)


def test_a_relative_import_of_testing_inside_halkit_is_refused(tmp_path):
    write_module(tmp_path, "halkit", "__init__.py", "from . import testing\n")
    _, problems = scan(tmp_path)
    assert any("test files only" in p for p in problems)


def test_halkit_testing_may_import_its_own_siblings(tmp_path):
    write_module(tmp_path, "halkit", "testing/__init__.py", "from .clock import FakeClock\n")
    write_module(tmp_path, "halkit", "testing/clock.py", "from halkit import paths\n")
    _, problems = scan(tmp_path)
    assert problems == []


def test_a_package_missing_from_the_table_is_reported(tmp_path):
    write_module(tmp_path, "halnew", "__init__.py", "")
    _, problems = scan(tmp_path)
    assert any("not in the allowed-imports table" in p for p in problems)


def test_a_cycle_is_found_and_shown():
    cycle = find_cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}})
    assert cycle == ["a", "b", "c", "a"]


def test_a_chain_is_not_a_cycle():
    assert find_cycle({"a": {"b"}, "b": {"c"}, "c": set()}) is None
