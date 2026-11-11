"""A fake Ollama and OpenAI-compatible model-list server on loopback, for discovery tests.

It answers `GET /api/tags`, `POST /api/show`, `GET /v1/models` and `GET /models` from the
documents given to the constructor, records every request path, and checks nothing else.
`POST /api/create` with `from` adds a tag (digest: the sha256 of the new name) and keeps the body in
`created`, as a create from an existing model does in Ollama.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import TracebackType

SERVER_THREAD_JOIN_S = 5.0


class FakeOllama:
    """Serves `tags`, per-model `show` documents and an OpenAI-style `models` list."""

    def __init__(
        self,
        tags: list[dict] | None = None,  # type: ignore[type-arg]
        show: dict[str, dict] | None = None,  # type: ignore[type-arg]
        openai_models: list[dict] | None = None,  # type: ignore[type-arg]
    ) -> None:
        self.tags = tags or []
        self.show = show or {}
        self.openai_models = openai_models or []
        self.requests: list[tuple[str, str, dict[str, str]]] = []  # method, path, headers
        self.created: list[dict] = []  # type: ignore[type-arg]  # bodies of `/api/create`
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """`http://127.0.0.1:PORT`, once started."""
        assert self._server is not None, "start the server first"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeOllama:
        """Starts serving on a free loopback port."""
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature of the base class
                return

            def _reply(self, status: int, doc: object) -> None:
                raw = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802 - the name http.server looks up
                owner.requests.append(("GET", self.path, dict(self.headers)))
                route = self.path.split("?", 1)[0]
                if route == "/api/tags":
                    self._reply(200, {"models": owner.tags})
                elif route in ("/v1/models", "/models"):
                    self._reply(200, {"object": "list", "data": owner.openai_models})
                else:
                    self._reply(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802 - the name http.server looks up
                owner.requests.append(("POST", self.path, dict(self.headers)))
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                doc = owner.show.get(body.get("model") or body.get("name") or "")
                if self.path == "/api/create" and any(t.get("name") == body.get("from") for t in owner.tags):
                    owner.created.append(body)
                    owner.tags = [t for t in owner.tags if t.get("name") != body["model"]]
                    digest = hashlib.sha256(str(body["model"]).encode()).hexdigest()
                    owner.tags.append({"name": body["model"], "size": 1, "digest": digest})
                    self._reply(200, {"status": "success"})
                elif self.path == "/api/show" and doc is not None:
                    self._reply(200, doc)
                else:
                    self._reply(404, {"error": "model not found"})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stops serving."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(SERVER_THREAD_JOIN_S)

    def __enter__(self) -> FakeOllama:
        return self.start()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.stop()
