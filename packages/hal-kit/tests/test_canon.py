"""Tests for `halkit.canon.canonical_json` (01-security "Approval binding").

The shared vectors in `tests/data/approval_vectors.json` at the repository root are read by
the tests of 01, 04 and 06 (RESOLUTIONS B1); here they check the encoding and that each
vector's key follows from its canonical text by the formula of REQ-SEC-19.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from halkit.canon import MAX_DEPTH, CanonicalJSONError, canonical_json

VECTORS = Path(__file__).resolve().parents[3] / "tests" / "data" / "approval_vectors.json"
KEY_TAG = b"hal-approval/v1\n"  # REQ-SEC-19


def _vectors() -> list[dict]:
    return json.loads(VECTORS.read_text(encoding="ascii"))


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: f"{v['pack']}-{v['tool']}-{v['key'][7:15]}")
def test_shared_approval_vectors_encode_to_their_canonical_text_and_key(vector):
    canonical = canonical_json(vector["args"])
    assert canonical == vector["canonical"].encode("ascii")
    digest = hashlib.sha256(
        KEY_TAG + vector["pack"].encode() + b"\n" + vector["tool"].encode() + b"\n" + canonical
    )
    assert "sha256:" + digest.hexdigest() == vector["key"]


def test_shared_vectors_cover_reordered_keys_and_a_trailing_space():
    keys = [v["key"] for v in _vectors()]
    assert len(keys) >= 4 and len(set(keys)) == len(keys) - 1


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
