"""Model name resolution and the `/v1/models` listing.

The order is the one in docs/design/05-gateway.md "Alias resolution": user alias, role alias,
`backend/model`, a bare name found in exactly one backend, a pattern, `default_model`, else 404.
Decisions:

- Backend model lists are cached for `LIST_TTL_S` seconds, so a request does not cost a list call.
  A backend with `static_models` (a managed server) lists those instead, so a listing never
  waits for, or depends on, a server that is stopped.
- Mode aliases (`hal/main`, preset names) come from the managed router (plan step 11); the
  resolver takes them as a plain mapping so that step only has to fill it.
- A role alias whose model is in no backend list falls back to the only Ollama backend: Ollama
  loads on demand and answers a missing model with its own error, which is clearer than 404 here.
- Session and client restrictions arrive with their registries (plan steps 19 and 21).
"""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .backends import OpenAIBackend
from .config import EMPTY_MEANS_CHAT, ROLE_ALIASES, GatewayConfig
from .errors import Conflict, NotFound

LIST_TTL_S = 30.0  # REQ design: lists cached for 30 s
RESERVED_BACKEND = "hal"
ListModels = Callable[[OpenAIBackend], list[str]]


@dataclass(frozen=True)
class Target:
    """A resolved model: where to send it and under which upstream name."""

    alias: str
    backend: OpenAIBackend
    model: str
    remote: bool
    context_length: int | None
    profile: str | None = None  # the manifest `profile` of the upstream model (`meta.hal_profile`)


@dataclass(frozen=True)
class ModelEntry:
    """One row of `/v1/models`."""

    id: str
    owned_by: str
    context_length: int | None
    profile: str | None = None  # the manifest `profile` of the upstream model (`meta.hal_profile`)
    slots: int = 0  # parallel decoding slots of the alias (`meta.slots`); 0 = unknown


class Resolver:
    """Maps a requested model name to a `Target` and lists the names a caller may use."""

    def __init__(
        self,
        cfg: GatewayConfig,
        backends: Mapping[str, OpenAIBackend],
        list_models: ListModels,
        mode_aliases: Mapping[str, tuple[str, str]] | None = None,
        profile_of: Callable[[str], str | None] = lambda model: None,
    ) -> None:
        self.cfg = cfg
        self.backends = backends
        self._list_models = list_models
        self._profile_of = profile_of
        self.mode_aliases = dict(mode_aliases or {})  # name -> (backend name, upstream model)
        # (backend name, upstream model) -> context the active mode started it with; lets roles
        # such as `hal-code` report the window `hal tune` chose instead of none
        self.mode_windows: dict[tuple[str, str], int] = {}
        self._cache: dict[str, tuple[float, list[str]]] = {}

    def models_of(self, backend: OpenAIBackend) -> list[str]:
        """The backend's model ids, cached for `LIST_TTL_S`; empty when it cannot answer."""
        static = getattr(backend, "static_models", None)
        if static is not None:
            return list(static())
        hit = self._cache.get(backend.name)
        if hit and time.monotonic() - hit[0] < LIST_TTL_S:
            return hit[1]
        ids = self._list_models(backend)
        self._cache[backend.name] = (time.monotonic(), ids)
        return ids

    def _target(self, alias: str, backend: OpenAIBackend, model: str, window: int | None = None) -> Target:
        window = window or self.mode_windows.get((backend.name, model))
        return Target(alias, backend, model, backend.remote, window or None)

    def role_value(self, role: str) -> str:
        """The model name `[hal]` gives to role alias `role`."""
        hal = self.cfg.hal
        value = str(hal[ROLE_ALIASES[role]])
        if not value and role in EMPTY_MEANS_CHAT:
            value = str(hal["chat_model"])
        return value

    def resolve(self, requested: str | None) -> Target:
        """Returns the target for `requested`; raises `NotFound` or `Conflict` (409)."""
        name = requested or ""
        if name:
            found = self._by_name(name)
            if found is not None:
                return found
            for alias_name, alias in self.cfg.aliases.items():  # step 6
                for pattern in alias["patterns"]:  # type: ignore[attr-defined]  # list of str
                    if fnmatch.fnmatchcase(name, pattern):
                        return self._from_alias(alias_name)
        default = str(self.cfg.gateway["default_model"])
        if default and default != name:
            found = self._by_name(default)
            if found is not None:
                return found
        shown = f"model {name!r}" if name else "the request names no model and default_model is empty; model"
        raise NotFound(f"{shown} not found; see GET /v1/models")

    def _from_alias(self, name: str) -> Target:
        alias = self.cfg.aliases[name]
        return self._target(
            name, self.backends[str(alias["backend"])], str(alias["model"]), alias["context_window"]
        )  # type: ignore[arg-type]

    def _by_name(self, name: str) -> Target | None:
        if name in self.cfg.aliases:  # step 1
            return self._from_alias(name)
        if name in ROLE_ALIASES:  # step 2
            value = self.role_value(name)
            found = self._by_model_name(value, name)
            if found is not None:
                return found
            ollama = [b for b in self.backends.values() if b.type == "ollama"]
            if len(ollama) == 1:
                return self._target(name, ollama[0], value)
            return None
        return self._by_model_name(name, name)

    def _by_model_name(self, name: str, alias: str) -> Target | None:
        if name in self.mode_aliases:  # step 3
            backend_name, model = self.mode_aliases[name]
            return self._target(alias, self.backends[backend_name], model)
        backend_name, _, model = name.partition("/")  # step 4
        backend = self.backends.get(backend_name)
        if model and backend is not None and backend.expose_models and backend_name != RESERVED_BACKEND:
            return self._target(alias, backend, model)
        owners = [
            b for b in self.backends.values() if b.expose_models and name in self.models_of(b)
        ]  # step 5
        if len(owners) > 1:
            raise Conflict(
                f"model {name!r} is served by several backends: "
                + ", ".join(f"{b.name}/{name}" for b in owners)
                + "; use one of those names"
            )
        return self._target(alias, owners[0], name) if owners else None

    def listing(self) -> list[ModelEntry]:
        """Every name a caller may use, without duplicates (aliases, roles, `backend/model`)."""
        entries: dict[str, ModelEntry] = {}

        def add(
            entry_id: str, backend: OpenAIBackend, window: int | None, upstream: str, slots: int = 0
        ) -> None:
            if entry_id in entries:
                return
            owner = f"remote:{backend.address().host}" if backend.remote else backend.name
            entries[entry_id] = ModelEntry(entry_id, owner, window, self._profile_of(upstream), slots)

        for name, alias in self.cfg.aliases.items():
            add(
                name,
                self.backends[str(alias["backend"])],
                alias["context_window"] or None,
                str(alias["model"]),
                int(alias["slots"]),  # type: ignore[call-overload]  # validated non-negative
            )  # type: ignore[arg-type]
        for role in ROLE_ALIASES:
            try:
                target = self._by_name(role)
            except Conflict:
                # an ambiguous role has no alias to list
                continue
            if target is not None:
                add(role, target.backend, target.context_length, target.model)
        for backend in self.backends.values():
            if backend.expose_models:
                for model in self.models_of(backend):
                    add(f"{backend.name}/{model}", backend, None, model)
        return list(entries.values())
