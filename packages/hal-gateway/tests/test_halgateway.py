"""Smoke test for `halgateway`: the package imports and carries the workspace version."""

import halgateway


def test_package_imports_and_has_a_version():
    assert halgateway.__version__ == "0.2.0"
