"""Token estimate for backends that cannot count (REQ-GW-18).

The rule is `ceil(len(canonical JSON of system, messages and tools) / 4)`: about four characters
per token, cheap and stable. It is an estimate and the reply says so (`X-Hal-Estimate`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping

CHARS_PER_TOKEN = 4  # the usual rule of thumb for English text and code
ESTIMATE_HEADER = ("X-Hal-Estimate", "chars/4")
COUNTED_FIELDS = ("system", "messages", "tools")


def estimate_input_tokens(body: Mapping[str, object]) -> int:
    """Returns the estimated input tokens of a Messages-style request body."""
    counted = {key: body[key] for key in COUNTED_FIELDS if key in body}
    text = json.dumps(counted, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return math.ceil(len(text) / CHARS_PER_TOKEN)
