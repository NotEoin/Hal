"""Container and namespace backends that turn a `LaunchSpec` into a `Launch` for one box."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Launch:
    """How halcore starts the box: the argv, plus env and cwd only for backends that run on the host."""

    argv: Sequence[str]
    env: Mapping[str, str] | None = None  # None: the argv carries its own environment
    cwd: str | None = None
    new_session: bool = False  # `start_new_session=True` for a host process
