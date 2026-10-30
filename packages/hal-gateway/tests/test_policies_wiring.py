"""Tests that the handler applies policies to router backends and logs their decisions (REQ-GW-49)."""

import http.client
import http.server
import json
import socket
import threading
import time

import pytest

from halgateway.config import load_config
from halgateway.server import Gateway

SEEN = []
USER = {"role": "user", "content": "fix the bug"}


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        SEEN.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        out = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def gateway(tmp_path):
    SEEN.clear()
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
        '[gateway.aliases.thinker]\nbackend = "local"\nmodel = "m"\neffort = true\n'
        '[gateway.aliases.plain]\nbackend = "local"\nmodel = "m"\n'
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
    return conn.getresponse().status


def test_effort_alias_gets_the_note_and_the_decision_is_logged(gateway):
    body = {"model": "thinker", "tools": [{"name": "Bash"}], "messages": [USER]}
    assert post(gateway, "/v1/chat/completions", body) == 200
    assert SEEN[0]["messages"][-1]["role"] == "system" and "xhigh" in SEEN[0]["messages"][-1]["content"]
    assert SEEN[0]["model"] == "m"
    deadline = time.monotonic() + 5
    log = gateway.state_dir / "requests.jsonl"
    while time.monotonic() < deadline and not (log.exists() and log.read_text().strip()):
        time.sleep(0.05)
    assert json.loads(log.read_text().splitlines()[-1])["policy"] == {"effort": "xhigh (user message)"}


def test_alias_without_the_policy_is_relayed_unchanged(gateway):
    body = {"model": "plain", "tools": [{"name": "Bash"}], "messages": [USER]}
    assert post(gateway, "/v1/messages", body) == 200
    assert SEEN[0]["messages"] == [USER]
