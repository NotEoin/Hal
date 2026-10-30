"""Shared fakes for the hal-remote tests."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

import pytest


class FakeRunner:
    """A `halkit.proc.run`-compatible fake: maps the program name to (returncode, stdout)."""

    def __init__(self, outputs: Mapping[str, tuple[int, str] | None] | None = None) -> None:
        self.outputs = dict(outputs or {})
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout, check=True, **_kw):
        self.calls.append(list(argv))
        assert timeout > 0
        out = self.outputs.get(argv[0] if argv[0] != "sudo" else "sudo")
        if out is None:
            raise FileNotFoundError(argv[0])
        code, text = out
        return subprocess.CompletedProcess(argv, code, text.encode(), b"")


@pytest.fixture
def fake_runner() -> type[FakeRunner]:
    return FakeRunner
