"""Per-request hints: the effort level and a role's default settings.

A client asks for an effort level with the `X-Hal-Effort` header or the `hal_effort` body field
(`off|low|medium|high|max`; the header wins). The gateway turns it into the fields the
upstream understands with `halkit.effort` and merges them into the request body. Decisions:

- The hint applies to Chat Completions requests only; on other routes it is dropped with a
  logged note, because the field names differ per API.
- `hal_effort` never reaches the upstream: a strict OpenAI-compatible server may refuse unknown
  fields.
- Without a hint, a role alias (`hal-chat`, ...) uses the role's `effort` and `temperature` from
  `[models.roles.ROLE]`. A field the client already set is never overwritten.
- A model that cannot think, or an unknown level, is ignored with a log line and a `policy`
  note in the request log; effort is advice, not a reason to fail the request.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from halkit import effort
from halkit.modelconf import UNSET_TEMPERATURE, Role

from .config import ROLE_ALIASES

log = logging.getLogger(__name__)

HEADER = "X-Hal-Effort"
BODY_FIELD = "hal_effort"
# a managed server speaks OpenAI (vLLM takes `chat_template_kwargs`, REQ-GW-94)
BACKEND_KINDS = {
    "ollama": "ollama",
    "openai": "openai",
    "managed-openai": "openai",
    "llama-router": "llamacpp",
}


def role_of(alias: str) -> str | None:
    """The role name of a role alias (`hal-chat` gives `chat`), else None."""
    return alias.removeprefix("hal-") if alias in ROLE_ALIASES else None


def _merge(body: dict, fields: Mapping[str, object]) -> None:  # type: ignore[type-arg]
    for key, value in fields.items():
        if isinstance(value, dict) and isinstance(body.get(key), dict):
            for inner, item in value.items():
                body[key].setdefault(inner, item)
        else:
            body.setdefault(key, value)


def apply_hints(
    body: dict,  # type: ignore[type-arg]
    *,
    header: str | None,
    alias: str,
    upstream_model: str,
    backend_type: str,
    is_chat: bool,
    roles: Mapping[str, Role],
) -> dict[str, object]:
    """Applies the effort hint and role defaults to `body` in place; returns the decisions to log."""
    raw = body.pop(BODY_FIELD, None)
    hint = header if header else raw
    role = roles.get(role_of(alias) or "")
    decisions: dict[str, object] = {}
    if role is not None and is_chat and role.temperature != UNSET_TEMPERATURE:
        body.setdefault("temperature", role.temperature)
    level = None
    if hint not in (None, ""):
        level = effort.parse_level(hint)
        if level is None:
            log.warning("ignoring effort hint %r: use one of %s", hint, ", ".join(effort.LEVELS))
            return {"effort": f"ignored: unknown level {str(hint)[:20]!r}"}
    elif role is not None and role.effort:
        level = role.effort
    if level is None:
        return decisions
    if not is_chat:
        log.info("effort hint ignored on a route other than chat completions")
        return {"effort": "ignored: chat completions only"}
    kind = BACKEND_KINDS.get(backend_type)
    if kind is None:
        return {"effort": f"ignored: backend type {backend_type}"}
    cap = effort.capability(role.family if role else "", upstream_model)
    result = effort.request_fields(level, cap, kind)
    if result.fields:
        _merge(body, result.fields)
    note = f"{level}" + (f" ({result.note})" if result.note else "")
    if not result.applied:
        log.info("effort %s not applied to %s: %s", level, upstream_model, result.note)
        note = f"ignored: {result.note}"
    decisions["effort"] = note
    return decisions
