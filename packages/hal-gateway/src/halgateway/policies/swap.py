"""Swap scheduler (REQ-GW-46..48), ported from `SwapRouter` in `hal-route.py`.

The main (strong) and agent (fast) models take turns on the GPU. One scheduler belongs to a
router backend and serves every alias of it. Design decisions, all kept from `hal-route.py`:

- A model is swapped out only when no request is in flight and `quiet_s` has passed, so a
  burst of requests does not thrash. The agent model is also held while a subagent
  conversation is active; one with no request for `agent_stale_s` no longer holds it (a stuck
  or killed subagent), and a long test run between two requests stays well inside that.
- Requests without tools and under `big_chars` are side requests: they run on whichever model
  is loaded, and when none is loaded the main model is loaded first.
- Leaving the main model saves its KV slots `0..swap_slots-1` to disk and arriving back
  restores them, so the long prompt is not reprocessed.
- New: a wait is bounded by `swap_wait_s` and ends in `SwapTimeout` (503 with Retry-After).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

QUIET_S = 2.0  # seconds without a request before the loaded model may be swapped out
AGENT_STALE_S = 900.0
BIG_CHARS = 50_000  # larger requests without tools are the conversation's own (compaction)
LOAD_TIMEOUT_S = 900.0
STATUS_POLL_S = 0.5
WAIT_POLL_S = 1.0
DONE_REASONS = ("end_turn", "stop_sequence", "stop")  # a subagent that ends this way is done
STOP_REASON = re.compile(rb'"(?:stop_reason|finish_reason)"\s*:\s*"(\w+)"')


class SwapError(Exception):
    """A swap could not finish; the request that triggered it gets 503."""


class SwapTimeout(SwapError):
    """A request waited longer than `swap_wait_s` for the other model."""


class RouterOps(Protocol):
    """What the scheduler needs from the router."""

    def swap_call(self, path: str, body: dict, check: bool = True) -> dict:  # type: ignore[type-arg]
        """POSTs to the router; with `check`, raises `SwapError` on an HTTP error."""

    def statuses(self) -> dict[str, str]:
        """Preset name -> status."""


@dataclass(frozen=True)
class Ticket:
    """An admitted request: the model it runs on, and its subagent key when it runs on the agent."""

    model: str
    key: str | None
    swap: str | None = None  # the swap this request waited for, e.g. `a -> b in 12.0s`


def last_stop_reason(tail: bytes) -> str | None:
    """The last `stop_reason` or `finish_reason` in the tail of a reply."""
    found = STOP_REASON.findall(tail)
    return found[-1].decode("ascii") if found else None


class SwapScheduler:
    """Admits requests to the main or agent model and swaps them when needed."""

    def __init__(
        self,
        ops: RouterOps,
        main: str,
        agent: str,
        *,
        slots: int = 2,
        swap_wait_s: float = 7200.0,
        quiet_s: float = QUIET_S,
        agent_stale_s: float = AGENT_STALE_S,
        load_timeout_s: float = LOAD_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        wait_poll_s: float = WAIT_POLL_S,
        status_poll_s: float = STATUS_POLL_S,
    ) -> None:
        self.ops, self.main, self.agent, self.slots = ops, main, agent, slots
        self.swap_wait_s, self.quiet_s, self.agent_stale_s = swap_wait_s, quiet_s, agent_stale_s
        self.load_timeout_s, self.clock = load_timeout_s, clock
        self.wait_poll_s, self.status_poll_s = wait_poll_s, status_poll_s
        self.cond = threading.Condition()
        self.active: str | None = None
        self._known = False
        self.inflight = 0
        self.switching = False
        self.last_done = float("-inf")
        self.agents: dict[str, float] = {}
        self.saved: list[int] = []

    # --- admission --------------------------------------------------------------------------

    def owner(self, model: str, body: dict) -> str | None:  # type: ignore[type-arg]
        """The model a request must run on, or None when either will do (REQ-GW-48)."""
        if model not in (self.main, self.agent):
            return None
        if body.get("tools") or len(json.dumps(body.get("messages") or [])) > BIG_CHARS:
            return model
        return None

    def forget(self) -> None:
        """Drops the cached active model, e.g. after a reclaim emptied the router."""
        with self.cond:
            self._known = False

    def loaded_model(self) -> str | None:
        """Which of the two models the router reports as loaded."""
        try:
            statuses = self.ops.statuses()
        except (OSError, ValueError, SwapError):
            return None
        return next((m for m in (self.main, self.agent) if statuses.get(m) == "loaded"), None)

    def admit(self, model: str, body: dict, key: str | None) -> Ticket:  # type: ignore[type-arg]
        """Blocks until `model` is loaded and quiet to use; raises `SwapTimeout` or `SwapError`.

        `key` identifies the conversation (used for subagents on the agent model).
        """
        owner = self.owner(model, body)
        want = owner or self.main
        deadline = self.clock() + self.swap_wait_s
        with self.cond:
            if not self._known:
                self.active, self._known = self.loaded_model(), True
            while True:
                if not self.switching:
                    if owner is None and self.active is not None:
                        self.inflight += 1
                        return Ticket(self.active, None)
                    if want == self.active:
                        self.inflight += 1
                        self._touch(owner, key)
                        return Ticket(want, key if owner == self.agent else None)
                    if self.active is None or self.can_leave():
                        self.switching = True
                        break
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise SwapTimeout(f"waited {self.swap_wait_s:.0f} s for the {want} model")
                self.cond.wait(min(self.wait_poll_s, remaining))
        try:
            swapped = self.switch(want)
        except (SwapError, OSError, ValueError) as exc:
            log.warning("swap to %s failed: %s", want, exc)
            with self.cond:
                self.active = self.loaded_model()  # re-read, so the next request starts from the truth
                self.switching = False
                self.cond.notify_all()
            raise SwapError(f"could not load {want}: {exc}") from exc
        with self.cond:  # claimed under the lock, so a waiting request cannot swap straight back
            self.active, self.switching = want, False
            self.inflight += 1
            self._touch(owner, key)
            self.cond.notify_all()
        return Ticket(want, key if owner == self.agent else None, swapped)

    def _touch(self, owner: str | None, key: str | None) -> None:
        if owner == self.agent and key is not None:
            self.agents[key] = self.clock()

    def finish(self, ticket: Ticket, stop_reason: str | None) -> None:
        """Ends a request; a subagent that answered without asking for a tool is done."""
        with self.cond:
            self.inflight -= 1
            self.last_done = self.clock()
            if ticket.key is not None:
                if stop_reason in DONE_REASONS:
                    self.agents.pop(ticket.key, None)
                else:
                    self.agents[ticket.key] = self.clock()
            self.cond.notify_all()

    def can_leave(self) -> bool:
        """Whether the loaded model may be swapped out now; called with the lock held."""
        now = self.clock()
        if self.inflight or now - self.last_done < self.quiet_s:
            return False
        if self.active == self.agent:
            self.agents = {k: t for k, t in self.agents.items() if now - t < self.agent_stale_s}
            return not self.agents
        return True

    # --- the swap itself --------------------------------------------------------------------

    def switch(self, target: str) -> str:
        """Unloads the active model and loads `target`; returns the decision text."""
        start = self.clock()
        leaving = self.active
        if leaving == self.main:
            self.saved = self.save_slots()
        if leaving is not None:
            self.ops.swap_call("/models/unload", {"model": leaving}, check=False)
            self.wait_status(leaving, "unloaded")
        self.ops.swap_call("/models/load", {"model": target})
        self.wait_status(target, "loaded")
        restored = self.restore_slots() if target == self.main else 0
        text = f"{leaving} -> {target} in {self.clock() - start:.1f}s"
        if target == self.main:
            text += f", restored {restored} tokens"
        log.info("swap %s", text)
        return text

    def save_slots(self) -> list[int]:
        """Saves the main model's KV slots; returns the indices that held something."""
        saved = []
        for index in range(self.slots):
            try:
                reply = self.ops.swap_call(
                    f"/slots/{index}?action=save",
                    {"model": self.main, "filename": f"{self.main}-{index}.bin"},
                )
                if reply.get("n_saved"):
                    saved.append(index)
            except (OSError, ValueError, SwapError) as exc:
                log.warning("saving slot %d failed: %s", index, exc)
        return saved

    def restore_slots(self) -> int:
        """Restores the saved slots; returns the tokens restored."""
        tokens = 0
        for index in self.saved:
            try:
                reply = self.ops.swap_call(
                    f"/slots/{index}?action=restore",
                    {"model": self.main, "filename": f"{self.main}-{index}.bin"},
                )
                tokens += int(reply.get("n_restored", 0))
            except (OSError, ValueError, SwapError) as exc:
                log.warning("restoring slot %d failed: %s; its prompt will be reprocessed", index, exc)
        return tokens

    def wait_status(self, name: str, value: str) -> None:
        """Polls until preset `name` has status `value`; raises `SwapError` on `failed` or timeout."""
        deadline = self.clock() + self.load_timeout_s
        while self.clock() < deadline:
            status = self.ops.statuses().get(name)
            if status == value:
                return
            if status == "failed":
                raise SwapError(f"{name} failed to load")
            time.sleep(self.status_poll_s)
        raise SwapError(f"{name} not {value} after {self.load_timeout_s:.0f} s")
