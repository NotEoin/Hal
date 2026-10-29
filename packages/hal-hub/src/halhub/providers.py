"""External tool providers: tools that no manifest declares, such as host MCP servers.

04-hub.md, "External tool providers". 03-agent's `[mcp.servers.<name>]` entries with
`host = true` become broker upstreams (RESOLUTIONS E6); 02-sandbox runs them and hands each
one to `Hub.add_provider`. The provider only runs calls; every other pipeline step (policy,
approvals, taint, guard, audit) is the hub's.

Design decisions:

- A provider's tools are named `<name>__<tool>` and belong to pack `mcp-<name>`. Manifests may
  not use `__` in a tool name or the `mcp-` pack prefix, so a provider can never shadow a
  pack's tool or inherit its grants (REQ-HUB-01, RESOLUTIONS E6).
- The hub, not the provider, sets the security fields: tier from `default_tier` (`act` unless
  `policy.toml` says otherwise per tool or pack, 01 REQ-SEC-88), `untrusted_output = true` and
  `reviewable = false`. Upstream annotations (`readOnlyHint` and the rest) are never read
  (01 REQ-SEC-87).
- Scalar upstream parameters keep their type; anything else becomes a `format = "json"`
  string, parsed back into the object for the upstream call, so 01's canonical form and the
  approval key only ever see flat scalars (D7).
- The upstream's own tool name is kept in `ToolSpec.function`, which is what the hub passes to
  `ToolProvider.call`; the hub-side name is lowercased and cleaned to the policy's name rule.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ._policy_standin import Tier, ToolMeta
from .manifest import DEFAULT_TOOL_TIMEOUT, MAX_DESCRIPTION, MAX_TOOL_NAME, ParamSpec, ToolSpec
from .runner import RunOutcome

PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,31}$")  # becomes pack "mcp-<name>" and the tool prefix
PROVIDER_PACK_PREFIX = "mcp-"
TOOL_SEPARATOR = "__"
PROVIDER_TOOL_RE = re.compile(r"^[a-z][a-z0-9]*__[a-z0-9]+(_[a-z0-9]+)*$")
SCALAR_TYPES = ("string", "integer", "number", "boolean")
PROVIDER_VERSION = "0"  # a provider has no pack version; grants still bind to the security digest

__all__ = ["ToolProvider", "provider_meta", "provider_tool"]


class ToolProvider(Protocol):
    """A source of tools that no manifest declares; 02-sandbox implements it for host MCP servers."""

    name: str
    default_tier: Tier

    def tools(self) -> list[ToolSpec]:
        """The provider's tools, usually built with `provider_tool`."""
        ...

    def call(
        self, tool: str, args: Mapping[str, object], *, timeout: float, cancel: threading.Event | None = None
    ) -> RunOutcome:
        """Runs upstream tool `tool` (the `ToolSpec.function`) and returns the outcome."""
        ...

    def close(self) -> None:
        """Stops the upstream."""
        ...


def provider_tool(
    provider: str,
    upstream: str,
    description: str,
    input_schema: Mapping[str, object],
    *,
    tier: Tier = Tier.ACT,
    timeout: float = DEFAULT_TOOL_TIMEOUT,
) -> ToolSpec:
    """A `ToolSpec` for upstream tool `upstream` of provider `provider`, from its JSON input schema.

    Raises `ValueError` when the provider name or the resulting tool name breaks the naming rule.
    """
    if not PROVIDER_NAME_RE.fullmatch(provider):
        raise ValueError(f"provider name {provider!r} must match {PROVIDER_NAME_RE.pattern}")
    suffix = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", upstream.lower())).strip("_")
    name = f"{provider}{TOOL_SEPARATOR}{suffix}"
    if not PROVIDER_TOOL_RE.fullmatch(name) or len(name) > MAX_TOOL_NAME:
        raise ValueError(f"upstream tool {upstream!r} of {provider} gives the unusable name {name!r}")
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    if not isinstance(properties, Mapping):
        properties = {}
    params = tuple(
        _param(str(key), spec, required=isinstance(required, list) and key in required)
        for key, spec in sorted(properties.items())
    )
    return ToolSpec(
        name=name,
        module=provider,
        pack=PROVIDER_PACK_PREFIX + provider,
        function=upstream,
        description=description[:MAX_DESCRIPTION],
        tier=str(tier),
        tier_inferred=False,
        untrusted_output=True,
        reviewable=False,
        timeout=timeout,
        params=params,
    )


def _param(name: str, spec: object, *, required: bool) -> ParamSpec:
    spec = spec if isinstance(spec, Mapping) else {}
    kind = spec.get("type")
    description = str(spec.get("description", ""))
    if kind in SCALAR_TYPES:
        enum = spec.get("enum")
        scalar_enum = isinstance(enum, list) and all(isinstance(v, str | int | float) for v in enum)
        return ParamSpec(
            name=name,
            type=kind,
            description=description,
            required=required,
            enum=tuple(enum) if scalar_enum else None,
        )
    # objects, arrays, unions and untyped values travel as one JSON string (D7)
    return ParamSpec(
        name=name, type="string", description=description, required=required, format="json", v1_object=True
    )


@dataclass(frozen=True)
class _ProviderModule:
    """The two module facts `ToolSpec.meta` reads, for a tool that has no module."""

    version: str = PROVIDER_VERSION
    domains: tuple[str, ...] = ()


def provider_meta(tool: ToolSpec) -> ToolMeta:
    """01's `ToolMeta` for a provider tool."""
    return tool.meta(_ProviderModule())  # duck-typed: `meta` reads only `version` and `domains`
