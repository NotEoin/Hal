"""Backends: the upstream servers behind the gateway, and a factory from the config entry.

A backend says which APIs it serves, where it listens and which credentials it needs. The
gateway never translates between APIs (decision 2): an API a backend lacks gets a clear 404.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ..routes import Api
from .ollama import OllamaBackend
from .openai import OpenAIBackend, UpstreamAddress, parse_address


class Backend(Protocol):
    """What the relay and the router need from a backend."""

    name: str
    remote: bool
    gpu: bool
    expose_models: bool

    def supports(self, api: Api) -> bool:
        """True when the backend serves `api`."""

    def address(self) -> UpstreamAddress:
        """Where to connect."""

    def auth_headers(self) -> dict[str, str]:
        """Credentials the gateway adds for this backend."""


def make_backend(name: str, entry: Mapping[str, object], ollama_url: str) -> OpenAIBackend:
    """Builds the backend for config `entry`; `ollama_url` is `[hal].ollama_url`."""
    cls = OllamaBackend if entry["type"] == "ollama" else OpenAIBackend
    return cls(name, entry, ollama_url=ollama_url)


__all__ = ["Backend", "OllamaBackend", "OpenAIBackend", "UpstreamAddress", "make_backend", "parse_address"]
