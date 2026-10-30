"""Tests for `halgateway.reqlog` and the records the handler writes."""

import http.client
import http.server
import json
import logging
import socket
import threading
import time

import pytest

from halgateway import reqlog
from halgateway.config import load_config
from halgateway.server import Gateway
from halkit import paths

REPLY = json.dumps({"id": "x", "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
SECRET_PROMPT = "ultra-secret-prompt"


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        out = json.dumps({"data": [{"id": "m"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(REPLY)))
        self.end_headers()
        self.wfile.write(REPLY)


@pytest.fixture
def gateway(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    built = []

    def build(log_table=""):
        token = tmp_path / "token"
        token.write_text("hal-gw-t\n")
        token.chmod(0o600)
        cfg = tmp_path / "c.toml"
        cfg.write_text(
            f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n'
            f"{log_table}\n"
            f'[gateway.backends.up]\ntype = "openai"\nurl = "http://127.0.0.1:{server.server_address[1]}"\n'
            'apis = ["chat", "models"]\n'
        )
        gw = Gateway(load_config(cfg))
        gw.start()
        built.append(gw)
        return gw

    yield build
    for gw in built:
        gw.stop()
    server.shutdown()


def post(gw, headers=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": SECRET_PROMPT}]})
    conn.request("POST", "/v1/chat/completions", body=body, headers=headers or {})
    resp = conn.getresponse()
    resp.read()
    return resp


def log_path():
    """Waits for the record, which is written just after the reply is sent."""
    path = paths.state_dir() / "gateway" / reqlog.LOG_NAME
    deadline = time.monotonic() + 3
    while not (path.exists() and path.read_text().endswith("\n")) and time.monotonic() < deadline:
        time.sleep(0.01)
    return path


def records():
    path = log_path()
    return [json.loads(line) for line in path.read_text().splitlines()]


def settings(**over):
    return {"requests": True, "content": False, "max_bytes": 10 * 1024 * 1024, "keep": 3, **over}


def test_request_record_has_no_content(gateway):
    gw = gateway("[gateway.log]\nrequests = true\n")
    post(gw, {"Authorization": "Bearer secret-token-value"})
    raw = log_path().read_text()
    assert SECRET_PROMPT not in raw and "secret-token-value" not in raw
    rec = records()[-1]
    assert (
        rec["status"] == 200
        and rec["backend"] == "up"
        and rec["usage"]
        == {
            "input_tokens": 7,
            "output_tokens": 3,
        }
    )
    assert rec["bytes_in"] > 0 and rec["bytes_out"] == len(REPLY) and rec["error"] is None
    assert not (paths.state_dir() / "gateway" / reqlog.CONTENT_DIR).exists()


def test_content_log_is_opt_in(gateway):
    gw = gateway("[gateway.log]\ncontent = true\n")
    resp = post(gw)
    rid = resp.getheader("X-Request-Id")
    log_path()
    stored = paths.state_dir() / "gateway" / reqlog.CONTENT_DIR / f"{rid}.json"
    deadline = time.monotonic() + 3
    while not stored.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert SECRET_PROMPT in json.loads(stored.read_text())["request"]
    assert stored.stat().st_mode & 0o777 == 0o600


def test_client_request_id_is_echoed_when_well_formed(gateway):
    gw = gateway()
    assert post(gw, {"X-Request-Id": "abc.123"}).getheader("X-Request-Id") == "abc.123"
    bad = post(gw, {"X-Request-Id": "bad id!"}).getheader("X-Request-Id")
    assert bad.startswith("r-") and len(bad) == 10


def test_error_response_is_logged_with_its_code(gateway):
    gw = gateway()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    conn.request("GET", "/nope")
    conn.getresponse().read()
    rec = records()[-1]
    assert rec["status"] == 404 and rec["error"]


def test_usage_takes_the_last_value_of_each_key():
    tail = b'data: {"usage":{"prompt_tokens":1}}\ndata: {"usage":{"prompt_tokens":5,"completion_tokens":9}}'
    assert reqlog.scan_usage(tail) == {"input_tokens": 5, "output_tokens": 9}
    assert reqlog.scan_usage(b"no usage here") is None


def test_ollama_counts_map_to_input_and_output():
    assert reqlog.scan_usage(b'{"prompt_eval_count": 4, "eval_count": 2}') == {
        "input_tokens": 4,
        "output_tokens": 2,
    }


def test_request_log_rotates(tmp_path):
    rl = reqlog.RequestLog(tmp_path, settings(max_bytes=300, keep=2))
    for n in range(40):
        rl.write({"n": n, "pad": "x" * 40})
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["requests.jsonl", "requests.jsonl.1", "requests.jsonl.2"]
    assert all(p.stat().st_size <= 300 for p in tmp_path.iterdir())
    assert (tmp_path / reqlog.LOG_NAME).stat().st_mode & 0o777 == 0o600


def test_log_failure_does_not_fail_request_and_reports_once(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    rl = reqlog.RequestLog(blocker / "sub", settings())
    with caplog.at_level(logging.ERROR, logger="halgateway.reqlog"):
        for _ in range(5):
            rl.write({"a": 1})
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_disabled_log_writes_nothing(tmp_path):
    rl = reqlog.RequestLog(tmp_path / "d", settings(requests=False))
    rl.write({"a": 1})
    assert not (tmp_path / "d").exists()
