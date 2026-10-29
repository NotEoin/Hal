"""Tests for `halkit.canon.canonical_json` (01-security "Approval binding")."""

from __future__ import annotations

import pytest

from halkit.canon import MAX_DEPTH, CanonicalJSONError, canonical_json


def test_keys_are_sorted_and_whitespace_is_dropped():
    assert canonical_json({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'


def test_key_order_of_the_input_does_not_change_the_encoding():
    assert canonical_json({"to": "a", "body": "b"}) == canonical_json({"body": "b", "to": "a"})


def test_booleans_stay_booleans_and_never_encode_as_numbers():
    assert canonical_json({"x": True}) == b'{"x":true}'
    assert canonical_json({"x": True}) != canonical_json({"x": 1})


def test_floats_use_the_shortest_round_trip_form():
    assert canonical_json([0.1, 1.0, 1e300]) == b"[0.1,1.0,1e+300]"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_refused(bad):
    with pytest.raises(CanonicalJSONError, match="finite"):
        canonical_json({"x": bad})


def test_non_ascii_text_is_escaped_and_never_normalised():
    composed = canonical_json({"name": "\u00e9"})
    decomposed = canonical_json({"name": "e\u0301"})
    assert composed == b'{"name":"\\u00e9"}'
    assert composed != decomposed
    assert canonical_json("a ") != canonical_json("a")


@pytest.mark.parametrize("bad", [{1: "a"}, ("a",), {"x": {1, 2}}, {"x": b"raw"}, object()])
def test_values_that_json_dumps_would_coerce_silently_are_refused(bad):
    with pytest.raises(CanonicalJSONError):
        canonical_json(bad)


def test_nesting_deeper_than_the_limit_is_refused():
    value: object = 1
    for _ in range(MAX_DEPTH + 1):
        value = [value]
    with pytest.raises(CanonicalJSONError, match="deeper"):
        canonical_json(value)


def test_routine_step_lists_encode_as_lists_of_objects():
    steps = [{"tool": "lights_on", "args": {"room": "hall"}}]
    assert canonical_json(steps) == b'[{"args":{"room":"hall"},"tool":"lights_on"}]'
