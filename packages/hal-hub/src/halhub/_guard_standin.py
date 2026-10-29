"""Stand-in for 01-security's `halhub.reviewer` interface.

Copies the published interface of 01-security.md, "Taint, guard and reviewer", so the hub's
call pipeline can take a reviewer: `ReviewRequest`, `ReviewResult` and the `Reviewer`
protocol. There is no reviewer model here; tests pass fakes. 01's step 28 replaces this
module with `halhub.reviewer`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from .policy.model import Taint, ToolMeta


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
