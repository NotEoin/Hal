"""Token counting: server counts where the endpoint offers them, else a calibrated estimate.

Constraints: the box forwarder allows only the Responses and Messages count routes, so the
Chat Completions count route is never tried through it; a 403, 404 or 405 marks a route missing
for the process (RESOLUTIONS D3). A server count costs a template render, so counting is skipped
while the last exact count is below half of the window (REQ-AGT-47).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable

log = logging.getLogger("halagent.tokens")

COUNT_PATHS = {
    "openai": "/v1/chat/completions/input_tokens",
    "responses": "/v1/responses/input_tokens",
    "anthropic": "/v1/messages/count_tokens",
}
MISSING_STATUSES = frozenset({403, 404, 405})
ESTIMATE_FACTOR = 1.15  # covers template overhead and tool schemas the character count misses
INITIAL_RATIO = 0.25  # tokens per character until the first response
COUNT_SKIP_FRACTION = 0.5  # below this share of the window an estimate is good enough

PostJson = Callable[[str, dict], tuple[int, object]]


class TokenCounter:
    """Counts prompt tokens for one model; `post` sends a JSON body and returns `(status, body)`."""

    def __init__(self, api: str, window: int, post: PostJson | None = None) -> None:
        if api not in COUNT_PATHS:
            raise ValueError(f"unknown api {api!r}; expected one of {sorted(COUNT_PATHS)}")
        self.api, self.window, self._post = api, window, post
        self.ratio = INITIAL_RATIO
        self.missing = False
        self.last_exact = 0

    def estimate(self, chars: int) -> int:
        """The calibrated estimate for a request of `chars` characters."""
        return math.ceil(chars * self.ratio * ESTIMATE_FACTOR)

    def observe(self, request_chars: int, prompt_tokens: int) -> None:
        """Updates the tokens-per-character ratio from a response's real prompt count."""
        if request_chars > 0 and prompt_tokens > 0:
            self.ratio = prompt_tokens / request_chars

    def count(self, body: dict) -> tuple[int, bool]:
        """Returns `(tokens, exact)` for the request `body` already in this API's wire format."""
        chars = len(json.dumps(body, ensure_ascii=False))
        if self._post is None or self.missing:
            return self.estimate(chars), False
        if 0 < self.last_exact < self.window * COUNT_SKIP_FRACTION:
            return self.estimate(chars), False  # far from the limit: skip the server render
        status, data = self._post(COUNT_PATHS[self.api], body)
        if status in MISSING_STATUSES:
            log.info("%s is not available (HTTP %d); using the estimate", COUNT_PATHS[self.api], status)
            self.missing = True
            return self.estimate(chars), False
        tokens = _read_count(data) if status == 200 else None
        if tokens is None:
            return self.estimate(chars), False
        self.last_exact = tokens
        return tokens, True


def _read_count(data: object) -> int | None:
    """The token count in a count-route reply (`input_tokens` or `total`), or None."""
    if not isinstance(data, dict):
        return None
    for key in ("input_tokens", "total", "tokens"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None
