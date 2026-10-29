"""All packages carry one version (REQ-SYS-18).

The version of each package is read from the file its `[tool.hatch.version]` points at, so the
test needs no installed package and cannot pass on stale install metadata.
"""

from __future__ import annotations

import re
import tomllib

import pytest
from test_layering import REPO

VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)
PEP440_RE = re.compile(r"^\d+\.\d+\.\d+(\.dev\d+)?$")  # a release or a dev build of one


def package_versions() -> dict[str, str]:
    found = {}
    for path in sorted(REPO.glob("packages/*/pyproject.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert "version" in data["project"].get("dynamic", []), f"{path}: version must be dynamic"
        source = path.parent / data["tool"]["hatch"]["version"]["path"]
        match = VERSION_RE.search(source.read_text(encoding="utf-8"))
        assert match, f"{source}: no __version__ assignment"
        found[data["project"]["name"]] = match.group(1)
    return found


VERSIONS = package_versions()


def test_every_package_has_a_version():
    assert len(VERSIONS) == 7


def test_all_packages_carry_the_same_version():
    assert len(set(VERSIONS.values())) == 1, VERSIONS


@pytest.mark.parametrize("dist", sorted(VERSIONS))
def test_version_is_a_release_or_dev_release(dist):
    assert PEP440_RE.match(VERSIONS[dist]), VERSIONS[dist]


def test_hal_kit_version_helper_reports_the_same_version():
    from halkit.version import __version__

    assert __version__ == VERSIONS["hal-kit"]
