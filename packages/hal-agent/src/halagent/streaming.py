"""Streaming readers: server-sent events to `StreamItem` objects (design 03, 3.3).

`iter_sse` turns lines into events (`event:` names, `data:` payloads, a blank line ends an
event, `[DONE]` ends the stream, `:` comments are ignored). `chat_stream` reads Chat
Completions chunks from those events, splits inline reasoning, and ends with a `Done` item
holding the assembled `ModelReply`. Timeouts and cancel belong to the transport that supplies
the lines; a stream that ends without a finish reason is marked `incomplete` and fails loudly
with `StreamError` rather than returning half a reply as complete.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from halagent.messages import (
    Done,
    FinishReason,
    Message,
    ModelReply,
    ReasoningDelta,
    StreamItem,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    Usage,
)
from halagent.reasoning import ReasoningSplitter

SSE_DONE = "[DONE]"
MAX_SSE_LINE = 4 * 1024 * 1024  # a single event larger than this is a broken or hostile server

# Chat Completions `finish_reason` values to the internal enum.
FINISH_MAP: dict[str, FinishReason] = {
    "stop": "stop",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "length": "length",
    "content_filter": "filtered",
}


class StreamError(Exception):
    """The stream was malformed or ended before a finish reason."""


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One server-sent event; `event` is empty when the server sent no `event:` line."""

    event: str
    data: str


def iter_sse(lines: Iterable[bytes | str]) -> Iterator[SseEvent]:
    """Yield events from raw lines; stops at `data: [DONE]` or the end of input."""
    event, data = "", []
    for raw in lines:
        if len(raw) > MAX_SSE_LINE:
            raise StreamError("SSE line over the size cap")
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                if payload.strip() == SSE_DONE:
                    return
                yield SseEvent(event, payload)
            event, data = "", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        payload = "\n".join(data)
        if payload.strip() != SSE_DONE:
            yield SseEvent(event, payload)


def chat_stream(events: Iterable[SseEvent], *, split_inline: bool = True) -> Iterator[StreamItem]:
    """Turn Chat Completions SSE events into stream items, ending with `Done`.

    Raises `StreamError` for a chunk that is not JSON or a stream with no finish reason.
    """
    started = time.monotonic()
    first: float | None = None
    splitter = ReasoningSplitter() if split_inline else None
    text, reasoning = [], []
    calls: dict[int, list[str]] = {}
    raw_finish, usage = "", Usage()
    for ev in events:
        try:
            chunk = json.loads(ev.data)
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
        except (json.JSONDecodeError, AttributeError, IndexError, TypeError) as exc:
            raise StreamError(f"bad stream chunk: {ev.data[:200]}") from exc
        if chunk.get("usage"):
            usage = _usage(chunk["usage"])
        raw_finish = choice.get("finish_reason") or raw_finish
        think = delta.get("reasoning_content") or delta.get("reasoning") or ""
        if think:
            reasoning.append(think)
            first = first if first is not None else time.monotonic() - started
            yield ReasoningDelta(think)
        for piece in _text_pieces(delta.get("content") or "", splitter):
            first = first if first is not None else time.monotonic() - started
            (reasoning if isinstance(piece, ReasoningDelta) else text).append(piece.text)
            yield piece
        for tc in delta.get("tool_calls") or []:
            index = int(tc.get("index", 0))
            fn = tc.get("function") or {}
            slot = calls.setdefault(index, ["", "", ""])
            slot[0] = tc.get("id") or slot[0]
            slot[1] += fn.get("name") or ""
            slot[2] += fn.get("arguments") or ""
            yield ToolCallDelta(index, tc.get("id") or "", fn.get("name") or "", fn.get("arguments") or "")
    for piece in _text_pieces("", splitter, final=True):
        (reasoning if isinstance(piece, ReasoningDelta) else text).append(piece.text)
        yield piece
    if not raw_finish:
        raise StreamError("stream ended without a finish reason")
    msg = Message(
        "assistant",
        "".join(text),
        reasoning="".join(reasoning),
        tool_calls=[_call(i, s) for i, s in sorted(calls.items())],
    )
    finish = FINISH_MAP.get(raw_finish, "stop")
    yield Done(ModelReply(msg, finish, usage, raw_finish, first, time.monotonic() - started))


def _text_pieces(
    chunk: str, splitter: ReasoningSplitter | None, *, final: bool = False
) -> list[TextDelta | ReasoningDelta]:
    if splitter is None:
        return [TextDelta(chunk)] if chunk else []
    pieces = splitter.finish() if final else splitter.feed(chunk)
    return [ReasoningDelta(p.text) if p.reasoning else TextDelta(p.text) for p in pieces]


def _call(index: int, slot: list[str]) -> ToolCall:
    """Parse one accumulated call; unparsable arguments are kept raw under `_raw` for repair."""
    raw = slot[2] or "{}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        args = {"_raw": raw}
    if not isinstance(args, dict):
        args = {"_raw": raw}
    return ToolCall(slot[0] or f"call_{index}", slot[1], args)


def _usage(data: dict) -> Usage:
    details = data.get("completion_tokens_details") or {}
    cached = (data.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return Usage(
        int(data.get("prompt_tokens") or 0),
        int(data.get("completion_tokens") or 0),
        int(details.get("reasoning_tokens") or 0),
        int(cached or 0),
    )
