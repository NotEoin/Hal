"""Smoke test for `halremote`: the package imports and carries the workspace version."""

import halremote


def test_package_imports_and_has_a_version():
    assert halremote.__version__ == "0.1.0.dev0"
