"""`hal tune --bench`: how fast each role answers, measured through the gateway.

Each role's alias gets one streamed chat request with a fixed prompt (a coding task for `code`,
prose for the others), as a client would send it, so the figures include the gateway, the
role's own settings and any speculative decoding. Design decisions, each with its reason:

- Decode speed is the completion tokens after the first, over the time from the first token
  to the last. Reasoning tokens count: a thinking model spends them as it answers.
- Time to first token includes a model load: the gateway loads, swaps or parks models to serve
  each role, so roles are measured one after another and the first answer after a switch pays
  for it. A second request per role shows the warm figure.
- `embed` and `classifier` are left out unless named: an embedding model does not chat, and
  Granite Guardian answers with a few tokens.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from halgateway.client import ClientError, GatewayClient

CODE_PROMPT = "Write a Python module with an LRU cache class (get, put, a decorator), with docstrings and tests."
PROSE_PROMPT = "Write a 400-word short story about a lighthouse keeper who finds a message in a bottle."
MAX_TOKENS = 400  # long enough for the decode speed to settle, short enough to bench every role
REQUEST_TIMEOUT_S = 900.0  # a cold start of a large model can take minutes
RUNS = 2  # the first may pay for a load; the second is the warm figure
SKIPPED = ("embed", "classifier")


@dataclass(frozen=True)
class Result:
    """One request to one role: seconds to the first token, decode tokens per second, tokens."""

    role: str
    run: int
    ttft_s: float = 0.0
    tok_s: float = 0.0
    tokens: int = 0
    error: str = ""


def _delta_has_text(doc: dict) -> bool:  # type: ignore[type-arg]
    choices = doc.get("choices") or []
    delta = choices[0].get("delta", {}) if choices and isinstance(choices[0], dict) else {}
    return any(delta.get(key) for key in ("content", "reasoning_content", "reasoning"))


def measure(
    client: GatewayClient, role: str, run: int = 1, *, clock: Callable[[], float] = time.monotonic
) -> Result:
    """Sends the role's prompt once to `hal-ROLE` and times the streamed answer."""
    body = {
        "model": f"hal-{role}",
        "messages": [{"role": "user", "content": CODE_PROMPT if role == "code" else PROSE_PROMPT}],
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    start, first, last, tokens = clock(), 0.0, 0.0, 0
    try:
        for raw in client.stream("/v1/chat/completions", body, timeout_s=REQUEST_TIMEOUT_S):
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            doc = json.loads(line[len("data: ") :])
            if _delta_has_text(doc):
                last = clock()
                first = first or last
            usage = doc.get("usage")
            if isinstance(usage, dict) and usage.get("completion_tokens"):
                tokens = int(usage["completion_tokens"])
    except (ClientError, ValueError) as exc:
        return Result(role, run, error=str(exc))
    if not first:
        return Result(role, run, error="no tokens came back")
    rate = (tokens - 1) / (last - first) if tokens > 1 and last > first else 0.0
    return Result(role, run, first - start, rate, tokens)


def bench(client: GatewayClient, roles: Iterable[str], runs: int = RUNS) -> list[Result]:
    """Measures each role `runs` times in turn."""
    return [measure(client, role, run) for role in roles for run in range(1, runs + 1)]


def render(results: Iterable[Result]) -> str:
    """A table: role, run, time to first token, decode speed, tokens."""
    lines = [f"{'role':<12} {'run':>3} {'first token':>12} {'tokens/s':>9} {'tokens':>7}"]
    for r in results:
        if r.error:
            lines.append(f"{r.role:<12} {r.run:>3}  failed: {r.error}")
        else:
            lines.append(f"{r.role:<12} {r.run:>3} {r.ttft_s:>11.2f}s {r.tok_s:>9.1f} {r.tokens:>7}")
    return "\n".join(lines)
