"""Ollama backend: the generic backend plus its native helpers (tags, loaded models, unload).

Ported from `halcore/power.py`. Decisions:

- Only the helpers the gateway needs are here; `pull`, `push`, `create`, `copy`, `delete` and
  `blobs` are never routed (REQ-GW-24), so a box or a client cannot manage models through us.
- `/v1/models` of Ollama carries `llamacpp:<sha>` ids and repeats names; both are dropped.
- Unloading posts `keep_alive: 0`, which is how Ollama frees VRAM without stopping.
"""

from __future__ import annotations

from collections.abc import Iterable

from .. import relay
from ..errors import BadGatewayError, BadRequest
from .openai import OpenAIBackend

LLAMACPP_PREFIX = "llamacpp:"
# Options a caller may set through the `options` field of a chat request; the rest of Ollama's
# options (paths, runners) are not the caller's business.
ALLOWED_OPTIONS = frozenset({"num_gpu", "num_ctx", "num_predict", "temperature", "top_p", "seed"})
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

    def chat_with_options(
        self,
        body: dict,
        options: dict,
        timeout_s: float,  # type: ignore[type-arg]
    ) -> dict:  # type: ignore[type-arg]
        """A non-streaming chat through the native `/api/chat`, returned in OpenAI shape.

        The OpenAI route of Ollama has no way to set `num_gpu`, which the GPU-or-CPU choice of
        the classifier needs (I5), so this one case is mapped by hand. Tools are not supported.
        """
        unknown = sorted(set(options) - ALLOWED_OPTIONS)
        if unknown:
            raise BadRequest(f"options {unknown} are not allowed; allowed: {sorted(ALLOWED_OPTIONS)}")
        if body.get("tools"):
            raise BadRequest("options cannot be combined with tools")
        native_options = dict(options)
        if body.get("max_tokens") is not None:
            native_options.setdefault("num_predict", body["max_tokens"])
        if body.get("temperature") is not None:
            native_options.setdefault("temperature", body["temperature"])
        native = {"model": body["model"], "messages": body.get("messages") or [], "stream": False}
        if native_options:
            native["options"] = native_options
        if body.get("logprobs"):
            # Ollama 0.12.11 and later take these on /api/chat and answer with `logprobs`
            native["logprobs"] = True
            native["top_logprobs"] = int(body.get("top_logprobs") or 0)
        reply = relay.call_json(self, "POST", "/api/chat", native, timeout_s=timeout_s)
        message = reply.get("message") if isinstance(reply, dict) else None
        if not isinstance(message, dict):
            raise BadGatewayError(f"backend {self.name} sent no message for /api/chat")
        prompt, completion = int(reply.get("prompt_eval_count") or 0), int(reply.get("eval_count") or 0)
        choice: dict = {  # type: ignore[type-arg]
            "index": 0,
            "message": {"role": "assistant", "content": str(message.get("content", ""))},
            "finish_reason": "length" if reply.get("done_reason") == "length" else "stop",
        }
        native_logprobs = reply.get("logprobs")
        if isinstance(native_logprobs, list):
            choice["logprobs"] = {"content": native_logprobs}  # same entry shape as the OpenAI route
        return {
            "object": "chat.completion",
            "model": body["model"],
            "choices": [choice],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }

    def in_vram(self) -> list[str]:
        """Names of the models that hold VRAM now (`size_vram > 0` in `GET /api/ps`)."""
        doc = relay.call_json(self, "GET", "/api/ps")
        return [
            str(m["name"])
            for m in doc.get("models", [])
            if isinstance(m, dict) and "name" in m and m.get("size_vram", 0) > 0
        ]

    def free_vram(self, served: Iterable[str]) -> list[str]:
        """Unloads the models in `served` that hold VRAM; returns their names (REQ-GW-53)."""
        wanted = set(served)
        freed = []
        for model in self.in_vram():
            if model in wanted or model.removesuffix(":latest") in wanted:
                self.unload(model)
                freed.append(model)
        return freed

    def unload_offloaded(self) -> list[str]:
        """Unloads models that sit partly in system RAM (`size_vram < size`, REQ-GW-57)."""
        doc = relay.call_json(self, "GET", "/api/ps")
        freed = []
        for m in doc.get("models", []):
            if isinstance(m, dict) and "name" in m and 0 < m.get("size_vram", 0) < m.get("size", 0):
                self.unload(str(m["name"]))
                freed.append(str(m["name"]))
        return freed

    def unload(self, model: str) -> None:
        """Asks Ollama to free `model` now."""
        relay.call_json(
            self, "POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout_s=UNLOAD_TIMEOUT_S
        )
