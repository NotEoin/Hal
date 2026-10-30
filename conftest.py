"""Repository-wide pytest fixtures: every test gets a private runtime dir.

The live `$XDG_RUNTIME_DIR/hal` can carry a mode that halkit refuses (it must be 0700), which
would make unrelated tests fail; pinning `HAL_RUNTIME_DIR` keeps the suite hermetic.
"""

import pytest


@pytest.fixture(autouse=True)
def pinned_runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """A private runtime dir for every test, so none touches the live one."""
    runtime = tmp_path_factory.mktemp("rt")
    runtime.chmod(0o700)  # halkit refuses any other mode
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(runtime))
