"""Recursive secret redaction for logs, audit records and tool output.

The rule set is 01-security's "Redaction rules" (REQ-SEC-45 to REQ-SEC-47):

- a key rule: a mapping key whose words name a secret (`password`, `smtpPass`, `api_key`,
  `client-secret`, `Authorization`) has its whole value replaced, whatever its type. Keys are
  split into words, not searched for substrings, so `passage`, `bypass`, `compass`, `keyword`
  and `tokenizer_path` stay visible;
- value patterns: known token formats, auth header values, URL userinfo and `name=value`
  assignments are replaced inside every string, keys included;
- known values: each exact secret the host holds (8 characters or more), and its base64 and
  percent-encoded forms, is replaced wherever it appears.

Redaction never raises on JSON-like input and never mutates it: it returns a new object.
Values deeper than `MAX_DEPTH` become `"[redacted]"` rather than being walked, so a hostile
structure cannot exhaust the stack. Non-JSON values are converted with `repr` before matching,
because a log line built from them would show that text anyway.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Collection, Iterable, Mapping
from urllib.parse import quote

REDACTED = "[redacted]"
MAX_DEPTH = 32  # 01-security "Redaction rules"; also the broker's JSON nesting limit (REQ-SEC-65)
MIN_KNOWN_SECRET_CHARS = 8  # shorter values would redact ordinary words; 01 "Redaction rules"

# 01-security "Redaction rules", key rule: single words, then adjacent word pairs.
SECRET_WORDS = frozenset(
    "password passwd passphrase passcode pass pwd secret secrets token tokens apikey credential "
    "credentials cookie cookies authorization auth bearer otp totp pin privatekey sessionid".split()
)
SECRET_PAIRS = frozenset(
    {
        ("api", "key"),
        ("access", "key"),
        ("secret", "key"),
        ("private", "key"),
        ("client", "secret"),
        ("app", "password"),
        ("session", "id"),
        ("refresh", "token"),
    }
)

# A camelCase-aware word splitter: "smtpPass" -> smtp, pass; "APIKey" -> api, key.
_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# 01-security "Redaction rules", value patterns, in table order. Each entry is the compiled
# pattern and its replacement; the token prefixes are the vendors' documented formats
# [unverified per vendor: 01 asks for a check against current vendor docs].
VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        REDACTED,
    ),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), REDACTED),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|pass|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\b"
            r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
        ),
        r"\1\2" + REDACTED,
    ),
)


def key_words(key: str) -> list[str]:
    """The lowercase words of `key`, split at `_`, `-`, `.`, spaces and camelCase boundaries."""
    return [word.lower() for word in _WORD_RE.findall(key)]


def is_secret_key(key: object) -> bool:
    """True when a mapping key names a secret under 01-security's word-based key rule."""
    words = key_words(str(key))
    if any(word in SECRET_WORDS for word in words):
        return True
    return any(pair in SECRET_PAIRS for pair in zip(words, words[1:], strict=False))


def known_forms(secrets: Iterable[str]) -> tuple[str, ...]:
    """Every literal to replace for `secrets`: each value and its encodings, longest first.

    Values shorter than `MIN_KNOWN_SECRET_CHARS` are skipped. Base64 forms drop their `=`
    padding so they also match where the encoder ran on and padding was never written.
    """
    forms: set[str] = set()
    for secret in secrets:
        if not isinstance(secret, str) or len(secret) < MIN_KNOWN_SECRET_CHARS:
            continue
        raw = secret.encode("utf-8")
        forms.add(secret)
        forms.add(base64.b64encode(raw).decode("ascii").rstrip("="))
        forms.add(base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="))
        forms.add(quote(secret, safe=""))
    return tuple(sorted(forms, key=len, reverse=True))


def scrub_text(text: str, *, secrets: Collection[str] = ()) -> str:
    """`text` with known secret values and every value-pattern match replaced by `[redacted]`."""
    return _scrub(text, known_forms(secrets))


def redact(value: object, *, secrets: Collection[str] = ()) -> object:
    """A redacted copy of a JSON-like `value`; dicts, lists and tuples are walked to depth 32.

    The value of every secret key becomes `"[redacted]"`; strings (keys included) go through
    `scrub_text`'s rules. The input is never modified.
    """
    return _redact(value, known_forms(secrets), 0)


def _scrub(text: str, forms: tuple[str, ...]) -> str:
    for form in forms:
        if form in text:
            text = text.replace(form, REDACTED)
    for pattern, replacement in VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact(value: object, forms: tuple[str, ...], depth: int) -> object:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _scrub(value, forms)
    if depth >= MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            name = _scrub(str(key), forms)
            out[name] = REDACTED if is_secret_key(key) else _redact(item, forms, depth + 1)
        return out
    if isinstance(value, list | tuple):
        items = [_redact(item, forms, depth + 1) for item in value]
        return tuple(items) if isinstance(value, tuple) else items
    return _scrub(repr(value), forms)
