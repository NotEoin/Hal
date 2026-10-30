"""Shared fixtures: every gateway test gets a private state directory."""

import pytest


@pytest.fixture(autouse=True)
def _private_user_dirs(tmp_path, monkeypatch):
    """Keeps the request log and the model manifest out of the real user directories."""
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "userconfig" / "config.toml"))
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
