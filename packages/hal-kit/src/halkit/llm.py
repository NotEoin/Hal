"""Gateway chat and embedding client for host-side code (OpenAI Chat Completions routes).

Host packs, the reviewer and the risk classifier reach models only through here, and through
here only to the Hal gateway (REQ-SYS-27). Design decisions:

- Endpoint order is `[gateway].url` (client mode, with its token file), then the gateway's unix
  socket in `runtime_dir()`, then Ollama's `/v1` only when no `hal-gateway` distribution is
  installed. A Hal install with the gateway stopped raises `ModelUnavailable`, because a silent
  direct call would bypass the gateway's scheduling and audit (D16).
- Standard library only (`http.client`): the client runs in every pack and must not add a
  dependency. Every call has a timeout; none is retried, the caller decides.
- `[gateway]` is read from the raw config file, not a registered table: the gateway package
  owns that table and its validation, and this module needs only two optional keys.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

from halkit import config, paths, secrets

log = logging.getLogger(__name__)

SOCKET_NAME = "gateway.sock"
GATEWAY_DIST = "hal-gateway"
MAX_BODY_BYTES = 8 * 1024 * 1024  # a reply larger than this is a runaway model, not an answer
UNAVAILABLE_STATUSES = frozenset({502, 503})  # backend down or `hal off`
ERROR_EXCERPT_CHARS = 300


class GatewayError(Exception):
    """The gateway answered with an error status."""

    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ModelUnavailable(GatewayError):
    """No model can be reached: the gateway is down, the backend is down or Hal is off."""


@dataclass(frozen=True)
class _Endpoint:
    kind: str  # "unix" or "tcp"
    target: str  # socket path, or host:port
    prefix: str  # path prefix, "" or "/v1"
    token: str = ""
    https: bool = False


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _gateway_installed() -> bool:
    try:
        metadata.distribution(GATEWAY_DIST)
    except metadata.PackageNotFoundError:
        return False
    return True


def _client_section() -> Mapping[str, object]:
    section = config.read_toml(paths.config_file()).get("gateway", {})
    return section if isinstance(section, Mapping) else {}


def _endpoint() -> _Endpoint:
    section = _client_section()
    url = section.get("url")
    if isinstance(url, str) and url:
        parts = urlsplit(url)
        token_file = section.get("token_file")
        token = secrets.read_secret(Path(str(token_file))) if token_file else ""
        if not parts.hostname:
            raise ModelUnavailable(f"[gateway].url {url!r} has no host")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return _Endpoint(
            "tcp", f"{parts.hostname}:{port}", parts.path.rstrip("/"), token, parts.scheme == "https"
        )
    sock = paths.runtime_dir() / SOCKET_NAME
    if sock.exists():
        return _Endpoint("unix", str(sock), "")
    if not _gateway_installed():
        base = urlsplit(str(config.hal()["ollama_url"]))
        return _Endpoint("tcp", f"{base.hostname}:{base.port or 80}", "")
    raise ModelUnavailable("the Hal gateway is not running; start hal-gateway.service")


def _connect(endpoint: _Endpoint, timeout: float) -> http.client.HTTPConnection:
    if endpoint.kind == "unix":
        return _UnixConnection(endpoint.target, timeout)
    host, _, port = endpoint.target.rpartition(":")
    cls = http.client.HTTPSConnection if endpoint.https else http.client.HTTPConnection
    return cls(host, int(port), timeout=timeout)


def _post(path: str, body: Mapping[str, object] | None, timeout: float) -> http.client.HTTPResponse:
    """Sends one JSON request (a GET when `body` is None); returns a 200 response, else raises."""
    endpoint = _endpoint()
    headers = {"Content-Type": "application/json"}
    if endpoint.token:
        headers["Authorization"] = f"Bearer {endpoint.token}"
    conn = _connect(endpoint, timeout)
    try:
        conn.request(
            "GET" if body is None else "POST",
            endpoint.prefix + path,
            None if body is None else json.dumps(body),
            headers,
        )
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        conn.close()
        raise ModelUnavailable(f"cannot reach the gateway: {type(exc).__name__}: {exc}") from exc
    if resp.status != 200:
        text = resp.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        conn.close()
        cls = ModelUnavailable if resp.status in UNAVAILABLE_STATUSES else GatewayError
        raise cls(f"gateway answered {resp.status}: {text[:ERROR_EXCERPT_CHARS]}", resp.status, text)
    return resp


def _json(resp: http.client.HTTPResponse) -> dict[str, object]:
    try:
        raw = resp.read(MAX_BODY_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise ModelUnavailable(f"gateway reply failed: {type(exc).__name__}: {exc}") from exc
    finally:
        resp.close()
    if len(raw) > MAX_BODY_BYTES:
        raise GatewayError("gateway reply is too large", 200)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise GatewayError(
            "gateway reply is not JSON", 200, raw[:ERROR_EXCERPT_CHARS].decode("utf-8", "replace")
        ) from exc
    if not isinstance(data, dict):
        raise GatewayError("gateway reply is not a JSON object", 200)
    return data


def chat(
    messages: Sequence[Mapping[str, object]],
    *,
    model: str = "hal-chat",
    timeout: float,
    max_tokens: int | None = None,
    temperature: float | None = None,
    options: Mapping[str, object] | None = None,
) -> str:
    """Returns the assistant text for `messages`; raises `GatewayError` or `ModelUnavailable`.

    `options` is sent as the Ollama `options` object (for example `{"num_gpu": 0}`); the gateway
    drops it for backends that do not take it.
    """
    body: dict[str, object] = {"model": model, "messages": list(messages)}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    if options:
        body["options"] = dict(options)
    data = _json(_post("/v1/chat/completions", body, timeout))
    try:
        content = data["choices"][0]["message"]["content"]  # type: ignore[index]  # shape checked by the except
    except (KeyError, IndexError, TypeError) as exc:
        raise GatewayError("gateway reply has no choices[0].message.content", 200) from exc
    return content if isinstance(content, str) else ""


def stream_chat(
    messages: Sequence[Mapping[str, object]], *, model: str = "hal-chat", timeout: float
) -> Iterator[str]:
    """Yields the assistant text in chunks as the gateway streams it."""
    body = {"model": model, "messages": list(messages), "stream": True}
    resp = _post("/v1/chat/completions", body, timeout)
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                delta = json.loads(payload)["choices"][0]["delta"].get("content")
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                raise GatewayError(
                    "gateway stream chunk is malformed", 200, payload[:ERROR_EXCERPT_CHARS]
                ) from exc
            if delta:
                yield delta
    except (OSError, http.client.HTTPException) as exc:
        raise ModelUnavailable(f"gateway stream failed: {type(exc).__name__}: {exc}") from exc
    finally:
        resp.close()


def embed(texts: Sequence[str], *, model: str = "hal-embed", timeout: float) -> list[list[float]]:
    """Returns one vector per text, in order."""
    body = {"model": model, "input": list(texts)}
    data = _json(_post("/v1/embeddings", body, timeout))
    try:
        rows = sorted(data["data"], key=lambda row: row["index"])  # type: ignore[index,union-attr]  # shape checked below
        vectors = [[float(x) for x in row["embedding"]] for row in rows]
    except (KeyError, TypeError, ValueError) as exc:
        raise GatewayError("gateway reply has no embeddings", 200) from exc
    if len(vectors) != len(texts):
        raise GatewayError(f"gateway returned {len(vectors)} vectors for {len(texts)} texts", 200)
    return vectors


def models(*, timeout: float) -> list[dict[str, object]]:
    """Returns the gateway's model rows (`id` plus whatever else it lists, such as a context size)."""
    data = _json(_post("/v1/models", None, timeout))
    rows = data.get("data")
    if not isinstance(rows, list):
        raise GatewayError("gateway reply has no model list", 200)
    return [row for row in rows if isinstance(row, dict) and "id" in row]


def ping(*, timeout: float) -> list[str]:
    """Returns the model aliases the gateway lists; raises when it cannot be reached."""
    return [str(row["id"]) for row in models(timeout=timeout)]
