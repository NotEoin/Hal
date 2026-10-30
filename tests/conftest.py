"""Shared fixtures for the cross-package tests.

It puts this folder on the import path so the tests can share `test_layering`, and it moves
every Hal path into a temporary home so a test never reads or writes the live runtime dir.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def pinned_runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """A private runtime dir for every test (the live one may have the wrong mode)."""
    runtime = tmp_path_factory.mktemp("rt")
    runtime.chmod(0o700)  # halkit refuses any other mode
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(runtime))
