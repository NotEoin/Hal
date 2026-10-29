"""Tests for `halkit.redact`: key rule, value patterns, known values, depth, no mutation.

Token samples are built at run time from a prefix and filler, so no string in this file looks
like a real credential to a secret scanner.
"""

from __future__ import annotations

import base64
import copy
import random
from urllib.parse import quote

import pytest

from halkit.redact import MAX_DEPTH, REDACTED, is_secret_key, known_forms, redact, scrub_text

KNOWN = "c4n4ry-Value/Plus+1"


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "smtpPass",
        "api_key",
        "apiKey",
        "APIKey",
        "client-secret",
        "Authorization",
        "ntfy_token",
        "refresh.token",
        "session id",
        "privateKey",
        "otp",
        "cookies",
    ],
)
def test_secret_keys_are_recognised_by_whole_words(key):
    assert is_secret_key(key)


@pytest.mark.parametrize(
    "key", ["passage", "bypass", "compass", "tokenizer_path", "keyword", "key", "session", "title"]
)
def test_ordinary_keys_that_contain_secret_substrings_stay_visible(key):
    assert not is_secret_key(key)


def test_secret_key_value_is_replaced_whatever_its_type():
    value = {"password": {"nested": 1}, "token": 42, "pin": None, "passage": "keep me"}
    assert redact(value) == {
        "password": REDACTED,
        "token": REDACTED,
        "pin": REDACTED,
        "passage": "keep me",
    }


def _samples() -> dict[str, tuple[str, str]]:
    """Kind -> (text containing a synthetic secret, expected text)."""
    pem_begin = "-----BEGIN " + "RSA PRIVATE KEY-----"
    pem_end = "-----END " + "RSA PRIVATE KEY-----"
    return {
        "pem": (f"x {pem_begin}\nMIIabc\n{pem_end} y", f"x {REDACTED} y"),
        "github": ("t " + "ghp" + "_" + "A1" * 18 + " e", f"t {REDACTED} e"),
        "github-pat": ("github" + "_pat_" + "a" * 60, REDACTED),
        "anthropic": ("k=" + "sk-" + "ant-" + "x" * 24, "k=" + REDACTED),
        "openai": ("use " + "sk-" + "proj-" + "Y" * 24, "use " + REDACTED),
        "huggingface": ("hf" + "_" + "Q" * 34, REDACTED),
        "slack": ("xox" + "b-" + "1234567890-abc", REDACTED),
        "aws": ("id AK" + "IA" + "B" * 16, "id " + REDACTED),
        "google": ("AI" + "za" + "C" * 35, REDACTED),
        "telegram": ("bot 123456789:" + "D" * 35 + " ok", f"bot {REDACTED} ok"),
        "jwt": ("ey" + "J" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10, REDACTED),
        "bearer": ("Authorization: Bearer abcdefgh1234", f"Authorization: Bearer {REDACTED}"),
        "basic": ("basic dXNlcjpwYXNz", f"basic {REDACTED}"),
        "userinfo": ("https://u:p@example.org/x", f"https://{REDACTED}@example.org/x"),
        "assignment": ("body token=abc&x=1", f"body token={REDACTED}&x=1"),
        "assignment-quoted": ('password: "two words"', f"password: {REDACTED}"),
    }


@pytest.mark.parametrize("kind", sorted(_samples()))
def test_every_value_pattern_redacts_its_synthetic_sample(kind):
    text, expected = _samples()[kind]
    assert scrub_text(text) == expected


def test_known_secret_and_its_encodings_are_redacted_inside_strings():
    raw = KNOWN.encode()
    encodings = [
        KNOWN,
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
        quote(KNOWN, safe=""),
    ]
    for form in encodings:
        out = scrub_text(f"<{form}>", secrets=[KNOWN])
        assert KNOWN not in out and form.rstrip("=") not in out and REDACTED in out


def test_known_values_shorter_than_eight_characters_are_ignored():
    assert known_forms(["short"]) == ()
    assert scrub_text("a short note", secrets=["short"]) == "a short note"


def test_keys_are_scrubbed_like_values():
    assert redact({KNOWN: 1}, secrets=[KNOWN]) == {REDACTED: 1}


def test_audit_style_record_from_attack_case_atk_34_is_redacted():
    args = {
        "opts": {"password": "p"},
        "body": "token=abc",
        "url": "https://u:p@example.org",
        "passage": "text",
    }
    assert redact(args) == {
        "opts": {"password": REDACTED},
        "body": f"token={REDACTED}",
        "url": f"https://{REDACTED}@example.org",
        "passage": "text",
    }


def test_values_deeper_than_the_depth_limit_are_redacted_not_walked():
    value: object = "leaf"
    for _ in range(MAX_DEPTH + 5):
        value = [value]
    out = redact(value)
    for _ in range(MAX_DEPTH):
        assert isinstance(out, list)
        out = out[0]
    assert out == REDACTED


def test_tuples_stay_tuples_and_non_json_values_become_their_repr():
    class Thing:
        def __repr__(self) -> str:
            return f"Thing({KNOWN})"

    assert redact(("a", 1)) == ("a", 1)
    assert redact(Thing(), secrets=[KNOWN]) == f"Thing({REDACTED})"


def _random_value(rng: random.Random, depth: int = 0) -> object:
    kind = rng.randrange(7 if depth < 6 else 4)
    if kind == 0:
        return rng.choice([None, True, False])
    if kind == 1:
        return rng.randint(-(10**12), 10**12)
    if kind == 2:
        return rng.choice([0.5, -1.25, 1e300])
    if kind == 3:
        alphabet = "abc=:/@ \n\x00\u202e" + KNOWN
        return "".join(rng.choice(alphabet) for _ in range(rng.randrange(30)))
    if kind == 4:
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    if kind == 5:
        return tuple(_random_value(rng, depth + 1) for _ in range(rng.randrange(4)))
    keys = ["password", "name", "apiKey", "x", KNOWN, "token_count"]
    return {rng.choice(keys): _random_value(rng, depth + 1) for _ in range(rng.randrange(4))}


def test_redact_never_raises_and_never_mutates_random_nested_input():
    rng = random.Random(20261029)
    for _ in range(500):
        value = _random_value(rng)
        before = copy.deepcopy(value)
        out = redact(value, secrets=[KNOWN])
        assert value == before
        assert KNOWN not in repr(out)
