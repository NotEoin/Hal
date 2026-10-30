"""Ollama backend: the generic backend plus its native helpers (tags, loaded models, unload).

Ported from `halcore/power.py`. Decisions:

- Only the helpers the gateway needs are here; `pull`, `push`, `create`, `copy`, `delete` and
  `blobs` are never routed (REQ-GW-24), so a box or a client cannot manage models through us.
- `/v1/models` of Ollama carries `llamacpp:<sha>` ids and repeats names; both are dropped.
- Unloading posts `keep_alive: 0`, which is how Ollama frees VRAM without stopping.
"""

from __future__ import annotations

from .. import relay
from .openai import OpenAIBackend

LLAMACPP_PREFIX = "llamacpp:"
UNLOAD_TIMEOUT_S = 30.0  # Ollama waits for a running generation before it unloads


class OllamaBackend(OpenAIBackend):
    """A backend built from a `type = "ollama"` entry."""

    estimates_count_tokens = True  # Ollama has no `/v1/messages/count_tokens` (REQ-GW-18)

    def clean_ids(self, ids: list[str]) -> list[str]:
        """Drops `llamacpp:<sha>` ids and repeated names, keeping the order."""
        seen: set[str] = set()
        out = []
        for model_id in ids:
            if model_id.startswith(LLAMACPP_PREFIX) or model_id in seen:
                continue
            seen.add(model_id)
            out.append(model_id)
        return out

    def tags(self) -> list[dict]:  # type: ignore[type-arg]
        """The model entries of `GET /api/tags`; raises `GatewayError` when Ollama is down."""
        doc = relay.call_json(self, "GET", "/api/tags")
        return [m for m in doc.get("models", []) if isinstance(m, dict)]

    def loaded(self) -> list[str]:
        """Names of the models Ollama holds in memory now (`GET /api/ps`)."""
        doc = relay.call_json(self, "GET", "/api/ps")
        return [str(m["name"]) for m in doc.get("models", []) if isinstance(m, dict) and "name" in m]

    def unload(self, model: str) -> None:
        """Asks Ollama to free `model` now."""
        relay.call_json(
            self, "POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout_s=UNLOAD_TIMEOUT_S
        )
