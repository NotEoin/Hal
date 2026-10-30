"""Upstream connections and the streaming copy between a backend and the client.

Decisions:

- Nothing is buffered beyond one read chunk (REQ-GW-12, 13): each `read1(CHUNK_BYTES)` is
  written and flushed before the next read, so a slow client slows the upstream read.
- Client credentials never reach a backend (REQ-GW-14); the gateway adds the backend's own.
- A timeout or failure before the response starts is a 504 or 502; after it starts the only
  honest signal left is closing the connection.
- One upstream connection per request: a pool is not worth its failure modes at the measured
  0.2 to 0.4 ms overhead.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
from collections.abc import Mapping
from typing import TYPE_CHECKING

from .errors import BadGatewayError, GatewayError, UpstreamTimeout

if TYPE_CHECKING:
    from .backends.openai import UpstreamAddress

log = logging.getLogger(__name__)

CHUNK_BYTES = 65536
MAX_LIST_BYTES = 8 * 1024 * 1024  # a model list larger than this is not a model list
HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade", "proxy-authenticate"}
)
CLIENT_ONLY = frozenset({"host", "content-length", "authorization", "x-api-key", "cookie"})


class _UnixConnection(http.client.HTTPConnection):
    """HTTP over a unix socket."""

    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        """Connects to the socket path."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def request_headers(client: Mapping[str, str], backend_auth: Mapping[str, str]) -> dict[str, str]:
    """Headers to forward: the client's minus hop-by-hop, credentials and `X-Hal-*`, plus the backend's."""
    out = {}
    for name, value in client.items():
        lower = name.lower()
        if lower in HOP_BY_HOP or lower in CLIENT_ONLY or lower.startswith(("proxy-", "x-hal-")):
            continue
        out[name] = value
    out.update(backend_auth)
    return out


def response_headers(upstream: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Reply headers to pass on: everything except hop-by-hop ones."""
    return [(n, v) for n, v in upstream if n.lower() not in HOP_BY_HOP and not n.lower().startswith("proxy-")]


def connect(
    address: UpstreamAddress, connect_timeout_s: float, read_timeout_s: float
) -> http.client.HTTPConnection:
    """Opens a connection and switches it to the read timeout; raises 504 or 502 errors."""
    if address.scheme == "unix":
        conn: http.client.HTTPConnection = _UnixConnection(address.path or "", connect_timeout_s)
    elif address.scheme == "https":
        conn = http.client.HTTPSConnection(address.host, address.port, timeout=connect_timeout_s)
    else:
        conn = http.client.HTTPConnection(address.host, address.port, timeout=connect_timeout_s)
    try:
        conn.connect()
    except TimeoutError as exc:
        raise UpstreamTimeout(f"backend did not accept a connection within {connect_timeout_s:g} s") from exc
    except OSError as exc:
        raise BadGatewayError(f"cannot connect to the backend: {exc}") from exc
    if conn.sock is not None:
        conn.sock.settimeout(read_timeout_s)
    return conn


def open_response(
    conn: http.client.HTTPConnection, method: str, target: str, body: bytes | None, headers: Mapping[str, str]
) -> http.client.HTTPResponse:
    """Sends the request and returns the response head; raises 504 or 502 errors."""
    try:
        conn.request(method, target, body=body, headers=dict(headers))
        return conn.getresponse()
    except TimeoutError as exc:
        conn.close()
        raise UpstreamTimeout("backend sent no response in time") from exc
    except (OSError, http.client.HTTPException) as exc:
        conn.close()
        raise BadGatewayError(f"backend failed before answering: {exc}") from exc


def copy_body(resp: http.client.HTTPResponse, write) -> bool:  # type: ignore[no-untyped-def]  # `write` is a callable taking bytes
    """Streams the body through `write`, one chunk at a time; returns False if the stream broke."""
    while True:
        try:
            chunk = resp.read1(CHUNK_BYTES)
        except (OSError, http.client.HTTPException) as exc:
            log.warning("upstream stream broke: %s", exc)
            return False
        if not chunk:
            return True
        try:
            write(chunk)
        except OSError:
            return False  # the client went away; the caller closes the upstream


LIST_TIMEOUT_S = 5.0  # a model list is a small local call; a backend slower than this counts as down


def fetch_model_ids(backend) -> list[str]:  # type: ignore[no-untyped-def]  # an OpenAIBackend
    """Returns the model ids of `backend` from `GET /v1/models`; empty when it cannot answer."""
    try:
        conn = connect(backend.address(), LIST_TIMEOUT_S, LIST_TIMEOUT_S)
        try:
            resp = open_response(conn, "GET", "/v1/models", None, backend.auth_headers())
            doc = json.loads(resp.read(MAX_LIST_BYTES))
        finally:
            conn.close()
        return backend.clean_ids([str(m["id"]) for m in doc["data"] if isinstance(m, dict) and "id" in m])
    except (GatewayError, OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("cannot list models of backend %s: %s", backend.name, exc)
        return []


def call_json(backend, method: str, path: str, body: dict | None = None, timeout_s: float = LIST_TIMEOUT_S):  # type: ignore[no-untyped-def,type-arg]
    """Calls a backend's JSON route and returns the parsed reply; raises `GatewayError` on failure."""
    conn = connect(backend.address(), timeout_s, timeout_s)
    headers = {"Content-Type": "application/json", **backend.auth_headers()}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        resp = open_response(conn, method, path, data, headers)
        raw = resp.read(MAX_LIST_BYTES)
        if resp.status >= 400:
            raise BadGatewayError(f"backend {backend.name} answered {resp.status} for {path}")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise BadGatewayError(f"backend {backend.name} sent invalid JSON for {path}") from exc
    finally:
        conn.close()
