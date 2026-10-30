"""Route policy (REQ-GW-42..44), ported from `Router` in `hal-route.py`.

Applies to requests for the mode's strong model (`[hal] main`) when the mode sets `route`.
New user text is classified once through llama-server's `/v1/systemone`, and the choice holds
for every request of that conversation until the next user text. Design decisions:

- Requests without tools are side requests (titles, permission checks), not turns: they go to
  the fast model and do not change the held choice.
- A classifier that fails or times out (30 s) yields the default (`strong`) and is logged;
  routing never fails a request.
- The memory is an LRU of 1024 conversations: `hal-route.py` kept a dict that grew forever.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable

from ..modes import HalSettings
from . import RequestContext
from .text import conversation_key, user_text

log = logging.getLogger(__name__)

STATE_CHARS = 4000  # keeps the classifier prompt inside the decision model's batch (hal-route.py)
MEMORY_SIZE = 1024  # REQ-GW-44
DEFAULT_CHOICE = "strong"
CLASSIFIER_TIMEOUT_S = 30.0  # REQ-GW-43
ROUTE_QUESTION = {
    "route": {
        "type": "choice",
        "instructions": "A coding assistant must handle this request. Pick the model that should do it.",
        "criteria": {
            "fast": "A small fast model is enough: a routine or mechanical task such as running "
            "commands, reading or searching code, simple questions, renames, small or "
            "obvious edits.",
            "strong": "Needs a large careful model: hard debugging, design or architecture "
            "decisions, multi-file refactors, subtle correctness or concurrency "
            "reasoning, or ambiguous requirements.",
        },
    }
}


class TurnMemory:
    """Thread-safe LRU of the choice held for each conversation."""

    def __init__(self, size: int = MEMORY_SIZE) -> None:
        self.size = size
        self._items: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, key: str, choice: str) -> None:
        """Stores `choice`, dropping the least recently used conversation beyond `size`."""
        with self._lock:
            self._items[key] = choice
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)

    def get(self, key: str) -> str | None:
        """The held choice of a conversation, marking it as recently used."""
        with self._lock:
            choice = self._items.get(key)
            if choice is not None:
                self._items.move_to_end(key)
            return choice

    def __len__(self) -> int:
        return len(self._items)


def parse_answer(doc: object) -> tuple[str, float]:
    """`(choice, p_strong)` from a `/v1/systemone` reply; raises `KeyError`, `TypeError` or `ValueError`."""
    answer = doc["answers"]["route"]  # type: ignore[index]
    choice = str(answer["choice"])
    if choice not in ("fast", "strong"):
        raise ValueError(f"unknown choice {choice!r}")
    return choice, float(answer["probabilities"]["strong"])


class RoutePolicy:
    """Sends each turn to the fast or the strong model."""

    name = "route"

    def __init__(
        self, hal: HalSettings, classify: Callable[[str, str], tuple[str, float]], memory: TurnMemory
    ) -> None:
        self.hal, self.classify, self.memory = hal, classify, memory

    def rewrite(self, ctx: RequestContext) -> None:
        """Picks the model for the request and records the decision."""
        fast = self.hal.agent
        if not ctx.body.get("tools"):
            ctx.model = fast
            ctx.decisions[self.name] = "side -> fast"
            return
        messages = ctx.body.get("messages") or []
        if not messages:
            return
        key = conversation_key(messages)
        last = next((m for m in reversed(messages) if m.get("role") != "system"), {})
        text = user_text(last, ctx.fmt)
        if text:
            try:
                choice, p_strong = self.classify(self.hal.classifier or "", text[-STATE_CHARS:])
            except Exception as exc:  # noqa: BLE001 - REQ-GW-43: any classifier failure uses the default
                log.warning("route classifier failed (%r); using %s", exc, DEFAULT_CHOICE)
                choice, p_strong = DEFAULT_CHOICE, float("nan")
            self.memory.put(key, choice)
            ctx.decisions[self.name] = f"{choice} p_strong={p_strong:.2f}"
        else:
            choice = self.memory.get(key) or DEFAULT_CHOICE
            ctx.decisions[self.name] = f"{choice} (held)"
        if choice == "fast":
            ctx.model = fast
