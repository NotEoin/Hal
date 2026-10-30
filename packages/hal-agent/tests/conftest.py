"""Shared fixtures: a scripted fake OpenAI server on loopback."""

from collections.abc import Iterator

import pytest

from halkit.testing.model_server import FakeModelServer


@pytest.fixture
def server() -> Iterator[FakeModelServer]:
    """A started fake model server; queue replies with `server.script(...)`."""
    with FakeModelServer() as srv:
        yield srv


@pytest.fixture(autouse=True)
def isolated_state(tmp_path_factory, monkeypatch) -> None:
    """Keeps session transcripts of every test out of the real state directory."""
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path_factory.mktemp("state")))


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch) -> None:
    """Keeps the user's real config, AGENTS.md and model profiles out of every test."""
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path_factory.mktemp("config") / "config.toml"))
