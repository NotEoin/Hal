"""Shared fixtures for hal-hub's tests: an isolated Hal home and the synthetic packs."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from halkit.testing import TempHalHome

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def hal_home() -> Iterator[TempHalHome]:
    """Every hub test runs against a throwaway config, state, data and runtime directory."""
    with TempHalHome() as home:
        yield home


@pytest.fixture
def fixtures() -> Path:
    """The directory of synthetic packs (see `fixtures/README.md`)."""
    return FIXTURES
