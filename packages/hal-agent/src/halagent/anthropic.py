"""Anthropic Messages provider: `POST /v1/messages` with the agent's Chat Completions history.

The loop keeps its history in Chat Completions shape; this provider converts it on the way out
(system text to `system`, tool calls to `tool_use` blocks, tool results to `tool_result` blocks
in a user message) and the reply on the way back. Streaming goes through `anthropic_stream`.
`max_tokens` is required by the API, so it defaults from the settings.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from halagent.provider import ChatProvider, Completion, ProviderError
from halagent.streaming import StreamError, anthropic_stream, iter_sse

log = logging.getLogger("halagent.anthropic")

DEFAULT_MAX_TOKENS = 4096  # the API requires max_tokens; the loop overrides this with the settings
API_VERSION = "2023-06-01"
FINISH_MAP = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "refusal": "refusal"}


class AnthropicProvider(ChatProvider):
    """Calls `POST {base}/v1/messages`."""

    route = "/v1/messages"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_output = DEFAULT_MAX_TOKENS

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "anthropic-version": API_VERSION}
        if self._api_key:
            headers["x-api-key"] = self._api_key
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _body(self, messages: list[dict], tools: list[dict] | None) -> dict:
        system, converted = convert_messages(messages)
        body: dict = {"max_tokens": self.max_output, **self.extra, "model": self.model, "messages": converted}
        if self.stream:
            body["stream"] = True
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters", {"type": "object"}),
                }
                for t in tools
            ]
        return body

    def _decode(self, resp: Any) -> Completion:
        if self.stream:
            try:
                return self._collect(anthropic_stream(iter_sse(resp)))
            except StreamError as exc:
                raise ProviderError(f"bad stream: {exc}") from exc
        return self._parse(json.loads(resp.read()))

    @staticmethod
    def _parse(data: dict) -> Completion:
        try:
            blocks = data["content"]
            stop = data.get("stop_reason") or ""
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: {str(data)[:200]}") from exc
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [
            (b.get("id") or f"call_{i}", b["name"], json.dumps(b.get("input") or {}))
            for i, b in enumerate(b for b in blocks if b.get("type") == "tool_use")
        ]
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            tool_calls=calls,
            finish=FINISH_MAP.get(stop, stop),
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
        )


def convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Returns `(system text, Messages-API messages)` for a Chat Completions history."""
    system: list[str] = []
    out: list[dict] = []

    def push(role: str, blocks: list[dict]) -> None:
        if out and out[-1]["role"] == role:  # the API wants alternating roles
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role == "system":
            system.append(str(content))
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": str(content)}])
        elif role == "assistant":
            blocks: list[dict] = [{"type": "text", "text": str(content)}] if content else []
            for c in m.get("tool_calls") or []:
                fn = c["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}  # the loop already told the model; the history must stay valid JSON
                blocks.append({"type": "tool_use", "id": c["id"], "name": fn["name"], "input": args})
            push("assistant", blocks or [{"type": "text", "text": "(no reply)"}])
        elif role == "user":
            push("user", [{"type": "text", "text": str(content)}])
        else:
            raise ValueError(f"unknown message role {role!r}")
    return "\n\n".join(system), out
