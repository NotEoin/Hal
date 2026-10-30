"""Integration tests for the route table, the relay and `OpenAIBackend`."""

import http.client
import http.server
import json
import socket
import threading

import pytest

from halgateway import relay, routes
from halgateway.backends import make_backend, parse_address
from halgateway.config import load_config
from halgateway.errors import BadGatewayError, UpstreamTimeout
from halgateway.routes import Api
from halgateway.server import Gateway

SSE_RELEASE = threading.Event()


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        Upstream.seen.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
        if self.path.startswith("/v1/chat/completions") and json.loads(raw).get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            SSE_RELEASE.wait(5)
            self.wfile.write(b"data: [DONE]\n\n")
            self.close_connection = True  # close-delimited body
            return
        out = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(out)
        self.close_connection = True


@pytest.fixture
def upstream():
    Upstream.seen = []
    SSE_RELEASE.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    SSE_RELEASE.set()
    server.shutdown()
    server.server_close()


@pytest.fixture
def gateway(tmp_path, upstream):
    def build(extra="", apis='["chat", "embeddings", "models"]', port=None):
        token = tmp_path / "token"
        token.write_text("hal-gw-t\n")
        token.chmod(0o600)
        cfg = tmp_path / "c.toml"
        cfg.write_text(
            f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n{extra}\n'
            f'[gateway.backends.up]\ntype = "openai"\nurl = "http://127.0.0.1:{port or upstream}"\n'
            f"apis = {apis}\n"
        )
        gw = Gateway(load_config(cfg))
        gw.start()
        built.append(gw)
        return gw

    built = []
    yield build
    for gw in built:
        gw.stop()


def call(gw, method, path, body=None, headers=None, raw=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    data = raw if raw is not None else (json.dumps(body) if body is not None else None)
    conn.request(method, path, body=data, headers=headers or {})
    resp = conn.getresponse()
    return resp, conn


def test_unknown_path_is_404(gateway):
    resp, _ = call(gateway(), "GET", "/nope")
    assert resp.status == 404 and "error" in json.loads(resp.read())


@pytest.mark.parametrize("path", ["/props", "/slots", "/metrics", "/completion", "/v1/internal"])
def test_llama_internal_routes_are_not_exposed(gateway, path):
    resp, _ = call(gateway(), "GET", path)
    assert resp.status == 404


def test_query_string_is_ignored_for_matching():
    assert routes.match("GET", "/v1/models?limit=3").api is Api.MODELS
    assert routes.match("GET", "/v1/models/abc").path == "/v1/models/{id}"
    assert routes.match("GET", "/v1/models/a/b").path == "/v1/models/{id}"
    assert routes.match("DELETE", "/hal/v1/sessions/a/b") is None
    assert routes.match("GET", "/v1/chat/completions") is None


def test_request_is_relayed_with_other_fields_and_query_kept(gateway, upstream):
    resp, _ = call(gateway(), "POST", "/v1/chat/completions?x=1", {"model": "up/m", "temperature": 0.2})
    assert resp.status == 200 and json.loads(resp.read()) == {"ok": True}
    seen = Upstream.seen[0]
    assert seen["path"] == "/v1/chat/completions?x=1" and seen["body"] == {"model": "m", "temperature": 0.2}


def test_client_credentials_never_reach_backend(gateway):
    headers = {
        "Authorization": "Bearer secret",
        "X-Api-Key": "secret",
        "Cookie": "a=b",
        "X-Hal-Session": "s",
        "Accept": "application/json",
    }
    call(gateway(), "POST", "/v1/chat/completions", {"model": "up/m"}, headers)
    sent = {k.lower() for k in Upstream.seen[0]["headers"]}
    assert not sent & {"authorization", "x-api-key", "cookie", "x-hal-session"}
    assert "accept" in sent


def test_backend_key_file_is_sent_to_the_backend(gateway, tmp_path):
    (tmp_path / "key").write_text("backend-key\n")
    gw = gateway()
    gw.backends["up"].entry = {**gw.backends["up"].entry, "api_key_file": str(tmp_path / "key")}
    call(gw, "POST", "/v1/chat/completions", {"model": "up/m"})
    assert Upstream.seen[0]["headers"]["Authorization"] == "Bearer backend-key"


def test_sse_events_arrive_before_stream_ends(gateway):
    resp, conn = call(gateway(), "POST", "/v1/chat/completions", {"model": "up/m", "stream": True})
    assert resp.status == 200
    assert resp.fp.readline() == b"data: first\n"  # the upstream is still holding the stream open
    SSE_RELEASE.set()
    assert b"[DONE]" in resp.read()
    conn.close()


def test_unsupported_api_names_backend_apis(gateway):
    resp, _ = call(gateway(), "POST", "/v1/messages", {"model": "up/m"})
    body = json.loads(resp.read())
    assert resp.status == 404
    assert "backend up" in body["error"]["message"] and "chat" in body["error"]["message"]


def test_anthropic_route_errors_use_the_anthropic_shape(gateway):
    resp, _ = call(gateway(), "POST", "/v1/messages", {"model": "up/m"})
    assert json.loads(resp.read())["type"] == "error"


def test_chunked_request_body_is_rejected(gateway):
    resp, _ = call(
        gateway(), "POST", "/v1/chat/completions", headers={"Transfer-Encoding": "chunked"}, raw="0\r\n\r\n"
    )
    assert resp.status == 400


def test_post_without_content_length_is_411(gateway):
    gw = gateway()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n\r\n")
    assert sock.recv(100).startswith(b"HTTP/1.1 411")


def test_oversized_body_is_413_without_reading(gateway):
    gw = gateway("max_request_bytes = 100")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: 999999\r\n\r\n")
    assert sock.recv(100).startswith(b"HTTP/1.1 413")  # no body was sent, so none was read


def test_request_without_model_and_no_default_is_404(gateway):
    resp, _ = call(gateway(), "POST", "/v1/chat/completions", {"messages": []})
    assert resp.status == 404


def test_default_model_is_used_when_the_request_names_none(gateway):
    call(gateway('default_model = "up/dflt"'), "POST", "/v1/chat/completions", {"messages": []})
    assert Upstream.seen[0]["body"]["model"] == "dflt"


def test_dead_backend_is_502(gateway):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    resp, _ = call(gateway(port=port), "POST", "/v1/chat/completions", {"model": "up/m"})
    assert resp.status == 502


def test_upstream_connect_timeout_is_504(monkeypatch):
    def refuse_in_time(self):
        raise TimeoutError

    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse_in_time)
    with pytest.raises(UpstreamTimeout):
        relay.connect(parse_address("http://127.0.0.1:1"), 0.1, 1.0)


