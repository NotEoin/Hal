"""Tests for the SSE reader and the Chat Completions stream reader."""

from __future__ import annotations

import json

import pytest

from halagent.messages import Done, ReasoningDelta, TextDelta, ToolCallDelta
from halagent.streaming import MAX_SSE_LINE, SseEvent, StreamError, chat_stream, iter_sse


def _sse(*chunks: dict | str) -> list[str]:
    out = []
    for c in chunks:
        out.append("data: " + (c if isinstance(c, str) else json.dumps(c)) + "\n")
        out.append("\n")
    return out


def _delta(**kw) -> dict:
    return {"choices": [{"delta": kw}]}


def _finish(reason: str, **extra) -> dict:
    return {"choices": [{"delta": {}, "finish_reason": reason}], **extra}


def test_sse_reader_ignores_comments_and_stops_at_done():
    lines = [": ping\n", "data: a\n", "\n", "data: [DONE]\n", "\n", "data: after\n", "\n"]
    assert list(iter_sse(lines)) == [SseEvent("", "a")]


def test_sse_reader_keeps_event_names_and_joins_multiline_data():
    lines = [b"event: message_start\r\n", b"data: x\r\n", b"data: y\r\n", b"\r\n"]
    assert list(iter_sse(lines)) == [SseEvent("message_start", "x\ny")]


def test_sse_reader_flushes_a_final_event_without_blank_line():
    assert list(iter_sse(["data: tail\n"])) == [SseEvent("", "tail")]


def test_sse_line_over_the_cap_fails_loudly():
    with pytest.raises(StreamError, match="cap"):
        list(iter_sse(["x" * (MAX_SSE_LINE + 1)]))


def test_chat_stream_assembles_text_usage_and_finish():
    lines = _sse(
        _delta(content="Hel"),
        _delta(content="lo"),
        _finish("stop", usage={"prompt_tokens": 7, "completion_tokens": 2}),
        "[DONE]",
    )
    items = list(chat_stream(iter_sse(lines)))
    assert [i.text for i in items if isinstance(i, TextDelta)] == ["Hel", "lo"]
    done = items[-1]
    assert isinstance(done, Done)
    assert done.completion.message.content == "Hello"
    assert done.completion.usage.prompt_tokens == 7
    assert done.completion.finish == "stop"


def test_chat_stream_joins_tool_call_fragments_into_a_dict():
    lines = _sse(
        _delta(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "run", "arguments": '{"cm'}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": 'd": "ls"}'}}]),
        _finish("tool_calls"),
    )
    items = list(chat_stream(iter_sse(lines)))
    assert sum(isinstance(i, ToolCallDelta) for i in items) == 2
    call = items[-1].completion.message.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("c1", "run", {"cmd": "ls"})
    assert items[-1].completion.finish == "tool_calls"


def test_chat_stream_keeps_unparsable_arguments_for_the_repair_layer():
    lines = _sse(
        _delta(tool_calls=[{"index": 0, "function": {"name": "run", "arguments": "{bad"}}]),
        _finish("tool_calls"),
    )
    call = list(chat_stream(iter_sse(lines)))[-1].completion.message.tool_calls[0]
    assert call.arguments == {"_raw": "{bad"}
    assert call.id == "call_0"


def test_server_separated_reasoning_goes_to_reasoning_deltas():
    lines = _sse(_delta(reasoning_content="hmm"), _delta(content="ok"), _finish("stop"))
    items = list(chat_stream(iter_sse(lines)))
    assert [type(i) for i in items[:2]] == [ReasoningDelta, TextDelta]
    assert items[-1].completion.message.reasoning == "hmm"


def test_inline_think_tags_split_even_across_chunks():
    lines = _sse(_delta(content="<thi"), _delta(content="nk>plan</think>Answer"), _finish("stop"))
    msg = list(chat_stream(iter_sse(lines)))[-1].completion.message
    assert (msg.content, msg.reasoning) == ("Answer", "plan")


def test_stream_without_finish_reason_fails_loudly():
    with pytest.raises(StreamError, match="finish"):
        list(chat_stream(iter_sse(_sse(_delta(content="cut off")))))


def test_non_json_chunk_fails_loudly():
    with pytest.raises(StreamError, match="bad stream chunk"):
        list(chat_stream(iter_sse(_sse("not json"))))


def test_length_finish_maps_to_length_and_keeps_the_raw_value():
    done = list(chat_stream(iter_sse(_sse(_finish("length")))))[-1].completion
    assert (done.finish, done.raw_finish) == ("length", "length")
