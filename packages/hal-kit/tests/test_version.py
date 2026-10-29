"""Tests for `halkit.version`."""

import halkit
from halkit import version


def test_package_exposes_the_version_string():
    assert halkit.__version__ == version.__version__


def test_versions_lists_hal_kit_itself():
    assert version.versions()["hal-kit"] == version.__version__


def test_versions_lists_only_hal_distributions():
    assert all(name.startswith("hal-") for name in version.versions())
