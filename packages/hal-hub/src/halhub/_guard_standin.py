"""Stand-in for 01-security's `halhub.guard` and `halhub.reviewer` interfaces.

Copies the published interfaces of 01-security.md, "Taint, guard and reviewer", so the hub's
call pipeline can take a guard client and a reviewer: `GuardResult` and the `GuardClient`
protocol, `ReviewRequest`, `ReviewResult` and the `Reviewer` protocol. There is no guard relay
and no reviewer model here; tests pass fakes, and with `[guard] enabled = true` and no client
the hub treats every scan as an error, which flags the session (fail closed, REQ-SEC-35).
The envelope lives in `halhub.untrusted`. 01's steps 27 and 28 replace this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from .policy.model import Taint, ToolMeta


@dataclass(frozen=True)
class GuardResult:
    """One scan's outcome; `label` `error` counts as a flag (REQ-SEC-35)."""

    label: Literal["benign", "injection", "error"]
    score: float | None
    windows: int
    ms: int
    model: str
    error: str | None = None


class GuardClient(Protocol):
    """Scans tool output for prompt injection."""

    def scan(self, text: str, *, source_tool: str) -> GuardResult:
        """Returns the scan of `text`, never raising for a guard failure."""
        ...


@dataclass(frozen=True)
class ReviewRequest:
    """What the reviewer model sees: the human's last message, the tool and the arguments."""

    user_message: str
    tool: ToolMeta
    args: Mapping[str, object]
    taint: Taint


@dataclass(frozen=True)
class ReviewResult:
    """The reviewer's answer; it never denies, `escalate` goes to the human."""

    decision: Literal["approve", "escalate"]
    reason: str
    ms: int
    model: str


class Reviewer(Protocol):
    """Screens a `reviewable` `ask` against the human's last message."""

    def review(self, req: ReviewRequest) -> ReviewResult:
        """Returns `approve` or `escalate`."""
        ...
