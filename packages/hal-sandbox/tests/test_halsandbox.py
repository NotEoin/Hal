"""Smoke test for `halsandbox`: the package imports and carries the workspace version."""

import halsandbox


def test_package_imports_and_has_a_version():
    assert halsandbox.__version__ == "0.1.0"
