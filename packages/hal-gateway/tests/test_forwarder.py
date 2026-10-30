"""Tests for the session model-socket forwarder (REQ-GW-63 to 65, 69; REQ-SEC-66)."""

import http.client
import http.server
import json
import socket
import threading
import time

import pytest

from halgateway.forwarder import Budget, ForwarderLimits, SessionForwarder

SEEN = []


class FakeGateway(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        SEEN.append({"path": self.path, "headers": dict(self.headers), "body": raw})
        if self.headers.get("Authorization") == "Bearer stale":
            out = json.dumps({"error": {"code": "session_unknown"}}).encode()
            self.send_response(401)
        elif b'"stream": true' in raw or b'"stream":true' in raw:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for i in range(3):
                self.wfile.write(f'data: {{"n": {i}}}\n\n'.encode())
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b'data: {"usage": {"prompt_tokens": 7, "completion_tokens": 5}}\n\n')
            self.close_connection = True
            return
        else:
            out = json.dumps({"ok": True, "usage": {"prompt_tokens": 10, "completion_tokens": 20}}).encode()
            self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _answer


@pytest.fixture
def gateway():
    SEEN.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
    threading.Thread(target=lambda: server.serve_forever(0.05), daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def make(tmp_path, gateway):
    made, events = [], []

    def build(**kw):
        kw.setdefault("gateway_url", gateway)
        kw.setdefault("token", "hal-sess-aaaa.secret")
        fwd = SessionForwarder(tmp_path / f"m{len(made)}.sock", on_event=events.append, **kw)
        fwd.start()
        made.append(fwd)
        return fwd

    build.events = events
    yield build
    for fwd in made:
        fwd.close()


def request(fwd, method, path, doc=None, headers=None, raw=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(str(fwd.listen_path))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    body = raw if raw is not None else (json.dumps(doc) if doc is not None else None)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    return resp, resp.read()


CHAT = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def test_forwarder_injects_token_and_strips_auth(make):
    fwd = make()
    resp, _ = request(
        fwd, "POST", "/v1/chat/completions", CHAT,
        {"Authorization": "Bearer evil", "X-Api-Key": "k", "Cookie": "c", "X-Hal-Admin": "1", "X-Other": "x"},
    )  # fmt: skip
    assert resp.status == 200
    seen = {k.lower(): v for k, v in SEEN[0]["headers"].items()}
    assert seen["authorization"] == "Bearer hal-sess-aaaa.secret"
    assert not {"x-api-key", "cookie", "x-hal-admin"} & seen.keys() and seen["x-other"] == "x"


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/hal/v1/status"), ("POST", "/hal/v1/sessions"), ("GET", "/api/tags"), ("POST", "/api/chat"),
     ("POST", "/tokenize"), ("POST", "/models/load"), ("GET", "/slots"), ("GET", "/props"),
     ("GET", "/v1/models/x"), ("GET", "/health"), ("DELETE", "/v1/chat/completions")],
)  # fmt: skip
def test_forwarder_rejects_admin_path_with_403(make, method, path):
    fwd = make()
    resp, _ = request(fwd, method, path, {} if method == "POST" else None)
    assert resp.status == 403 and SEEN == []  # the gateway was never contacted


def test_forwarder_matches_path_without_query(make):
    fwd = make()
    resp, _ = request(fwd, "POST", "/v1/messages?beta=true", {**CHAT, "max_tokens": 5})
    assert resp.status == 200 and SEEN[0]["path"] == "/v1/messages?beta=true"
    assert request(fwd, "GET", "/hal/v1/status?x=/v1/models")[0].status == 403


def test_forwarder_streams_sse(make):
    fwd = make()
    resp, body = request(fwd, "POST", "/v1/chat/completions", {**CHAT, "stream": True})
    assert resp.status == 200 and body.count(b"data:") == 4
    assert resp.getheader("Content-Type") == "text/event-stream"


def test_forwarder_reports_rejected_token(make):
    fwd = make(token="stale")
    resp, _ = request(fwd, "POST", "/v1/chat/completions", CHAT)
    assert resp.status == 401 and [e.kind for e in make.events] == ["token_rejected"]
    fwd.set_token("fresh")
    assert request(fwd, "POST", "/v1/chat/completions", CHAT)[0].status == 200
    assert SEEN[-1]["headers"]["Authorization"] == "Bearer fresh"


def test_forwarder_relays_to_a_unix_gateway(tmp_path):
    import socketserver

    class UnixGateway(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    path = tmp_path / "gw.sock"
    server = UnixGateway(str(path), FakeGateway)
    server.server_name, server.server_port = "localhost", 0
    threading.Thread(target=lambda: server.serve_forever(0.05), daemon=True).start()
    fwd = SessionForwarder(tmp_path / "m.sock", path, "tok")
    fwd.start()
    try:
        SEEN.clear()
        assert request(fwd, "POST", "/v1/chat/completions", CHAT)[0].status == 200
        assert oct(fwd.listen_path.stat().st_mode & 0o777) == "0o600"
    finally:
        fwd.close()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "url", ["http://example.com:80", "https://127.0.0.1:1", "http://10.0.0.1:1", "ftp://x"]
)
def test_forwarder_refuses_non_loopback_url(tmp_path, url):
    with pytest.raises(ValueError, match="loopback"):
        SessionForwarder(tmp_path / "m.sock", gateway_url=url, token="t")


