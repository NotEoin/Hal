"""Tests for `halkit.tomledit`."""

import tomllib

import pytest

from halkit import tomledit

BASE = """# my settings
[hal]
chat_model = "old"   # the brain
fast_model = "f"

[notify]
ntfy_topic = "t"
"""


def test_set_keys_replaces_in_place_and_keeps_the_comment():
    out = tomledit.set_keys(BASE, "hal", {"chat_model": "new"})
    assert 'chat_model = "new"   # the brain' in out and "# my settings" in out
    assert tomllib.loads(out)["notify"]["ntfy_topic"] == "t"


def test_set_keys_adds_a_missing_key_and_a_missing_table():
    out = tomledit.set_keys(BASE, "hal", {"code_model": "c"})
    assert tomllib.loads(out)["hal"]["code_model"] == "c"
    out = tomledit.set_keys(BASE, "models", {"context_cap": 4096})
    assert tomllib.loads(out)["models"] == {"context_cap": 4096}


def test_set_table_replaces_the_whole_table_only():
    once = tomledit.set_table(BASE, "models.sources.a", {"kind": "ollama", "url": "http://127.0.0.1:11434"})
    twice = tomledit.set_table(once, "models.sources.a", {"kind": "gguf", "path": "/m"})
    assert tomllib.loads(twice)["models"]["sources"]["a"] == {"kind": "gguf", "path": "/m"}
    assert tomllib.loads(twice)["hal"]["fast_model"] == "f"


def test_remove_table_drops_it_and_keeps_the_rest():
    once = tomledit.set_table(BASE, "models.sources.a", {"kind": "gguf", "path": "/m"})
    out = tomledit.remove_table(once, "models.sources.a")
    assert "models" not in tomllib.loads(out) and tomllib.loads(out)["notify"]


def test_remove_missing_table_changes_nothing():
    assert tomledit.remove_table(BASE, "nope") == BASE


def test_strings_are_escaped_and_ascii():
    value = 'a"b\\c' + chr(0xE9)  # built from the code point so this file stays ASCII
    out = tomledit.set_table("", "t", {"p": value})
    assert tomllib.loads(out)["t"]["p"] == value and out.isascii()


def test_an_edit_that_would_break_the_file_raises():
    with pytest.raises(ValueError, match="invalid TOML"):
        tomledit.set_keys("[a]\nx = 1\n[a]\ny = 2\n", "a", {"z": 1})
