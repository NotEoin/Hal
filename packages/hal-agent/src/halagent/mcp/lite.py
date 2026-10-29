"""Stdlib MCP client for the hub socket: newline-delimited JSON-RPC 2.0 over a unix socket.

It declares empty client capabilities, so the hub can never ask this process to approve
anything. It does not import `halhub` (resolution A2). Every read has a timeout.
"""

from __future__ import annotations

import json
import logging
import socket
from pathlib import Path
from typing import Any

from halkit import paths

log = logging.getLogger("halagent.mcp")

PROTOCOL_VERSION = "2025-11-25"
MAX_MESSAGE_BYTES = 4 * 2**20  # the hub closes a connection on a larger message
MAX_PAGES = 50  # a runaway nextCursor loop must end


class McpError(Exception):
    """The hub answered with a JSON-RPC error, or the connection failed."""


class LiteMcpClient:
    """A minimal MCP client: initialize, list_tools, call_tool."""

    def __init__(self, socket_path: str, *, client_name: str = "halagent", timeout_s: float = 30.0) -> None:
        path = socket_path[len("unix://") :] if socket_path.startswith("unix://") else socket_path
        self._path = str(paths.check_socket_path(Path(path)))
        self._name = client_name
        self._timeout = timeout_s
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 0

    def _ensure(self) -> socket.socket:
        if self._sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)
            try:
                sock.connect(self._path)
            except OSError as exc:
                sock.close()
                raise McpError(f"cannot connect to hub socket {self._path}: {exc}") from exc
            self._sock = sock
        return self._sock

    def _send(self, msg: dict) -> None:
        try:
            self._ensure().sendall(json.dumps(msg).encode() + b"\n")
        except OSError as exc:
            raise McpError(f"hub send failed: {exc}") from exc

    def _read_line(self) -> bytes:
        sock = self._ensure()
        while b"\n" not in self._buf:
            if len(self._buf) > MAX_MESSAGE_BYTES:
                raise McpError("hub message too large")
            try:
                chunk = sock.recv(65536)
            except OSError as exc:  # includes timeout
                raise McpError(f"hub read failed: {exc}") from exc
            if not chunk:
                raise McpError("hub closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def _request(self, method: str, params: dict | None = None) -> Any:
        self._next_id += 1
        ident = self._next_id
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        while True:
            msg = json.loads(self._read_line() or b"{}")
            if msg.get("id") != ident:
                continue  # a notification or stray message
            if "error" in msg:
                raise McpError(f"{method}: {msg['error'].get('message', msg['error'])}")
            return msg.get("result", {})

    def initialize(self) -> dict:
        """Performs the handshake and returns the server's result."""
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self._name, "version": "0.1.0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def list_tools(self) -> list[dict]:
        """All tools the hub offers, following `nextCursor`."""
        tools: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools += result.get("tools", [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise McpError("tools/list did not end")

    def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Calls a tool; returns `(text, is_error)`."""
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return text, bool(result.get("isError"))

    def close(self) -> None:
        """Closes the connection."""
        if self._sock is not None:
            self._sock.close()
            self._sock = None
