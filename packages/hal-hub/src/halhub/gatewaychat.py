"""Plugs the gateway chat client (`halkit.llm`) into the hub's classifier and reviewer.

Both take a `chat(model, messages, *, temperature, max_tokens, timeout_s)` callable so tests can
pass a fake; this module is the production one. Reason it is a separate module: the hub core
stays free of any network client, and a process without a gateway still builds a hub whose
calls simply classify as `high` (I5).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from halkit import llm

from . import policy
from .classifier import ChatFn
from .reviewer import Reviewer


def gateway_chat(
    model: str,
    messages: Sequence[Mapping[str, object]],
    *,
    temperature: float,
    max_tokens: int,
    timeout_s: float,
) -> str:
    """Sends one chat request to the gateway alias `model`; raises `halkit.llm.GatewayError`."""
    return llm.chat(messages, model=model, timeout=timeout_s, max_tokens=max_tokens, temperature=temperature)


def hub_kwargs() -> dict[str, object]:
    """`Hub(**hub_kwargs())` arguments for production: the classifier chat and, if enabled, the reviewer."""
    chat: ChatFn = gateway_chat
    settings = policy.load_policy().reviewer
    kwargs: dict[str, object] = {"classify_chat": chat}
    if settings.enabled:
        kwargs["reviewer"] = Reviewer(settings, chat)
    return kwargs
