"""Context overflow mapping (REQ-GW-40), ported from `hal-route.py`.

llama-server answers a prompt that does not fit with 400 `exceed_context_size_error`. Claude
Code compacts its history only on the text "prompt is too long", and OpenAI clients look for the
`context_length_exceeded` code, so the body is rewritten for each format. Any other body is
left alone: the function returns None and the caller relays the original bytes.
"""

from __future__ import annotations

import json

OVERFLOW_TYPE = "exceed_context_size_error"
MAX_BODY_BYTES = 1024 * 1024  # the relay reads a 400 body whole, up to this cap


def map_overflow(fmt: str, status: int, body: bytes) -> bytes | None:
    """The rewritten error body for a llama.cpp overflow in `fmt` (`anthropic` or `openai`), else None."""
    if status != 400 or fmt not in ("anthropic", "openai") or len(body) > MAX_BODY_BYTES:
        return None
    try:
        err = json.loads(body)["error"]
        if err.get("type") != OVERFLOW_TYPE:
            return None
        tokens, window = int(err["n_prompt_tokens"]), int(err["n_ctx"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if fmt == "anthropic":
        message = f"prompt is too long: {tokens} tokens > {window} maximum"
        doc: dict[str, object] = {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        }
    else:
        message = (
            f"This model's maximum context length is {window} tokens. "
            f"However, your messages resulted in {tokens} tokens."
        )
        doc = {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": "messages",
                "code": "context_length_exceeded",
            }
        }
    return json.dumps(doc).encode("ascii")
