"""Fixtures for hal-sandbox tests: a short private socket directory and a fake model upstream."""

import http.server
import json
import shutil
import tempfile
import threading
from pathlib import Path

import pytest


@pytest.fixture
def sockdir():
    """A short private directory (unix socket paths are limited to 107 bytes)."""
    d = Path(tempfile.mkdtemp(prefix="hs-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class FakeUpstream(http.server.ThreadingHTTPServer):
    """Records every request path and answers with a small JSON body."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, bytes]] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.seen.append((self.command, self.path, self.rfile.read(n)))
                body = json.dumps({"ok": True, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = _go

        super().__init__(("127.0.0.1", 0), H)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


@pytest.fixture
def upstream():
    """A fake llama-server on localhost."""
    srv = FakeUpstream()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()
