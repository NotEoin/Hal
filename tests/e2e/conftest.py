"""Keeps the e2e tests on their own XDG directories when another package's tests were collected first.

`halcore_env` sets `HAL_CONFIG`, `HAL_STATE` and `HAL_DATA` when pytest imports it, and those win
over the `XDG_*` variables the e2e tests set, so a combined `pytest -m docker` run lost the audit
log and the isolated copy.
"""

import pytest

OVERRIDES = ("HAL_CONFIG", "HAL_STATE", "HAL_DATA", "HAL_STATE_DIR", "HAL_DATA_DIR", "HAL_RUNTIME_DIR")


@pytest.fixture(autouse=True)
def _xdg_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in OVERRIDES:
        monkeypatch.delenv(name, raising=False)
