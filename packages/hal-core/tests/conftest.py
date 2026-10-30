"""Puts this folder on the import path so tests can share `halcore_env` under pytest.

`python -m unittest discover -s packages/hal-core/tests` already does this by itself.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def pinned_runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """A private runtime dir for every test, so none touches the live one."""
    runtime = tmp_path_factory.mktemp("rt")
    runtime.chmod(0o700)  # halkit refuses any other mode
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(runtime))
