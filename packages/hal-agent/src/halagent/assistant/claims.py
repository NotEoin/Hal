"""The claims check: a reply that says something was done with no tool call is retried once.

The loop calls `ClaimsCheck.decide` when a round ends with no tool call. A suspect reply with no
tool run gets one silent retry with `RETRY`; with no tools at all the retry is `NO_TOOLS_RETRY`
and fires on `CLAIM`; an `OK` reply after a retry restores the original (REQ-AGT-51). The text of
a round is held until it is decided, because the provider returns whole completions; the first
sentence rule of the streaming brain is therefore met trivially.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from halagent.assistant.prompts import CLAIM, NO_TOOLS_RETRY, RETRY, SUSPECT, THINK


@dataclass(frozen=True)
class Decision:
    """`retry` drops the reply and adds `system_message`; `accept` releases `text`."""

    action: Literal["accept", "retry"]
    text: str = ""
    system_message: str = ""


class ClaimsCheck:
    """Decides whether a final reply is released."""

    def __init__(self, *, has_tools: bool) -> None:
        self.has_tools = has_tools
        self.original = ""

    def releasable(self, text: str) -> bool:
        """True when text that comes with tool calls may be shown at once."""
        return not SUSPECT.search(text) and "<think>" not in text

    def decide(self, held: str, *, tools_run: int, retried: bool) -> Decision:
        """The decision for a reply that came with no tool call."""
        if not retried and self.has_tools and not tools_run and SUSPECT.search(held):
            self.original = held
            return Decision("retry", system_message=RETRY)
        if not retried and not self.has_tools and CLAIM.search(held):
            self.original = held
            return Decision("retry", system_message=NO_TOOLS_RETRY)
        if retried and held.strip().rstrip(".") == "OK":
            held = self.original
        return Decision("accept", text=THINK.sub("", held))
