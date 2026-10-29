"""Tool functions of the synthetic v1 `legacy` module."""

from __future__ import annotations


def add(a: int, b: int) -> int:
    """Returns `a + b`."""
    return a + b


def tag(tags: list[str] | None = None, extra: dict | None = None) -> dict:
    """Echoes what it received, so tests can see list splitting and v1 object parsing."""
    return {"tags": tags, "extra": extra}