def test_connect_failure_is_a_bad_gateway(monkeypatch):
    def fail(self):
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", fail)
    with pytest.raises(BadGatewayError):
        relay.connect(parse_address("http://127.0.0.1:1"), 0.1, 1.0)


def test_parse_address_handles_unix_https_and_garbage():
    assert parse_address("unix:/run/x.sock").path == "/run/x.sock"
    assert (parse_address("https://example.invalid").port) == 443
    with pytest.raises(ValueError):
        parse_address("ftp://x")


def test_backend_defaults_follow_the_type_table(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[gateway.backends.o]\ntype = "ollama"\n'
        '[gateway.backends.r]\ntype = "llama-router"\nmodels_dir = "/m"\n'
        '[gateway.backends.x]\ntype = "openai"\napis = ["messages"]\n'
    )
    backends = {n: make_backend(n, e, "http://127.0.0.1:11434") for n, e in load_config(cfg).backends.items()}
    assert backends["o"].supports(Api.OLLAMA_NATIVE) and not backends["o"].supports(Api.RESPONSES_COUNT)
    assert backends["r"].supports(Api.RESPONSES_COUNT) and not backends["r"].supports(Api.OLLAMA_NATIVE)
    assert backends["x"].supports(Api.COUNT_TOKENS) and not backends["x"].supports(Api.CHAT)
    assert backends["o"].address().port == 11434 and backends["r"].address().port == 8080


def test_chat_options_are_stripped_for_a_backend_that_is_not_ollama(gateway):
    body = {"model": "up/m", "options": {"num_gpu": 0}, "temperature": 0.1}
    resp, _ = call(gateway(), "POST", "/v1/chat/completions", body)
    assert resp.status == 200 and Upstream.seen[0]["body"] == {"model": "m", "temperature": 0.1}
