"""Package metadata: Python floor, third-party allowlist and internal dependencies.

REQ-SYS-02: every package declares `requires-python = ">=3.11"`.
REQ-SYS-03: runtime dependencies are a subset of the "Third-party dependencies" table of
docs/design/00-overview.md, and hal-kit has none. Any other dependency needs a reason in the
spec and a line in `THIRD_PARTY` below.
Internal `hal-*` dependencies follow the layering table and cover every import edge.
01 REQ-SEC-36: the guard's model stack stays out of every package and the workspace lock.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from test_layering import ALLOWED, REPO, scan

# distribution -> allowed third-party runtime dependencies (normalised names)
THIRD_PARTY: dict[str, frozenset[str]] = {
    "hal-kit": frozenset(),
    "hal-hub": frozenset({"mcp"}),
    "hal-agent": frozenset({"mcp"}),
    "hal-sandbox": frozenset({"mcp"}),
    "hal-gateway": frozenset(),
    "hal-remote": frozenset(),
    "hal-core": frozenset({"rich", "prompt-toolkit", "textual"}),
}
DIST_OF = {
    "halkit": "hal-kit",
    "halhub": "hal-hub",
    "halagent": "hal-agent",
    "halsandbox": "hal-sandbox",
    "halgateway": "hal-gateway",
    "halremote": "hal-remote",
    "halcore": "hal-core",
}
GUARD_STACK = frozenset({"torch", "transformers", "tokenizers", "safetensors"})  # guard image only
MCP_SPEC = ">=2.3,<3"  # the official SDK range the brief fixes
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dependency_name(requirement: str) -> str:
    match = NAME_RE.match(requirement.strip())
    assert match, f"cannot read dependency {requirement!r}"
    return normalise(match.group(0))


def load_projects(repo: Path) -> dict[str, dict]:
    """Maps distribution name to its parsed `pyproject.toml`."""
    projects = {}
    for path in sorted(repo.glob("packages/*/pyproject.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        projects[normalise(data["project"]["name"])] = data
    return projects


def split_dependencies(project: dict) -> tuple[set[str], set[str]]:
    """Returns `(internal hal-* names, third-party names)` from `[project].dependencies`."""
    names = [dependency_name(r) for r in project["project"].get("dependencies", [])]
    return {n for n in names if n.startswith("hal-")}, {n for n in names if not n.startswith("hal-")}


PROJECTS = load_projects(REPO)


def test_the_workspace_has_exactly_the_seven_documented_packages():
    assert set(PROJECTS) == set(THIRD_PARTY)


@pytest.mark.parametrize("dist", sorted(PROJECTS))
def test_package_requires_python_3_11_or_newer(dist):
    assert PROJECTS[dist]["project"]["requires-python"] == ">=3.11"


@pytest.mark.parametrize("dist", sorted(PROJECTS))
def test_runtime_dependencies_are_in_the_allowlist(dist):
    _, third_party = split_dependencies(PROJECTS[dist])
    assert third_party <= THIRD_PARTY[dist], (
        f"{dist} has unlisted dependencies {third_party - THIRD_PARTY[dist]}"
    )


@pytest.mark.parametrize("dist", sorted(PROJECTS))
def test_guard_model_stack_is_no_package_dependency(dist):
    project = PROJECTS[dist]["project"]
    extras = [r for group in project.get("optional-dependencies", {}).values() for r in group]
    names = {dependency_name(r) for r in [*project.get("dependencies", []), *extras]}
    assert not names & GUARD_STACK


def test_guard_model_stack_is_not_in_the_workspace_lock():
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    assert not {normalise(p["name"]) for p in lock.get("package", [])} & GUARD_STACK


def test_hal_kit_has_no_dependencies_at_all():
    assert PROJECTS["hal-kit"]["project"].get("dependencies", []) == []


@pytest.mark.parametrize("dist", ["hal-hub", "hal-agent", "hal-sandbox"])
def test_mcp_is_pinned_to_the_documented_range_when_declared(dist):
    requirements = [
        r for r in PROJECTS[dist]["project"].get("dependencies", []) if dependency_name(r) == "mcp"
    ]
    assert all(r.replace(" ", "").endswith(MCP_SPEC) for r in requirements)


@pytest.mark.parametrize("dist", sorted(PROJECTS))
def test_internal_dependencies_follow_the_layering_table(dist):
    internal, _ = split_dependencies(PROJECTS[dist])
    import_name = next(k for k, v in DIST_OF.items() if v == dist)
    allowed = {DIST_OF[p] for p in ALLOWED[import_name]}
    assert internal <= allowed, f"{dist} depends on {internal - allowed}"


def test_every_import_edge_is_declared_as_a_dependency():
    edges, _ = scan(REPO)
    for importer, imported in edges.items():
        internal, _ = split_dependencies(PROJECTS[DIST_OF[importer]])
        missing = {DIST_OF[i] for i in imported} - internal
        assert not missing, f"{DIST_OF[importer]} imports {missing} but does not depend on it"


@pytest.mark.parametrize("dist", sorted(PROJECTS))
def test_internal_dependencies_resolve_to_the_workspace(dist):
    internal, _ = split_dependencies(PROJECTS[dist])
    sources = PROJECTS[dist].get("tool", {}).get("uv", {}).get("sources", {})
    assert all(sources.get(name) == {"workspace": True} for name in internal)


def test_dependency_name_ignores_version_markers_and_extras():
    assert dependency_name("Prompt_Toolkit>=3.0,<4; python_version >= '3.11'") == "prompt-toolkit"
    assert dependency_name("mcp[cli] >=2.3,<3") == "mcp"
