"""The models `/model` offers: those of the configured sources, with the name the gateway serves them under.

`hal code` talks to the gateway, so a choice needs a gateway model name. It follows the rules of
`halcore.modelplan.wire`: an Ollama source that is `[hal] ollama_url` is served by its bare model
name, any other Ollama or OpenAI-compatible source as `SOURCE/MODEL` (the backend `/model save`
adds), a GGUF model through the preset of the role that holds it, and an Anthropic source not at
all (the gateway has no backend for it). A choice with no gateway name is listed but not
selectable, with the reason.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from halgateway import tunedmode
from halkit import discovery, effort, modelconf

from .. import modelplan

log = logging.getLogger(__name__)

ROUTER_BACKEND = modelplan.ROUTER_BACKEND


@dataclass(frozen=True)
class Choice:
    """One model of one source."""

    source: str
    kind: str
    name: str
    gateway: str  # "" when the gateway cannot serve it yet
    family: str = ""
    size_bytes: int | None = None
    thinks: bool = False
    note: str = ""


def gateway_name(
    src: modelconf.Source, model: str, roles: Mapping[str, modelconf.Role], ollama_url: str
) -> tuple[str, str]:
    """`(gateway model name, note)`; the name is empty when the gateway cannot serve the model yet."""
    if src.kind == "ollama":
        if src.url.rstrip("/") == ollama_url.rstrip("/"):
            return model, ""
        return f"{src.name}/{model}", "needs /model save once so the gateway has this backend"
    if src.kind in ("openai", "openai-cloud"):
        return f"{src.name}/{model}", "needs /model save once so the gateway has this backend"
    if src.kind == "gguf":
        for role in roles.values():
            if role.source == src.name and role.model == model:
                return f"{ROUTER_BACKEND}/{tunedmode.preset_name(role.role)}", ""
        return "", "not served yet: /model save assigns and wires it"
    return "", "the gateway has no backend for this source"


def load(
    sources: Mapping[str, modelconf.Source], roles: Mapping[str, modelconf.Role], ollama_url: str,
    discover: Callable[[modelconf.Source], discovery.Discovery] = discovery.discover,
) -> list[Choice]:
    """Every discovered model, in source order. An unreachable source adds no rows and is logged."""
    out: list[Choice] = []
    for name, src in sources.items():
        found = discover(src)
        if found.error:
            log.warning("model source %s: %s", name, found.error)
        for m in found.models:
            gateway, note = gateway_name(src, m.name, roles, ollama_url)
            kind, _ = effort.levels_for(effort.capability(m.family, m.name))
            out.append(Choice(name, src.kind, m.name, gateway, m.family, m.size_bytes, bool(kind), note))
    return out


def find(choices: list[Choice], text: str) -> Choice | None:
    """The choice a user typed: a gateway name, a model name or `SOURCE/MODEL`, else a unique substring."""
    if not text:
        return None
    exact = [c for c in choices if text in (c.gateway, c.name, f"{c.source}/{c.name}")]
    if len(exact) == 1:
        return exact[0]
    partial = [c for c in choices if text.lower() in f"{c.source}/{c.name}".lower()]
    return partial[0] if len(partial) == 1 and not exact else None
