"""OpenAI-compatible chat provider over TCP or a unix socket, with optional streaming.

It returns one `Completion` per call. Timeouts are explicit: a stalled server raises
`ProviderError` rather than hanging. Tokens are never logged.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from halkit import paths

log = logging.getLogger("halagent.provider")

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 300.0  # a local model can take minutes to answer a long prompt


class ProviderError(Exception):
    """The model endpoint failed, timed out or answered something unusable."""


@dataclass
class Completion:
    """One assistant message: text and any native tool calls as `(id, name, arguments_json)`."""

    text: str = ""
    tool_calls: list[tuple[str, str, str]] = field(default_factory=list)


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class ChatProvider:
    """Calls `POST {base}/v1/chat/completions` on an endpoint."""

    def __init__(self, endpoint: str, model: str, *, api_key: str = "", stream: bool = False) -> None:
        self.model = model
        self.stream = stream
        self._api_key = api_key
        self._prefix = ""
        self._unix: str | None = None
        self._host = ""
        self._tls = False
        self._port: int | None = None
        if endpoint.startswith("unix://"):
            endpoint = endpoint[len("unix://") :]
        if endpoint.startswith("/"):
            self._unix = str(paths.check_socket_path(Path(endpoint)))
        else:
            parts = urlsplit(endpoint)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"endpoint must be an http(s) URL or a unix socket path: {endpoint!r}")
            self._host, self._port, self._tls = parts.hostname, parts.port, parts.scheme == "https"
            self._prefix = parts.path.rstrip("/")

    def _connect(self) -> http.client.HTTPConnection:
        if self._unix:
            return _UnixConnection(self._unix, READ_TIMEOUT_S)
        if self._tls:
            return http.client.HTTPSConnection(self._host, self._port, timeout=READ_TIMEOUT_S)
        return http.client.HTTPConnection(self._host, self._port, timeout=READ_TIMEOUT_S)

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Completion:
        """Sends `messages` (and native `tools` if given) and returns the assistant reply."""
        body: dict = {"model": self.model, "messages": messages, "stream": self.stream}
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        path = self._prefix + (
            "/chat/completions" if self._prefix.endswith("/v1") else "/v1/chat/completions"
        )
        conn = self._connect()
        try:
            conn.request("POST", path, json.dumps(body), headers)
            resp = conn.getresponse()
            if resp.status != 200:
                detail = resp.read(2000).decode("utf-8", "replace")
                raise ProviderError(f"model endpoint returned HTTP {resp.status}: {detail}")
            return self._read_stream(resp) if self.stream else self._parse(json.loads(resp.read()))
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise ProviderError(f"model request failed: {exc}") from exc
        finally:
            conn.close()

    @staticmethod
    def _parse(data: dict) -> Completion:
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: {str(data)[:200]}") from exc
        calls = [
            (c.get("id") or f"call_{i}", c["function"]["name"], c["function"].get("arguments") or "{}")
            for i, c in enumerate(msg.get("tool_calls") or [])
        ]
        return Completion(text=msg.get("content") or "", tool_calls=calls)

    @staticmethod
    def _read_stream(resp: http.client.HTTPResponse) -> Completion:
        text: list[str] = []
        calls: dict[int, list[str]] = {}
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                delta = json.loads(payload)["choices"][0].get("delta", {})
            except (KeyError, IndexError, json.JSONDecodeError) as exc:
                raise ProviderError(f"bad stream chunk: {payload[:200]}") from exc
            if delta.get("content"):
                text.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0), ["", "", ""])
                fn = tc.get("function") or {}
                slot[0] = tc.get("id") or slot[0]
                slot[1] += fn.get("name") or ""
                slot[2] += fn.get("arguments") or ""
        out = [(s[0] or f"call_{i}", s[1], s[2] or "{}") for i, s in sorted(calls.items())]
        return Completion(text="".join(text), tool_calls=out)
