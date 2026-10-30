"""Shared types and renderers for the box CLI profiles (02-sandbox REQ-BOX-60).

A profile turns a `BoxContext` into the argv, env and `$HOME` seed files that point one agent CLI at
the relay and the MCP bridge. Writers emit ASCII only: JSON with `ensure_ascii=True`, TOML and YAML
from renderers that quote every string, so a model alias cannot inject a key.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Protocol

from ..errors import SandboxError

DEFAULT_RELAY_URL = "http://127.0.0.1:8741"  # REQ-BOX-37: the relay's default port
DEFAULT_CONTEXT_WINDOW = 32768  # the old launcher's CLAUDE_CODE_MAX_CONTEXT_TOKENS default
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DUMMY_KEY = "hal-box"  # the forwarder strips it; it is not a secret
BRIDGE = "hal-mcp-bridge"


@dataclass(frozen=True)
class BoxContext:
    """Everything a profile needs to configure a CLI."""

    model: str
    context_window: int = DEFAULT_CONTEXT_WINDOW
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    relay_url: str = DEFAULT_RELAY_URL
    mcp_command: tuple[str, ...] = (BRIDGE,)
    auto_approve: bool = True
    user_args: tuple[str, ...] = ()
    home: str = "/home/hal"


@dataclass(frozen=True)
class BoxLaunch:
    """The argv, extra env (no secrets) and seed files (path under `$HOME` -> bytes) for one CLI."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    home_seed: Mapping[str, bytes]


class CliProfile(Protocol):
    """A per-CLI writer."""

    name: ClassVar[str]

    def launch(self, ctx: BoxContext) -> BoxLaunch:
        """Returns the launch description for `ctx`."""
        ...


def common_env() -> dict[str, str]:
    """The env every CLI gets: no tracking, a dummy API key."""
    return {"DO_NOT_TRACK": "1", "HAL_API_KEY": DUMMY_KEY}


def check_context(ctx: BoxContext) -> None:
    """Raises `SandboxError` for a model alias the writers cannot place safely in a config file."""
    if not ctx.model or any(ord(c) < 0x20 or ord(c) > 0x7E for c in ctx.model):
        raise SandboxError(f"model alias {ctx.model!r} must be printable ASCII")
    if ctx.context_window <= 0 or ctx.max_output_tokens <= 0:
        raise SandboxError("context_window and max_output_tokens must be positive")


def dump_json(data: object) -> bytes:
    """Stable ASCII JSON with a trailing newline."""
    return (json.dumps(data, ensure_ascii=True, indent=2) + "\n").encode("ascii")


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)  # a JSON string is a valid TOML and YAML scalar
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    raise SandboxError(f"cannot render {type(value).__name__} as a config scalar")


def dump_toml(top: Mapping[str, object], tables: Mapping[str, Mapping[str, object]]) -> bytes:
    """TOML from top-level scalars and one level of `[table.name]` sections; every string quoted."""
    lines = [f"{k} = {_scalar(v)}" for k, v in top.items()]
    for name, body in tables.items():
        lines += ["", f"[{name}]", *(f"{k} = {_scalar(v)}" for k, v in body.items())]
    return ("\n".join(lines) + "\n").encode("ascii")


def dump_yaml(data: Mapping[str, object], indent: int = 0) -> str:
    """YAML (block style) from nested mappings and scalars; every string quoted."""
    pad = "  " * indent
    out = []
    for key, value in data.items():
        if isinstance(value, Mapping):
            out.append(f"{pad}{key}:\n{dump_yaml(value, indent + 1)}")
        else:
            out.append(f"{pad}{key}: {_scalar(value)}\n")
    return "".join(out)
