"""Tests for `halgateway.estimate`."""

import json
import math

from halgateway.estimate import estimate_input_tokens


def test_estimate_is_a_quarter_of_the_canonical_json_length():
    body = {"model": "m", "system": "abcd", "messages": [{"role": "user", "content": "hi"}], "tools": []}
    counted = {"system": "abcd", "messages": [{"role": "user", "content": "hi"}], "tools": []}
    text = json.dumps(counted, sort_keys=True, separators=(",", ":"))
    assert estimate_input_tokens(body) == math.ceil(len(text) / 4)


def test_estimate_ignores_fields_that_are_not_prompt_text():
    assert estimate_input_tokens({"model": "x" * 1000, "max_tokens": 5}) == math.ceil(len("{}") / 4)
