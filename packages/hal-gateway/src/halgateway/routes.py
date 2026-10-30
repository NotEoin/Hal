"""The route table: which method and path is an API, in which error format, for whom.

The table is the one in docs/design/05-gateway.md "Route table". The gateway serves exactly
these routes (REQ-GW-10): llama-server's internal routes (`/props`, `/slots`, `/metrics`, ...)
are not here, so they are 404. Matching ignores the query string. A model id may contain `/`
(`backend/model`), so `/v1/models/{id}` takes the rest of the path.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Api(enum.Enum):
    """The API family of a route; a backend serves a subset (`Backend.supports`)."""

    HEALTH = "health"
    CHAT = "chat"
    RESPONSES = "responses"
    RESPONSES_COUNT = "responses_count"
    MESSAGES = "messages"
    COUNT_TOKENS = "count_tokens"
    EMBEDDINGS = "embeddings"
    MODELS = "models"
    OLLAMA_NATIVE = "ollama_native"
    ADMIN = "admin"


@dataclass(frozen=True)
class Route:
    """One entry of the route table."""

    method: str
    path: str  # a literal, or ending in `/{id}`
    api: Api
    fmt: str  # error format: openai, anthropic or ollama
    client: bool  # a named client token may call it
    session: bool  # a boxed session may call it
    owner_unix_only: bool = False  # mutating admin routes


def _r(
    method: str, path: str, api: Api, fmt: str, client: bool, session: bool, unix_only: bool = False
) -> Route:
    return Route(method, path, api, fmt, client, session, unix_only)


ROUTES: tuple[Route, ...] = (
    _r("GET", "/health", Api.HEALTH, "openai", True, True),
    _r("POST", "/v1/chat/completions", Api.CHAT, "openai", True, True),
    _r("POST", "/v1/responses", Api.RESPONSES, "openai", True, True),
    _r("POST", "/v1/responses/input_tokens", Api.RESPONSES_COUNT, "openai", True, True),
    _r("POST", "/v1/messages", Api.MESSAGES, "anthropic", True, True),
    _r("POST", "/v1/messages/count_tokens", Api.COUNT_TOKENS, "anthropic", True, True),
    _r("GET", "/v1/models", Api.MODELS, "openai", True, True),
    _r("GET", "/v1/models/{id}", Api.MODELS, "openai", True, False),
    _r("POST", "/v1/embeddings", Api.EMBEDDINGS, "openai", True, True),
    *(
        _r("POST", f"/api/{name}", Api.OLLAMA_NATIVE, "ollama", True, False)
        for name in ("chat", "generate", "embed", "embeddings", "show")
    ),
    _r("GET", "/api/tags", Api.OLLAMA_NATIVE, "ollama", True, False),
    _r("GET", "/api/version", Api.OLLAMA_NATIVE, "ollama", True, False),
    _r("GET", "/api/ps", Api.OLLAMA_NATIVE, "ollama", False, False),
    *(
        _r("GET", f"/hal/v1/{name}", Api.ADMIN, "openai", False, False)
        for name in ("status", "aliases", "sessions", "leases")
    ),
    *(
        _r("POST", f"/hal/v1/{name}", Api.ADMIN, "openai", False, False, True)
        for name in ("mode", "router/stop", "models/load", "models/unload", "gpu/release", "sessions")
    ),
    _r("DELETE", "/hal/v1/sessions/{id}", Api.ADMIN, "openai", False, False, True),
)


def match(method: str, target: str) -> Route | None:
    """Returns the route for `method` and request `target` (query string ignored), or None."""
    path = target.split("?", 1)[0]
    for route in ROUTES:
        if route.method != method:
            continue
        if route.path == path:
            return route
        if route.path.endswith("/{id}"):
            prefix = route.path[: -len("{id}")]
            tail = path[len(prefix) :]
            if path.startswith(prefix) and tail and (route.api is Api.MODELS or "/" not in tail):
                return route
    return None
