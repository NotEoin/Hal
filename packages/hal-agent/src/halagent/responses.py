"""Responses provider: `POST /v1/responses`, stateless, with the agent's Chat Completions history.

The loop keeps its history in Chat Completions shape; this provider converts it to Responses
input items (`store: false`, the full input each time) and the reply back. Constraints:
streaming goes through `responses_stream`; reasoning items are not replayed yet.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from halagent.provider import ChatProvider, Completion, ProviderError
from halagent.streaming import StreamError, iter_sse, responses_stream

log = logging.getLogger("halagent.responses")

DEFAULT_MAX_OUTPUT = 4096  # the loop overrides this with the settings


class ResponsesProvider(ChatProvider):
    """Calls `POST {base}/v1/responses`."""

    route = "/v1/responses"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_output = DEFAULT_MAX_OUTPUT

    def _body(self, messages: list[dict], tools: list[dict] | None) -> dict:
        instructions, items = convert_messages(messages)
        body: dict = {
            "max_output_tokens": self.max_output, **self.extra,
            "model": self.model, "input": items, "store": False,
        }  # fmt: skip
        if self.stream:
            body["stream"] = True
        if instructions:
            body["instructions"] = instructions
        if tools:
            body["tools"] = [{"type": "function", **t["function"]} for t in tools]
        return body

    def _decode(self, resp: Any) -> Completion:
        if self.stream:
            try:
                return self._collect(responses_stream(iter_sse(resp)))
            except StreamError as exc:
                raise ProviderError(f"bad stream: {exc}") from exc
        return self._parse(json.loads(resp.read()))

    @staticmethod
    def _parse(data: dict) -> Completion:
        output = data.get("output")
        if not isinstance(output, list):
            raise ProviderError(f"unexpected response shape: {str(data)[:200]}")
        text = "".join(
            part.get("text", "")
            for item in output
            if item.get("type") == "message"
            for part in item.get("content") or []
            if part.get("type") == "output_text"
        )
        calls = [
            (item.get("call_id") or f"call_{i}", item["name"], item.get("arguments") or "{}")
            for i, item in enumerate(i for i in output if i.get("type") == "function_call")
        ]
        incomplete = data.get("status") == "incomplete"
        reason = (data.get("incomplete_details") or {}).get("reason")
        finish = (
            "length" if incomplete and reason == "max_output_tokens" else "tool_calls" if calls else "stop"
        )
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            tool_calls=calls,
            finish="filtered" if incomplete and reason == "content_filter" else finish,
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
        )


def convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Returns `(instructions, input items)` for a Chat Completions history."""
    system: list[str] = []
    items: list[dict] = []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role == "system":
            system.append(str(content))
        elif role == "user":
            items.append({"role": "user", "content": str(content)})
        elif role == "tool":
            items.append(
                {"type": "function_call_output", "call_id": m["tool_call_id"], "output": str(content)}
            )
        elif role == "assistant":
            if content:
                items.append({"role": "assistant", "content": str(content)})
            for c in m.get("tool_calls") or []:
                fn = c["function"]
                items.append({
                    "type": "function_call", "call_id": c["id"], "name": fn["name"],
                    "arguments": fn.get("arguments") or "{}",
                })  # fmt: skip
        else:
            raise ValueError(f"unknown message role {role!r}")
    return "\n\n".join(system), items
