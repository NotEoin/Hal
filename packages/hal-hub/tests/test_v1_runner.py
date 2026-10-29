"""Tests for `halhub.v1_runner`, kept until plan step 26 deletes the module.

Nothing calls it any more: the hub facade replaced it in plan step 14.
"""

from __future__ import annotations

from halhub import v1_runner


def test_secrets_are_kept_out_of_the_log():
    assert v1_runner.redact({"username": "a", "password": "p", "api_key": "k"}) == {
        "username": "a",
        "password": "***",
        "api_key": "***",
    }


def test_near_miss_argument_names_are_mapped():
    known = {"when_text": None, "title": None}
    assert v1_runner._alias("when", known) == "when_text"
    assert v1_runner._alias("whentext", known) == "when_text"
    assert v1_runner._alias("colour", known) is None
