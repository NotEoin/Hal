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

log = logging.getLogger(__name__)

MODE_ALIASES = ("hal/main", "hal/agent")  # the aliases that follow the active mode's `[hal]` section


@dataclass
class RequestContext:
    """What a policy sees of one request; `decisions` goes to the `policy` field of the log record."""

    fmt: str  # `anthropic` or `openai`
    body: dict  # type: ignore[type-arg]
    alias: str
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


def select(
    alias: str,
    alias_entry: Mapping[str, object] | None,
    mode_hal: object | None,
    load_note: Callable[[object | None], str],
) -> list[Policy]:
    """The policies that apply to a request for `alias`.

    `alias_entry` is the `[gateway.aliases.NAME]` table when the name is a configured alias;
    `mode_hal` is the active mode's `HalSettings` when the target is the managed router.
    """
    from .effort import EffortPolicy  # imported here: effort needs this module's types

    chosen: list[Policy] = []
    from_mode = mode_hal is not None and alias in MODE_ALIASES
    if (alias_entry is not None and alias_entry.get("effort")) or (
        from_mode and getattr(mode_hal, "effort", False)
    ):
        chosen.append(EffortPolicy(load_note(mode_hal)))
    return chosen
