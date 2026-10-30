"""OpenAI-compatible chat provider over TCP or a unix socket, with optional streaming.

It returns one `Completion` per call. Timeouts are explicit: a stalled server raises
`ProviderError` rather than hanging. Tokens are never logged.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from halkit import paths

log = logging.getLogger("halagent.provider")

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 300.0  # a local model can take minutes to answer a long prompt
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})  # transient: the gateway may be swapping models
MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0


class ProviderError(Exception):
    """The model endpoint failed, timed out or answered something unusable."""

    def __init__(self, message: str, *, status: int = 0, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Seconds to wait before retry `attempt` (0-based); a server `Retry-After` wins, capped."""
    if retry_after is not None:
        return min(max(retry_after, 0.0), BACKOFF_CAP_S)
    return min(BACKOFF_BASE_S * 2**attempt, BACKOFF_CAP_S)


@dataclass
class Completion:
    """One assistant message: text and any native tool calls as `(id, name, arguments_json)`."""

    text: str = ""
    tool_calls: list[tuple[str, str, str]] = field(default_factory=list)
    finish: str = ""  # the server's finish_reason; "length" means the reply was cut off
    prompt_tokens: int = 0  # from the server's usage block, 0 when it sent none
    completion_tokens: int = 0


@dataclass(frozen=True)
class ModelInfo:
    """What the gateway's `GET /v1/models` says about one model; zero or empty when unknown."""

    context_length: int = 0
    hal_profile: str = ""


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

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        api_key: str = "",
        stream: bool = False,
        max_retries: int = MAX_RETRIES,
        sleep: Callable[[float], None] | None = None,
        extra: dict | None = None,
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        self._sleep = sleep or time.sleep
        self.extra = dict(extra or {})  # sampling and other body keys from the model profile
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

    def model_info(self) -> ModelInfo:
        """Reads `context_length` and `meta.hal_profile` of the model; empty if the server has none."""
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        path = self._prefix + ("/models" if self._prefix.endswith("/v1") else "/v1/models")
        conn = self._connect()
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                return ModelInfo()
            rows = json.loads(resp.read()).get("data") or []
        except (OSError, http.client.HTTPException, ValueError, AttributeError) as exc:
            log.info("model list unavailable (%s); using configured limits", exc)
            return ModelInfo()
        finally:
            conn.close()
        row = next((r for r in rows if isinstance(r, dict) and r.get("id") == self.model), None)
        if row is None:
            return ModelInfo()
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        length = row.get("context_length")
        return ModelInfo(
            context_length=length if isinstance(length, int) and length > 0 else 0,
            hal_profile=str(meta.get("hal_profile") or ""),
        )

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Completion:
        """Sends `messages` (and native `tools` if given) and returns the assistant reply."""
        body: dict = {**self.extra, "model": self.model, "messages": messages, "stream": self.stream}
        if self.stream:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        path = self._prefix + (
            "/chat/completions" if self._prefix.endswith("/v1") else "/v1/chat/completions"
        )
        payload = json.dumps(body)
        for attempt in range(self.max_retries + 1):
            try:
                return self._once(path, payload, headers)
            except ProviderError as exc:
                transient = exc.status in RETRY_STATUSES or exc.status == 0
                if not transient or attempt >= self.max_retries:
                    raise
                delay = backoff_delay(attempt, exc.retry_after)
                log.warning("model request failed (%s); retry %d in %.1fs", exc, attempt + 1, delay)
                self._sleep(delay)
        raise AssertionError("unreachable")  # the loop returns or raises

    def _once(self, path: str, payload: str, headers: dict[str, str]) -> Completion:
        """One request. Only a failure before any reply text is a `ProviderError` worth retrying."""
        conn = self._connect()
        try:
            conn.request("POST", path, payload, headers)
            resp = conn.getresponse()
            if resp.status != 200:
                detail = resp.read(2000).decode("utf-8", "replace")
                raise ProviderError(
                    f"model endpoint returned HTTP {resp.status}: {detail}",
                    status=resp.status,
                    retry_after=_retry_after(resp.getheader("Retry-After")),
                )
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
        usage = data.get("usage") or {}
        return Completion(
            text=msg.get("content") or "",
            tool_calls=calls,
            finish=data["choices"][0].get("finish_reason") or "",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    @staticmethod
    def _read_stream(resp: http.client.HTTPResponse) -> Completion:
        text: list[str] = []
        calls: dict[int, list[str]] = {}
        finish, usage = "", {}
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                usage = chunk.get("usage") or usage
                choice = chunk["choices"][0] if chunk.get("choices") else {}
            except (KeyError, IndexError, json.JSONDecodeError, AttributeError) as exc:
                raise ProviderError(f"bad stream chunk: {payload[:200]}") from exc
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta", {})
            if delta.get("content"):
                text.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0), ["", "", ""])
                fn = tc.get("function") or {}
                slot[0] = tc.get("id") or slot[0]
                slot[1] += fn.get("name") or ""
                slot[2] += fn.get("arguments") or ""
        out = [(s[0] or f"call_{i}", s[1], s[2] or "{}") for i, s in sorted(calls.items())]
        return Completion(
            text="".join(text),
            tool_calls=out,
            finish=finish,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


def _retry_after(value: str | None) -> float | None:
    """Parses a `Retry-After` header given in seconds; HTTP dates are ignored."""
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
