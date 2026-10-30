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
import logging
import socket
from collections.abc import Mapping

from .backends.openai import UpstreamAddress
from .errors import BadGatewayError, UpstreamTimeout

log = logging.getLogger(__name__)

CHUNK_BYTES = 65536
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
