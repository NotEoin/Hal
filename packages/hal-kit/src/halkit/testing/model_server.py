"""A scripted model server for tests: OpenAI Chat Completions, Responses, Anthropic Messages
and Embeddings, over loopback TCP or a unix socket.

Tests queue `Reply` objects; each model request takes the next one, whatever the API. A
request with nothing queued gets HTTP 500 and is recorded, so a test never hangs and never
passes by accident. Streaming (`"stream": true`) is supported for Chat Completions only.
"""

from __future__ import annotations

import hashlib
import json
import socket
import socketserver
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import TracebackType

from halkit import paths

EMBEDDING_DIM = 8  # small and deterministic; real models are far larger
SERVER_THREAD_JOIN_S = 5.0
POLL_INTERVAL_S = 0.02  # the default 0.5 s makes every shutdown wait that long
MODEL_ID = "fake-model"


@dataclass
class Reply:
    """One scripted answer.

    `tool_calls` is a list of `(name, arguments_dict)`. `status` other than 200 returns an error
    body. `delay` seconds are slept before answering, to test client timeouts.
    """

    text: str = ""
    tool_calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    finish_reason: str | None = None
    status: int = 200
    delay: float = 0.0
    prompt_tokens: int = 10
    completion_tokens: int = 5
    logprobs: list[dict[str, object]] | None = None  # choices[0].logprobs.content, when set


