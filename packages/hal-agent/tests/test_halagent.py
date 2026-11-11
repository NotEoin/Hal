"""Smoke test for `halagent`: the package imports and carries the workspace version."""

import halagent


def test_package_imports_and_has_a_version():
    assert halagent.__version__ == "0.2.0"
