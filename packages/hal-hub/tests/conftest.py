"""Shared fixtures for hal-hub's tests: an isolated Hal home and the synthetic packs."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from halkit.testing import TempHalHome

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_hubkit() -> ModuleType:
    """Loads `hubkit.py` as module `hubkit`: with `--import-mode=importlib` the tests directory
    is not on `sys.path`, and test files import the shared helpers by that name."""
    spec = importlib.util.spec_from_file_location("hubkit", Path(__file__).resolve().parent / "hubkit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hubkit"] = module
    spec.loader.exec_module(module)
    return module


hubkit = _load_hubkit()


@pytest.fixture(autouse=True)
def hal_home() -> Iterator[TempHalHome]:
    """Every hub test runs against a throwaway config, state, data and runtime directory."""
    with TempHalHome() as home:
        yield home


@pytest.fixture
def fixtures() -> Path:
    """The directory of synthetic packs (see `fixtures/README.md`)."""
    return FIXTURES


@pytest.fixture
def demo_token(hal_home, tmp_path) -> str:
    """Configures the `demo` pack's declared secret and returns its value."""
    path = tmp_path / "demo-token"
    path.write_text(hubkit.DEMO_TOKEN + "\n")
    path.chmod(0o600)
    hal_home.config.write_text(f'[demo]\ntoken_file = "{path}"\n')
    return hubkit.DEMO_TOKEN


@pytest.fixture
def make_hub(demo_token, tmp_path):
    """Builds a `Hub` over the given fixture packs (default `demo`) with a `SpyRunner`."""
    from halhub.config import HubConfig
    from halhub.hub import Hub
    from halhub.registry import Registry

    hubs = []

    def make(*packs: str, runner=None, paths: tuple[Path, ...] = (), **kwargs):
        dirs = tuple(FIXTURES / p for p in (packs or ("demo",))) + tuple(paths)
        config = HubConfig(module_paths=dirs, use_entry_points=False)
        registry = Registry.load(config, version="0.1.0")
        hub = Hub(registry, runner=runner or hubkit.SpyRunner(), config=config, home=tmp_path, **kwargs)
        hubs.append(hub)
        return hub

    yield make
    for hub in hubs:
        hub.close()
