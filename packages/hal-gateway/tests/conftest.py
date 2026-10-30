"""Shared fixtures: every gateway test gets a private state directory."""

import pytest


@pytest.fixture(autouse=True)
def _private_user_dirs(tmp_path, monkeypatch):
    """Keeps the request log, the model manifest and gpu-light out of the real user directories."""
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "userconfig" / "config.toml"))
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    # a developer machine may run a real gpu-light daemon; a test must never claim or register there
    monkeypatch.setenv("GPU_LIGHT_SOCKET", str(tmp_path / "no-gpu-light" / "daemon.sock"))
    monkeypatch.setenv("GPU_LIGHT_DIR", str(tmp_path / "no-gpu-light-dir"))
