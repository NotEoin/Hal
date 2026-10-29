"""Shared fixtures: a scripted fake OpenAI server on loopback."""

from collections.abc import Iterator

import pytest

from halkit.testing.model_server import FakeModelServer


@pytest.fixture
def server() -> Iterator[FakeModelServer]:
    """A started fake model server; queue replies with `server.script(...)`."""
    with FakeModelServer() as srv:
        yield srv
