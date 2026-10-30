"""Shared fixtures: every gateway test gets a private state directory."""

import pytest


@pytest.fixture(autouse=True)
def _private_state_dir(tmp_path, monkeypatch):
    """Keeps the request log out of the real state directory."""
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
