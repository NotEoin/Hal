"""Tests for `halgateway.policies.overflow` and its wiring in the relay (REQ-GW-40)."""

import http.client
import http.server
import json
import socket
import threading

import pytest

from halgateway.config import load_config
from halgateway.policies.overflow import map_overflow
from halgateway.server import Gateway

OVERFLOW = {"error": {"type": "exceed_context_size_error", "n_prompt_tokens": 120000, "n_ctx": 114688}}


def test_overflow_maps_to_claude_code_message():
    out = json.loads(map_overflow("anthropic", 400, json.dumps(OVERFLOW).encode()))
    assert out == {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "prompt is too long: 120000 tokens > 114688 maximum",
        },
    }


def test_overflow_maps_to_openai_code():
    err = json.loads(map_overflow("openai", 400, json.dumps(OVERFLOW).encode()))["error"]
    assert (
        err["code"] == "context_length_exceeded" and "114688" in err["message"] and "120000" in err["message"]
    )


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, b'{"error": {"type": "invalid_request_error", "message": "bad"}}'),
        (400, b"not json"),
        (400, b'{"error": {"type": "exceed_context_size_error"}}'),
        (400, b'{"error": "text"}'),
        (500, json.dumps(OVERFLOW).encode()),
    ],
)
def test_other_400_passes_unchanged(status, body):
    assert map_overflow("anthropic", status, body) is None


def test_oversized_body_is_not_mapped():
    assert map_overflow("openai", 400, b" " * (1024 * 1024 + 1)) is None


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    reply = b""

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(Upstream.reply)))
        self.end_headers()
        self.wfile.write(Upstream.reply)


@pytest.fixture
def gateway(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = tmp_path / "token"
    token.write_text("t\n")
    token.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n'
        f'[gateway.backends.local]\ntype = "llama-router"\nmanaged = false\n'
        f'url = "http://127.0.0.1:{server.server_address[1]}"\n'
    )
    gw = Gateway(load_config(cfg))
    gw.start()
    yield gw
    gw.stop()
    server.shutdown()
    server.server_close()


def post(gw, path, doc):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    conn.request("POST", path, body=json.dumps(doc))
    resp = conn.getresponse()
    return resp.status, resp.getheader("Content-Length"), resp.read()


def test_relay_maps_overflow_on_the_messages_route_and_fixes_content_length(gateway):
    Upstream.reply = json.dumps(OVERFLOW).encode()
    status, length, body = post(gateway, "/v1/messages", {"model": "local/m", "messages": []})
    assert status == 400 and int(length) == len(body)
    assert "prompt is too long: 120000 tokens > 114688 maximum" in body.decode()


def test_relay_passes_an_unrelated_400_unchanged(gateway):
    Upstream.reply = b'{"error": {"message": "bad"}}'
    status, _, body = post(gateway, "/v1/chat/completions", {"model": "local/m", "messages": []})
    assert status == 400 and body == Upstream.reply
