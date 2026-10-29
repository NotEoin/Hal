"""A scripted MCP server on a unix socket, standing in for the Hal broker in tests.

It speaks MCP with stdio framing over the socket (one JSON-RPC message per line), the same
transport the real broker uses. It answers `initialize`, `ping`, `tools/list` and `tools/call`
from tools that the test registers, and records every call.
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType

from halkit import paths

POLL_INTERVAL_S = 0.02  # the default 0.5 s makes every shutdown wait that long
MAX_MESSAGE_BYTES = 4 * 2**20  # resolution E9: the broker closes a connection on a larger message
DEFAULT_PROTOCOL = "2025-06-18"  # used when the client names none
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_PARSE_ERROR = -32700

Result = str | Mapping[str, object] | Exception | Callable[[Mapping[str, object]], object]


class _Handler(socketserver.StreamRequestHandler):
    server: _Server

    def handle(self) -> None:
        while True:
            line = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
            if not line or len(line) > MAX_MESSAGE_BYTES:
                return
            reply = self.server.broker.dispatch(line)
            if reply is not None:
                self.wfile.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
                self.wfile.flush()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    broker: FakeBroker


class FakeBroker:
    """Scripted MCP tool results over a unix socket.

    Use as a context manager. `socket_path` is where a client connects; `calls` lists every
    `tools/call` as `(name, arguments)`.
    """

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = paths.check_socket_path(Path(socket_path))
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._tools: dict[str, tuple[dict[str, object], Result]] = {}
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def add_tool(
        self,
        name: str,
        result: Result = "",
        *,
        description: str = "",
        input_schema: Mapping[str, object] | None = None,
    ) -> None:
        """Registers a tool.

        `result` is a string (returned as text), a mapping (returned as the full MCP result, so
        a test can set `isError` or `_meta`), an exception (returned as an error result whose
        text is its message) or a callable taking the arguments and returning one of those.
        """
        schema = dict(input_schema or {"type": "object", "properties": {}})
        info = {"name": name, "description": description, "inputSchema": schema}
        self._tools[name] = (info, result)

    def dispatch(self, line: bytes) -> dict[str, object] | None:
        """Handles one JSON-RPC line and returns the reply, or None for a notification."""
        try:
            message = json.loads(line)
        except ValueError:
            return self._error(None, JSONRPC_PARSE_ERROR, "parse error")
        if not isinstance(message, dict) or "method" not in message:
            return None
        ident = message.get("id")
        method = message["method"]
        params = message.get("params") or {}
        if ident is None:  # a notification such as notifications/initialized
            return None
        if method == "initialize":
            version = params.get("protocolVersion") or DEFAULT_PROTOCOL
            return self._ok(
                ident,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "fake-broker", "version": "0"},
                },
            )
        if method == "ping":
            return self._ok(ident, {})
        if method == "tools/list":
            return self._ok(ident, {"tools": [info for info, _ in self._tools.values()]})
        if method == "tools/call":
            return self._call(ident, params)
        return self._error(ident, JSONRPC_METHOD_NOT_FOUND, f"unknown method {method}")

    def _call(self, ident: object, params: Mapping[str, object]) -> dict[str, object]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in self._tools:
            return self._error(ident, JSONRPC_INVALID_PARAMS, f"unknown tool {name!r}")
        self.calls.append((name, dict(arguments)))  # type: ignore[arg-type]  # JSON object
        result = self._tools[name][1]
        if callable(result):
            result = result(arguments)  # type: ignore[assignment]
        if isinstance(result, Exception):
            return self._ok(ident, {"content": [{"type": "text", "text": str(result)}], "isError": True})
        if isinstance(result, Mapping):
            return self._ok(ident, dict(result))
        return self._ok(ident, {"content": [{"type": "text", "text": str(result)}], "isError": False})

    @staticmethod
    def _ok(ident: object, result: object) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    @staticmethod
    def _error(ident: object, code: int, message: str) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}

    def start(self) -> FakeBroker:
        """Binds the socket (mode 0600) and serves in a background thread."""
        paths.ensure_private_dir(self.socket_path.parent)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = _Server(str(self.socket_path), _Handler)
        self._server.broker = self
        os.chmod(self.socket_path, 0o600)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": POLL_INTERVAL_S},
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stops serving and removes the socket file."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.socket_path.exists():
            self.socket_path.unlink()

    def __enter__(self) -> FakeBroker:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
