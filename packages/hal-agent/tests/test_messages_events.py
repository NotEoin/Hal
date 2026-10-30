"""Tests for the wire-independent message and event model."""

from __future__ import annotations

import json

import pytest

from halagent.events import (
    REQUIRED_FIELDS,
    RESULT_DISPLAY_CAP,
    Event,
    cap_text,
    make_event,
)
from halagent.messages import Message, ModelReply, ToolCall, ToolSpec, Usage


def test_assistant_message_with_calls_round_trips_through_json():
    msg = Message(
        "assistant",
        "ok",
        reasoning="think",
        tool_calls=[ToolCall("c1", "run", {"cmd": "ls"}, ("alias:bash->run",))],
        meta={"incomplete": True},
    )
    assert Message.from_json(json.loads(json.dumps(msg.to_json()))) == msg


def test_empty_optional_fields_are_left_out_of_message_json():
    assert Message("user", "hi").to_json() == {"role": "user", "content": "hi"}


def test_tool_message_without_call_id_is_refused():
    with pytest.raises(ValueError, match="tool_call_id"):
        Message("tool", "x")


def test_user_message_with_tool_calls_is_refused():
    with pytest.raises(ValueError, match="assistant"):
        Message("user", "x", tool_calls=[ToolCall("c", "run", {})])


def test_unknown_role_is_refused():
    with pytest.raises(ValueError, match="role"):
        Message("robot", "x")  # type: ignore[arg-type]


def test_tool_call_with_string_arguments_is_refused():
    with pytest.raises(ValueError, match="object"):
        ToolCall.from_json({"id": "c", "name": "run", "arguments": "{}"})


def test_tool_spec_round_trips_and_rejects_unknown_kind():
    spec = ToolSpec("read", "Read a file", {"type": "object"}, kind="read", counts=False)
    assert ToolSpec.from_json(spec.to_json()) == spec
    with pytest.raises(ValueError, match="kind"):
        ToolSpec.from_json({**spec.to_json(), "kind": "magic"})


def test_usage_round_trips_with_missing_counts_as_zero():
    assert Usage.from_json({"prompt_tokens": 5}) == Usage(prompt_tokens=5)
    assert Usage.from_json(Usage(1, 2, 3, 4, True).to_json()) == Usage(1, 2, 3, 4, True)


def test_model_reply_with_unknown_finish_is_refused():
    with pytest.raises(ValueError, match="finish"):
        ModelReply(Message("assistant", "x"), "weird")  # type: ignore[arg-type]


def test_event_round_trips_and_stamps_utc_z():
    ev = make_event("tool_start", "abc123def456", 2, {"id": "c1"})
    assert ev.ts.endswith("Z")
    assert Event.from_json(json.loads(ev.dumps())) == ev


def test_event_missing_a_required_field_is_refused():
    with pytest.raises(ValueError, match="missing delta"):
        make_event("text", "s", 1, {"message_id": "m"})


def test_unknown_event_type_is_refused():
    with pytest.raises(ValueError, match="unknown event type"):
        make_event("bogus", "s", 1, {})


def test_every_documented_event_type_has_required_fields():
    assert len(REQUIRED_FIELDS) == 12


def test_plan_entry_with_unknown_status_is_refused():
    entry = {"content": "a", "status": "done", "priority": "high"}
    with pytest.raises(ValueError, match="status"):
        make_event("plan", "s", 1, {"entries": [entry]})


def test_notice_level_outside_the_enum_is_refused():
    with pytest.raises(ValueError, match="level"):
        make_event("notice", "s", 1, {"level": "loud", "code": "x", "text": "t"})


def test_cap_text_marks_the_cut():
    assert cap_text("a" * 10, 20) == "a" * 10
    assert cap_text("a" * (RESULT_DISPLAY_CAP + 1), RESULT_DISPLAY_CAP).endswith("[cut]")
