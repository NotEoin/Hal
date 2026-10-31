"""Shared fixtures: a scripted fake OpenAI server on loopback."""

from collections.abc import Iterator

import pytest

from halkit.testing.model_server import FakeModelServer


@pytest.fixture
def server() -> Iterator[FakeModelServer]:
    """A started fake model server; queue replies with `server.script(...)`."""
    with FakeModelServer() as srv:
        srv.model_list_requests = []  # the agent asks for the model list once per start-up
        record = srv._record

        def skip_model_list(handler, body) -> None:
            if handler.path == "/v1/models":
                srv.model_list_requests.append(handler.path)
            else:
                record(handler, body)

        srv._record = skip_model_list
        yield srv


@pytest.fixture(autouse=True)
def isolated_state(tmp_path_factory, monkeypatch) -> None:
    """Keeps session transcripts of every test out of the real state directory."""
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path_factory.mktemp("state")))


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch) -> None:
    """Keeps the user's real config, AGENTS.md and model profiles out of every test."""
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path_factory.mktemp("config") / "config.toml"))


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch) -> None:
    """Makes the agent's reply timing constant (0.5 s per call) so tokens per second is stable."""
    ticks = iter(x * 0.5 for x in range(10**6))
    monkeypatch.setattr("halagent.loop._now", lambda: next(ticks))
