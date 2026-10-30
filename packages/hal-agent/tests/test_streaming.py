"""Tests for the SSE reader and the Chat Completions stream reader."""

from __future__ import annotations

import json

import pytest

from halagent.messages import Done, ReasoningDelta, TextDelta, ToolCallDelta
from halagent.streaming import (
    MAX_SSE_LINE,
    SseEvent,
    StreamError,
    anthropic_stream,
    chat_stream,
    iter_sse,
    responses_stream,
)


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


def _events(*items: dict) -> list[SseEvent]:
    return [SseEvent(i["type"], json.dumps(i)) for i in items]


def test_anthropic_stream_assembles_thinking_text_and_tool_call():
    evs = _events(
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 4}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hm"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "x"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi"}},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "t1", "name": "read"},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"p":'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '"a"}'},
        },
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    )
    items = list(anthropic_stream(evs))
    reply = items[-1].completion
    assert (reply.message.content, reply.message.reasoning) == ("Hi", "hm")
    assert [(c.id, c.name, c.arguments) for c in reply.message.tool_calls] == [("t1", "read", {"p": "a"})]
    assert (reply.finish, reply.usage.prompt_tokens, reply.usage.completion_tokens) == ("tool_calls", 14, 7)
    assert reply.usage.cached_tokens == 4


def test_anthropic_stream_error_event_fails_loudly():
    with pytest.raises(StreamError, match="overloaded"):
        list(anthropic_stream(_events({"type": "error", "error": {"message": "overloaded"}})))


def test_anthropic_stream_without_stop_reason_fails_loudly():
    evs = _events({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}})
    with pytest.raises(StreamError, match="finish"):
        list(anthropic_stream(evs))


def test_anthropic_max_tokens_and_refusal_map_to_internal_finish():
    for raw, want in (("max_tokens", "length"), ("refusal", "refusal")):
        evs = _events({"type": "message_delta", "delta": {"stop_reason": raw}})
        assert list(anthropic_stream(evs))[-1].completion.finish == want


def test_responses_stream_assembles_reasoning_text_and_call():
    evs = _events(
        {"type": "response.reasoning_text.delta", "delta": "why"},
        {"type": "response.output_text.delta", "delta": "ok"},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {"type": "function_call", "call_id": "c1", "name": "grep"},
        },
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"q":"z"}'},
        {
            "type": "response.completed",
            "response": {
                "usage": {
                    "input_tokens": 9,
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 2},
                    "input_tokens_details": {"cached_tokens": 3},
                }
            },
        },
    )
    reply = list(responses_stream(evs))[-1].completion
    assert (reply.message.content, reply.message.reasoning) == ("ok", "why")
    assert reply.message.tool_calls[0].arguments == {"q": "z"}
    assert (reply.finish, reply.usage.reasoning_tokens, reply.usage.cached_tokens) == ("tool_calls", 2, 3)


def test_responses_incomplete_maps_max_output_tokens_to_length():
    evs = _events(
        {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}
    )
    reply = list(responses_stream(evs))[-1].completion
    assert (reply.finish, reply.raw_finish) == ("length", "max_output_tokens")


def test_responses_failed_event_fails_loudly():
    with pytest.raises(StreamError, match="boom"):
        list(
            responses_stream(_events({"type": "response.failed", "response": {"error": {"message": "boom"}}}))
        )


def test_responses_stream_without_terminal_event_fails_loudly():
    with pytest.raises(StreamError, match="finish"):
        list(responses_stream(_events({"type": "response.output_text.delta", "delta": "x"})))