def fake_embedding(text: str) -> list[float]:
    """A deterministic vector for `text`: equal texts give equal vectors, others differ."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [round(b / 255.0, 6) for b in digest[:EMBEDDING_DIM]]


class _Handler(BaseHTTPRequestHandler):
    server: _HTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return  # tests read `FakeModelServer.requests` instead of stderr noise

    def _send(self, status: int, body: object, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        owner = self.server.owner
        owner._record(self, None)
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        elif self.path == "/v1/models":
            data = [
                {"id": m, "object": "model", "context_length": owner.context_length} for m in owner.models
            ]
            self._send(200, {"object": "list", "data": data})
        else:
            self._send(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self) -> None:
        owner = self.server.owner
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = None
        owner._record(self, body)
        if body is None:
            self._send(400, {"error": {"message": "request body is not JSON"}})
            return
        route = self.path.split("?")[0]
        if route == "/v1/embeddings":
            self._embeddings(body)
        elif route in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
            self._model(route, body)
        elif route in ("/v1/responses/input_tokens", "/v1/messages/count_tokens"):
            self._send(200, {"input_tokens": owner.count_tokens(body)})
        else:
            self._send(404, {"error": {"message": f"no route {route}"}})

    def _embeddings(self, body: dict) -> None:
        inputs = body.get("input", [])
        texts = [inputs] if isinstance(inputs, str) else list(inputs)
        data = [
            {"object": "embedding", "index": i, "embedding": fake_embedding(t)} for i, t in enumerate(texts)
        ]
        self._send(200, {"object": "list", "data": data, "model": body.get("model", MODEL_ID)})

    def _model(self, route: str, body: dict) -> None:
        reply = self.server.owner._next_reply()
        if reply is None:
            self._send(500, {"error": {"message": "FakeModelServer: no scripted reply left"}})
            return
        if reply.delay:
            time.sleep(reply.delay)
        if reply.status != 200:
            self._send(reply.status, {"error": {"message": f"scripted error {reply.status}"}})
            return
        model = body.get("model", MODEL_ID)
        if route == "/v1/chat/completions":
            if body.get("stream"):
                self._stream_chat(reply, model)
            else:
                self._send(200, _chat_body(reply, model))
        elif route == "/v1/responses":
            self._send(200, _responses_body(reply, model))
        else:
            self._send(200, _messages_body(reply, model))

    def _stream_chat(self, reply: Reply, model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": word + " "}}]}
            for word in reply.text.split()
        ]
        final = _chat_body(reply, model)["choices"][0]
        chunks.append(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": final["message"].get("tool_calls", [])},
                        "finish_reason": final["finish_reason"],
                    }
                ]
            }
        )
        for chunk in chunks:
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


def _chat_body(reply: Reply, model: str) -> dict:
    message: dict[str, object] = {"role": "assistant", "content": reply.text}
    finish = reply.finish_reason or "stop"
    if reply.tool_calls:
        message["tool_calls"] = [
            {"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(reply.tool_calls)
        ]
        finish = reply.finish_reason or "tool_calls"
    usage = {
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "total_tokens": reply.prompt_tokens + reply.completion_tokens,
    }
    choice: dict[str, object] = {"index": 0, "message": message, "finish_reason": finish}
    if reply.logprobs is not None:
        choice["logprobs"] = {"content": reply.logprobs}
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": model,
        "choices": [choice],
        "usage": usage,
    }


def _responses_body(reply: Reply, model: str) -> dict:
    output: list[dict[str, object]] = []
    if reply.text:
        output.append(
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": reply.text}]}
        )
    for i, (name, args) in enumerate(reply.tool_calls):
        output.append(
            {"type": "function_call", "call_id": f"call_{i}", "name": name, "arguments": json.dumps(args)}
        )
    usage = {"input_tokens": reply.prompt_tokens, "output_tokens": reply.completion_tokens}
    return {
        "id": "resp_fake",
        "object": "response",
        "model": model,
        "status": "completed",
        "output": output,
        "usage": usage,
    }


def _messages_body(reply: Reply, model: str) -> dict:
    content: list[dict[str, object]] = []
    if reply.text:
        content.append({"type": "text", "text": reply.text})
    for i, (name, args) in enumerate(reply.tool_calls):
        content.append({"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": args})
    stop = reply.finish_reason or ("tool_use" if reply.tool_calls else "end_turn")
    usage = {"input_tokens": reply.prompt_tokens, "output_tokens": reply.completion_tokens}
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "usage": usage,
    }


class _HTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    owner: FakeModelServer


class _UnixHTTPServer(_HTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        sock, _ = super().get_request()
        return sock, ("unix", 0)  # the base handler indexes the client address


class FakeModelServer:
    """Scripted model endpoints. Use as a context manager.

    With `socket_path` it listens on that unix socket, otherwise on `127.0.0.1` at a free port.
    `requests` holds every request as a dict with `method`, `path`, `headers` and `body`.
    """

    def __init__(self, socket_path: Path | None = None, *, context_length: int = 8192) -> None:
        self.socket_path = paths.check_socket_path(Path(socket_path)) if socket_path else None
        self.context_length = context_length
        self.models = [MODEL_ID]
        self.requests: list[dict[str, object]] = []
        self._replies: deque[Reply] = deque()
        self._lock = threading.Lock()
        self._server: _HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def script(self, *replies: Reply | str) -> None:
        """Queues replies; a bare string is shorthand for `Reply(text=...)`."""
        with self._lock:
            self._replies.extend(r if isinstance(r, Reply) else Reply(text=r) for r in replies)

    def count_tokens(self, body: dict) -> int:
        """A crude token count (4 characters per token) for the count endpoints."""
        return max(1, len(json.dumps(body)) // 4)

    def _next_reply(self) -> Reply | None:
        with self._lock:
            return self._replies.popleft() if self._replies else None

    def _record(self, handler: BaseHTTPRequestHandler, body: object) -> None:
        entry = {
            "method": handler.command,
            "path": handler.path,
            "headers": dict(handler.headers),
            "body": body,
        }
        with self._lock:
            self.requests.append(entry)

    @property
    def url(self) -> str:
        """Base URL for TCP mode, for example `http://127.0.0.1:41234`."""
        if self._server is None or self.socket_path is not None:
            raise RuntimeError("url is only available for a started TCP server")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeModelServer:
        """Starts serving in a background thread."""
        if self.socket_path is not None:
            paths.ensure_private_dir(self.socket_path.parent)
            if self.socket_path.exists():
                self.socket_path.unlink()
            self._server = _UnixHTTPServer(str(self.socket_path), _Handler)
        else:
            self._server = _HTTPServer(("127.0.0.1", 0), _Handler)
        self._server.owner = self
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
            self._thread.join(timeout=SERVER_THREAD_JOIN_S)
        if self.socket_path is not None and self.socket_path.exists():
            self.socket_path.unlink()

    def __enter__(self) -> FakeModelServer:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
