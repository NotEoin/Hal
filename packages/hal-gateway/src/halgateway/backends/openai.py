"""Generic OpenAI-compatible backend: address, API support and credentials from the config.

Used as is for `openai` backends and as the base of the Ollama, llama-router and managed-openai
backends, which add their own routes and lifecycle. Decisions:

- Credentials come from `api_key_file` or `api_key_env`, read on each request so a rotated key
  needs no restart; a configured key that cannot be read is an error, not a silent no-key call.
- A remote backend is never `gpu` and never `expose_models` unless the entry says so.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from ..errors import Unavailable
from ..routes import Api

DEFAULT_PORTS = {"http": 80, "https": 443}
ALL_APIS = frozenset(Api) - {Api.HEALTH, Api.ADMIN}
DEFAULT_APIS: dict[str, frozenset[Api]] = {
    "llama-router": ALL_APIS - {Api.OLLAMA_NATIVE},
    "ollama": ALL_APIS - {Api.RESPONSES_COUNT},
    "openai": frozenset({Api.CHAT, Api.MODELS, Api.EMBEDDINGS}),
    "managed-openai": frozenset({Api.CHAT, Api.MODELS, Api.EMBEDDINGS}),
}
API_NAMES = {
    "chat": Api.CHAT,
    "responses": Api.RESPONSES,
    "messages": Api.MESSAGES,
    "models": Api.MODELS,
    "embeddings": Api.EMBEDDINGS,
    "native": Api.OLLAMA_NATIVE,
}
API_GROUPS = {
    Api.RESPONSES: (Api.RESPONSES, Api.RESPONSES_COUNT),
    Api.MESSAGES: (Api.MESSAGES, Api.COUNT_TOKENS),
}
OLLAMA_PORT = 11434


@dataclass(frozen=True)
class UpstreamAddress:
    """Where a backend listens."""

    scheme: Literal["http", "https", "unix"]
    host: str
    port: int
    path: str | None = None  # unix socket path


def parse_address(url: str) -> UpstreamAddress:
    """Parses `http://host:port`, `https://host` or `unix:/path.sock`; raises `ValueError`."""
    parts = urlsplit(url)
    if parts.scheme == "unix":
        return UpstreamAddress("unix", "localhost", 0, parts.path)
    if parts.scheme in ("http", "https") and parts.hostname:
        port = parts.port or DEFAULT_PORTS[parts.scheme]
        return UpstreamAddress(parts.scheme, parts.hostname, port)  # type: ignore[arg-type]  # checked above
    raise ValueError(f"not a backend URL: {url!r}")


class OpenAIBackend:
    """A backend built from one `[gateway.backends.NAME]` entry."""

    def __init__(self, name: str, entry: Mapping[str, object], *, ollama_url: str) -> None:
        self.name = name
        self.type = str(entry["type"])
        self.entry = entry
        url = str(entry["url"])
        if not url:
            url = ollama_url if self.type == "ollama" else f"http://127.0.0.1:{entry.get('port', 8080)}"
        self.url = url
        self._address = parse_address(url)
        self.remote = bool(entry["allow_remote"]) and self._address.scheme == "https"
        self.gpu = bool(entry["gpu"]) and not self.remote
        self.expose_models = bool(entry["expose_models"])
        configured = [API_NAMES[a] for a in entry["apis"]]  # type: ignore[attr-defined]  # list of str
        grouped = {member for api in configured for member in API_GROUPS.get(api, (api,))}
        self.apis = frozenset(grouped) if grouped else DEFAULT_APIS[self.type]

    def supports(self, api: Api) -> bool:
        """True when the backend serves `api`."""
        return api in self.apis

    def ensure(self) -> None:
        """Makes the backend ready for a request; a backend without a process has nothing to do."""

    @contextlib.contextmanager
    def request(self) -> Iterator[None]:
        """Marks one request in flight; the base backend does not count."""
        yield

    def clean_ids(self, ids: list[str]) -> list[str]:
        """Returns the model ids worth listing; the generic backend keeps all of them."""
        return ids

    def api_names(self) -> list[str]:
        """Sorted names of the APIs served, for 404 messages."""
        return sorted(api.value for api in self.apis)

    def address(self) -> UpstreamAddress:
        """Where to connect."""
        return self._address

    def auth_headers(self) -> dict[str, str]:
        """Returns `Authorization` for the configured key, or nothing; raises `Unavailable`."""
        key_file = str(self.entry["api_key_file"])
        key_env = str(self.entry["api_key_env"])
        key = ""
        if key_file:
            try:
                key = Path(key_file).expanduser().read_text(encoding="ascii").strip()
            except OSError as exc:
                raise Unavailable(f"backend {self.name}: cannot read api_key_file: {exc}") from exc
        elif key_env:
            key = os.environ.get(key_env, "")
            if not key:
                raise Unavailable(f"backend {self.name}: environment variable {key_env} is empty")
        return {"Authorization": f"Bearer {key}"} if key else {}
