"""Request and response policies of the router backends (REQ-GW-40..49).

A policy is a pure function of the request body or response body where it can be, so it is
testable without sockets. The handler applies them on `llama-router` backends for the
`/v1/messages` and `/v1/chat/completions` routes only (design decision 6).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ..config import MODE_ALIASES  # the aliases a mode's `[hal]` policies apply to
from ..modes import HalSettings

log = logging.getLogger(__name__)


@dataclass
class RequestContext:
    """What a policy sees of one request.

    `model` is the upstream model name and policies may change it; `decisions` goes to the
    `policy` field of the log record.
    """

    fmt: str  # `anthropic` or `openai`
    body: dict  # type: ignore[type-arg]
    alias: str
    model: str = ""
    decisions: dict[str, object] = field(default_factory=dict)


class Policy(Protocol):
    """A request rewriter; it must not raise (the pipeline is fail-open)."""

    name: str

    def rewrite(self, ctx: RequestContext) -> None:
        """Changes `ctx.body` in place and records its decision in `ctx.decisions`."""


def run_rewrites(policies: list[Policy], ctx: RequestContext) -> None:
    """Applies `policies` in order; a policy that raises is logged and skipped (fail-open)."""
    for policy in policies:
        try:
            policy.rewrite(ctx)
        except Exception as exc:  # noqa: BLE001 - a broken policy must not take the request down
            log.warning("policy %s failed: %r", policy.name, exc)
            ctx.decisions[policy.name] = f"error: {type(exc).__name__}"


@dataclass
class Services:
    """What the policies need from the backend and the mode files."""

    load_note: Callable[[HalSettings | None], str]
    load_delegate_note: Callable[[HalSettings | None], str]
    classify: Callable[[str, str], tuple[str, float]]  # (classifier model, user text) -> (choice, p_strong)
    memory: object  # a `route.TurnMemory`


def select(
    alias: str,
    alias_entry: Mapping[str, object] | None,
    hal: HalSettings | None,
    model: str,
    services: Services,
) -> list[Policy]:
    """The policies that apply to a request for `alias`, in pipeline order (route, delegate, effort).

    `alias_entry` is the `[gateway.aliases.NAME]` table when the name is a configured alias;
    `hal` is the active mode's `HalSettings` when the target is the managed router; `model` is
    the upstream model name. Route and delegate apply to the mode's main model (the strong one).
    """
    from .delegate import DelegatePolicy  # imported here: these modules need this module's types
    from .effort import EffortPolicy
    from .route import RoutePolicy

    chosen: list[Policy] = []
    strong = hal is not None and model == hal.main
    if hal is not None and strong and hal.route:
        chosen.append(RoutePolicy(hal, services.classify, services.memory))  # type: ignore[arg-type]
    if hal is not None and strong and hal.delegate_note:
        chosen.append(DelegatePolicy(hal.strip_tools, services.load_delegate_note(hal), hal.main))
    effort_on = (alias_entry is not None and alias_entry.get("effort")) or (
        hal is not None and alias in MODE_ALIASES and hal.effort
    )
    if effort_on:
        chosen.append(EffortPolicy(services.load_note(hal)))
    return chosen