def test_forwarder_needs_exactly_one_gateway_and_a_token(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        SessionForwarder(tmp_path / "m.sock", token="t")
    with pytest.raises(ValueError, match="token"):
        SessionForwarder(tmp_path / "m.sock", gateway_url="http://127.0.0.1:1", token="")


def test_forwarder_slowloris_times_out(make):
    fwd = make(limits=ForwarderLimits(read_timeout_s=0.3))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(fwd.listen_path))
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: 50\r\n\r\n{")
    start = time.monotonic()
    assert sock.recv(1) == b""  # the forwarder closes the connection
    assert time.monotonic() - start < 3 and SEEN == []
    sock.close()


def test_forwarder_clamps_max_tokens(make):
    fwd = make(limits=ForwarderLimits(max_output_tokens=100))
    for sent, expected in (({}, 100), ({"max_tokens": 5000}, 100), ({"max_tokens": 7}, 7)):
        SEEN.clear()
        assert request(fwd, "POST", "/v1/chat/completions", {**CHAT, **sent})[0].status == 200
        body = json.loads(SEEN[0]["body"])
        assert body["max_tokens"] == expected
        assert SEEN[0]["headers"]["Content-Length"] == str(len(SEEN[0]["body"]))
    SEEN.clear()
    request(fwd, "POST", "/v1/responses", {"model": "m", "input": "x", "max_output_tokens": 10**6})
    assert json.loads(SEEN[0]["body"])["max_output_tokens"] == 100
    SEEN.clear()
    request(fwd, "POST", "/v1/embeddings", {"model": "m", "input": "x"})
    assert "max_tokens" not in json.loads(SEEN[0]["body"])


def test_forwarder_rejects_a_non_json_body_without_contacting_the_gateway(make):
    fwd = make()
    assert request(fwd, "POST", "/v1/chat/completions", raw="not json")[0].status == 400 and SEEN == []


def test_forwarder_request_rate_is_429_with_retry_after(make):
    fwd = make(limits=ForwarderLimits(max_requests_per_min=2))
    for _ in range(2):
        assert request(fwd, "POST", "/v1/chat/completions", CHAT)[0].status == 200
    resp, body = request(fwd, "POST", "/v1/chat/completions", CHAT)
    assert resp.status == 429 and 1 <= int(resp.getheader("Retry-After")) <= 61
    assert json.loads(body)["error"]["code"] == "budget_exceeded" and len(SEEN) == 2
    assert make.events[-1].kind == "rejected" and make.events[-1].detail["limit"] == "max_requests_per_min"


def test_session_token_budget_is_enforced_host_side(make):
    fwd = make(limits=ForwarderLimits(max_tokens_per_hour=500, max_output_tokens=100))
    first = request(fwd, "POST", "/v1/chat/completions", CHAT)[0]  # estimate ~125, actual 30
    assert first.status == 200
    big = {**CHAT, "messages": [{"role": "user", "content": "x" * 1600}]}  # estimate 100 + 400
    resp, _ = request(fwd, "POST", "/v1/chat/completions", big)
    assert resp.status == 429 and int(resp.getheader("Retry-After")) > 1
    assert make.events[-1].detail["limit"] == "max_tokens_per_hour"


def test_forwarder_counts_usage_from_the_final_sse_event(make):
    fwd = make(limits=ForwarderLimits(max_tokens_per_hour=10_000, max_output_tokens=100))
    request(fwd, "POST", "/v1/chat/completions", {**CHAT, "stream": True})
    deadline = time.monotonic() + 3
    while not fwd.budget._tokens and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [t for _, t in fwd.budget._tokens] == [12]


def test_forwarder_counts_estimate_when_usage_missing(make):
    fwd = make(limits=ForwarderLimits(max_output_tokens=100))
    request(fwd, "GET", "/v1/models")  # no body, no usage in the fake's answer: tokens recorded = 0 estimate
    SEEN.clear()
    fwd2 = make(limits=ForwarderLimits(max_output_tokens=100))
    # the fake answers 401-free JSON with usage, so test the estimate through a gateway that is down
    fwd2.address = fwd2._address(None, "http://127.0.0.1:9")
    assert request(fwd2, "POST", "/v1/chat/completions", CHAT)[0].status == 502
    assert fwd2.budget._tokens[0][1] >= 100 and make.events[-1].kind == "gateway_down"


def test_budget_retry_after_is_the_time_until_room_frees():
    now = [0.0]
    budget = Budget(ForwarderLimits(max_tokens_per_hour=100, max_requests_per_min=99), lambda: now[0])
    assert budget.admit(60) is None
    budget.record(60)
    now[0] = 1000.0
    assert budget.admit(60) == ("max_tokens_per_hour", 2601)
    now[0] = 3601.0
    assert budget.admit(60) is None


def test_concurrency_cap_is_429(make):
    fwd = make(limits=ForwarderLimits(max_concurrent=1))
    assert fwd._slots.acquire(blocking=False)
    try:
        assert request(fwd, "POST", "/v1/chat/completions", CHAT)[0].status == 429
    finally:
        fwd._slots.release()
    assert request(fwd, "POST", "/v1/chat/completions", CHAT)[0].status == 200


def test_oversize_body_is_413(make):
    fwd = make(limits=ForwarderLimits(max_request_bytes=50))
    assert request(fwd, "POST", "/v1/chat/completions", {**CHAT, "pad": "x" * 100})[0].status == 413
