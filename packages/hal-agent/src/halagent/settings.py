"""Resolved settings of the agent: the `[agent]` config table and its environment overrides.

The endpoint is either an http(s) base URL or a unix socket path (a bare path or `unix://...`).
Unknown keys are rejected by halkit's strict loader, so a typo fails loudly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from halkit import config, paths

DEFAULT_MAX_TURNS = 30
DEFAULT_MAX_TOOL_CALLS = 100
DEFAULT_MODEL = "default"
DEFAULT_WINDOW = 32_768
DEFAULT_MAX_OUTPUT = 4_096
DEFAULT_MAX_WALL_S = 1_800  # interactive turn cap, 03-agent 3.5.4; users can cancel earlier


def _positive(value: object) -> str | None:
    return None if isinstance(value, int) and value >= 1 else "must be a positive integer"


AGENT_TABLE = config.register(
    config.Table(
        name="agent",
        owner="hal-agent",
        keys=(
            config.Key("endpoint", str, "", env="HAL_AGENT_ENDPOINT", doc="Base URL or unix socket path"),
            config.Key("model", str, DEFAULT_MODEL, env="HAL_AGENT_MODEL", doc="Model name to request"),
            config.Key(
                "api_key", str, "", env="HAL_AGENT_API_KEY", doc="Bearer token, if the endpoint needs one"
            ),
            config.Key("stream", bool, False, env="HAL_AGENT_STREAM", doc="Use streaming responses"),
            config.Key(
                "tool_format",
                str,
                "native",
                env="HAL_AGENT_TOOL_FORMAT",
                choices=("native", "prompt"),
                doc="native tool calls, or a text format for models without them",
            ),
            config.Key("max_turns", int, DEFAULT_MAX_TURNS, check=_positive, doc="Model calls per prompt"),
            config.Key(
                "max_tool_calls", int, DEFAULT_MAX_TOOL_CALLS, check=_positive, doc="Tool calls per prompt"
            ),
            config.Key(
                "max_wall_s", int, DEFAULT_MAX_WALL_S, check=_positive, doc="Wall-clock seconds per prompt"
            ),
            config.Key("context_window", int, DEFAULT_WINDOW, check=_positive, doc="Model context in tokens"),
            config.Key(
                "max_output", int, DEFAULT_MAX_OUTPUT, check=_positive, doc="Tokens kept free for a reply"
            ),
            config.Key("broker", str, "", env="HAL_AGENT_BROKER", doc="Hub MCP socket path, empty for none"),
        ),
    )
)


@dataclass(frozen=True)
class AgentSettings:
    """Resolved agent settings."""

    endpoint: str
    model: str = DEFAULT_MODEL
    api_key: str = ""
    stream: bool = False
    tool_format: str = "native"
    max_turns: int = DEFAULT_MAX_TURNS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    broker: str = ""
    context_window: int = DEFAULT_WINDOW
    max_output: int = DEFAULT_MAX_OUTPUT
    max_wall_s: int = DEFAULT_MAX_WALL_S


def load_settings(
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    cli: Mapping[str, object] | None = None,
) -> AgentSettings:
    """Loads settings (cli, env, file, defaults); raises `ConfigError` or `ValueError` if unusable."""
    values = dict(config.load(AGENT_TABLE, path=path, environ=environ, cli=cli))
    endpoint = str(values["endpoint"]) or str(paths.runtime_dir() / "gateway.sock")
    return AgentSettings(
        endpoint=endpoint,
        model=str(values["model"]),
        api_key=str(values["api_key"]),
        stream=bool(values["stream"]),
        tool_format=str(values["tool_format"]),
        max_turns=int(values["max_turns"]),  # type: ignore[call-overload]  # validated by `_positive`
        max_tool_calls=int(values["max_tool_calls"]),  # type: ignore[call-overload]  # validated
        broker=str(values["broker"]),
        context_window=int(values["context_window"]),  # type: ignore[call-overload]  # validated
        max_wall_s=int(values["max_wall_s"]),  # type: ignore[call-overload]  # validated
        max_output=int(values["max_output"]),  # type: ignore[call-overload]  # validated
    )
