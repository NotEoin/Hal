"""Context management: token estimate, observation masking, summary compaction, state note.

`ContextManager.fit` keeps a request under the input budget in the order the design gives: send
as is, mask old tool output down to `mask_to`, summarise, then cut the largest message. Masking
runs as one sweep to `mask_to` rather than a little before each request, because every rewrite of
early context invalidates the server's prefix cache. A failing summary falls back to the state
note alone, so compaction never loses the work. The harness writes the state note; the model does
not (a model-written notes slot delayed the first edit in the gemma campaign).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources

log = logging.getLogger("halagent.context")

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 8
SAFETY_TOKENS = 512
MASK_AT, MASK_TO, SUMMARIZE_AT = 0.6, 0.4, 0.8
KEEP_RECENT = 3
CUT_KEEP_CHARS = 2_000
MIN_CUT_CHARS = 4_000
SUMMARY_MAX_OUTPUT = 1_500
NOTE_MAX_CHARS = 3_000
NOTE_TASK_CHARS = 600
SUMMARY_INPUT_CHARS = 60_000
TOOL_RESULT_PREFIX = "<tool_result"
SUMMARY_HEADER = "[hal] The earlier part of this session was compacted. Summary:\n"


def estimate_tokens(
    messages: list[dict], extra_chars: int = 0, chars_per_token: float = CHARS_PER_TOKEN
) -> int:
    """A rough token count: characters over `chars_per_token` plus a per-message overhead."""
    chars = extra_chars + sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    return int(chars / chars_per_token) + MESSAGE_OVERHEAD_TOKENS * len(messages)


@dataclass
class StateNote:
    """What the harness knows about the work so far, rendered into the compaction note."""

    task: str = ""
    plan: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    changed: dict[str, int] = field(default_factory=dict)
    last_command: str = ""
    errors: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0

    def observe(self, name: str, args: dict, result: str) -> None:
        """Records one finished tool call."""
        self.tool_calls += 1
        path = str(args.get("path", ""))
        if name == "read" and path:
            span = f"{path}:{args.get('start_line', 1)}-{args.get('end_line', '')}".rstrip("-")
            if span not in self.read:
                self.read.append(span)
        elif name in ("edit", "write") and path and not result.startswith('{"status": "error"'):
            self.changed[f"{path} ({name})"] = self.changed.get(f"{path} ({name})", 0) + 1
        elif name == "run":
            tail = "\n".join("  " + ln for ln in result.splitlines()[-5:])
            self.last_command = (
                f"{str(args.get('command', ''))[:200]} -> {result.splitlines()[0] if result else ''}\n{tail}"
            )
        elif name == "todo":
            self.plan = [ln for ln in result.splitlines()[1:]]
        if result.startswith('{"status": "error"'):
            try:
                kind = json.loads(result.split("\n")[0]).get("error_type", "error")
            except ValueError:
                kind = "error"
            key = f"{name} {path} {kind}".replace("  ", " ")
            self.errors[key] = self.errors.get(key, 0) + 1

    def render(self) -> str:
        """The note text, at most 3000 characters."""
        lines = [
            "[hal state note]",
            f"Task: {self.task[:NOTE_TASK_CHARS]}",
            "Plan: " + ("; ".join(self.plan) if self.plan else "none"),
            "Files read: " + (", ".join(self.read[-20:]) or "none"),
            "Files changed: " + (", ".join(f"{k} x{v}" for k, v in self.changed.items()) or "none"),
            f"Last command: {self.last_command or 'none'}",
            f"Tool calls so far: {self.tool_calls}",
            "Recent errors: "
            + (", ".join(f"{k} (x{v})" for k, v in list(self.errors.items())[-5:]) or "none"),
        ]
        return "\n".join(lines)[:NOTE_MAX_CHARS]


@dataclass
class Fitted:
    """The result of `fit`: new history, ids, and the compaction records to write."""

    messages: list[dict]
    ids: list[int]
    records: list[dict] = field(default_factory=list)
    summary_at: int | None = None  # position in `messages` of a new summary message


def _is_result(msg: dict) -> bool:
    return msg.get("role") == "tool" or (
        msg.get("role") == "user" and str(msg.get("content", "")).startswith(TOOL_RESULT_PREFIX)
    )


def _tool_names(messages: list[dict]) -> dict[str, tuple[str, str]]:
    names: dict[str, tuple[str, str]] = {}
    for m in messages:
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {})
            try:
                path = str(json.loads(fn.get("arguments") or "{}").get("path", ""))
            except (ValueError, AttributeError):
                path = ""
            names[call.get("id", "")] = (fn.get("name", "tool"), path)
    return names


def protected_from(messages: list[dict], keep_recent: int = KEEP_RECENT) -> int:
    """Index from which the newest `keep_recent` tool exchanges start; earlier ones may be masked."""
    tool_starts = [i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("tool_calls")]
    return max(1, tool_starts[-keep_recent]) if len(tool_starts) >= keep_recent else 1


def summary_range(messages: list[dict], limit: int) -> tuple[int, int]:
    """The (start, end) slice to summarise: the larger side of the latest user message, below `limit`."""
    last_user = max(
        (i for i, m in enumerate(messages) if m.get("role") == "user" and not _is_result(m)), default=0
    )
    if last_user <= 0 or last_user >= limit:
        return 1, limit
    before, after = (1, last_user), (last_user + 1, limit)
    return before if before[1] - before[0] >= after[1] - after[0] else after


class ContextManager:
    """Keeps requests under the input budget; `summarize` is one model call without tools."""

    def __init__(
        self,
        window: int,
        max_output: int,
        summarize: Callable[[list[dict]], str] | None = None,
        *,
        keep_recent: int = KEEP_RECENT,
        mask_at: float = MASK_AT,
        mask_to: float = MASK_TO,
        summarize_at: float = SUMMARIZE_AT,
        safety_tokens: int = SAFETY_TOKENS,
    ) -> None:
        self.mask_at, self.mask_to, self.summarize_at = mask_at, mask_to, summarize_at
        self.input_budget = window - max_output - safety_tokens
        if self.input_budget <= 0:
            raise ValueError(f"context window {window} is too small for max_output {max_output}")
        self.summarize = summarize
        self.chars_per_token: float = CHARS_PER_TOKEN  # recalibrated from the server's prompt counts
        self.keep_recent = keep_recent

    def _est(self, messages: list[dict], extra_chars: int = 0) -> int:
        return estimate_tokens(messages, extra_chars, self.chars_per_token)

    def fit(self, messages: list[dict], ids: list[int], note: StateNote, tools_chars: int = 0) -> Fitted:
        """Returns the history to send, compacted if it was over budget."""
        out = Fitted(list(messages), list(ids))
        if self._est(out.messages, tools_chars) <= self.mask_at * self.input_budget:
            return out
        self._mask(out, tools_chars)
        if self._est(out.messages, tools_chars) > self.summarize_at * self.input_budget:
            self._summarise(out, note)
        self._cut_largest(out, tools_chars)
        if self._est(out.messages, tools_chars) > self.input_budget:
            raise ContextOverflow(
                f"the request needs about {self._est(out.messages, tools_chars)} tokens, "
                f"the budget is {self.input_budget}"
            )
        return out

    def _mask(self, out: Fitted, tools_chars: int) -> None:
        names = _tool_names(out.messages)
        limit = protected_from(out.messages, self.keep_recent)
        masked: dict[str, str] = {}
        before = self._est(out.messages, tools_chars)
        for i in range(1, limit):
            msg = out.messages[i]
            if not _is_result(msg) or str(msg["content"]).startswith("[hal] output of"):
                continue
            if self._est(out.messages, tools_chars) <= self.mask_to * self.input_budget:
                break
            name, path = names.get(msg.get("tool_call_id", ""), (msg.get("name", "tool"), ""))
            stub = (
                f"[hal] output of {name} {path} hidden to save context "
                f"({len(str(msg['content']))} characters). Call it again if you need it."
            ).replace("  ", " ")
            out.messages[i] = {**msg, "content": stub}
            masked[str(out.ids[i])] = stub
        if masked:
            after = self._est(out.messages, tools_chars)
            out.records.append(
                {
                    "type": "compaction",
                    "kind": "mask",
                    "masked": masked,
                    "before_tokens": before,
                    "after_tokens": after,
                }
            )

    def _summarise(self, out: Fitted, note: StateNote) -> None:
        start, end = summary_range(out.messages, protected_from(out.messages, self.keep_recent))
        old = out.messages[start:end]
        if len(old) < 2:
            return
        before = self._est(out.messages)
        summary, kind = "", "note"
        if self.summarize:
            try:
                summary = self.summarize(old).strip()
            except Exception:  # noqa: BLE001 - a failing summary must fall back to the note, never lose the work
                log.warning("summary compaction failed; using the state note", exc_info=True)
        text = (
            SUMMARY_HEADER + summary + "\n\n"
            if summary
            else "[hal] The earlier part of this session was compacted.\n\n"
        ) + note.render()
        kind = "summary" if summary else "note"
        first, last = out.ids[start], out.ids[end - 1]
        out.messages[start:end] = [{"role": "user", "content": text}]
        out.ids[start:end] = [-1]  # the agent assigns the id when it writes the summary message
        out.summary_at = start
        after = self._est(out.messages)
        record = {"type": "compaction", "kind": kind, "replaces": [first, last]}
        out.records.append({**record, "before_tokens": before, "after_tokens": after})

    def _cut_largest(self, out: Fitted, tools_chars: int) -> None:
        while self._est(out.messages, tools_chars) > self.input_budget:
            sizes = [(len(str(out.messages[i].get("content") or "")), i) for i in range(1, len(out.messages))
                     if i != out.summary_at and out.messages[i].get("role") != "system"]  # fmt: skip
            if not sizes:
                return
            size, i = max(sizes)
            if size < MIN_CUT_CHARS:
                return
            text = str(out.messages[i]["content"])
            note = f"\n[... {size - 2 * CUT_KEEP_CHARS} characters cut ...]\n"
            cut = text[:CUT_KEEP_CHARS] + note + text[-CUT_KEEP_CHARS:]
            out.messages[i] = {**out.messages[i], "content": cut}


class ContextOverflow(Exception):
    """The history cannot be made to fit the model's context window."""


def compaction_prompt() -> str:
    """The summary instruction text (`prompts/compaction.md`)."""
    return resources.files("halagent.prompts").joinpath("compaction.md").read_text("utf-8")


def render_for_summary(messages: list[dict]) -> str:
    """The old messages as plain text for the summary request, capped."""
    parts = []
    for m in messages:
        body = str(m.get("content") or "")
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {})
            body += f"\n[call {fn.get('name')} {fn.get('arguments')}]"
        parts.append(f"{m.get('role')}: {body}")
    text = "\n\n".join(parts)
    return text[-SUMMARY_INPUT_CHARS:]
