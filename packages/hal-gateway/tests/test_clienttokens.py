"""Tests for named client tokens, client scope, owner token rotation and client mode (REQ-GW-66 to 68)."""

import http.client
import http.server
import json
import os
import socket
import threading
import time

import pytest

from halgateway import cli
from halgateway.clienttokens import ClientTokens, TokenError
from halgateway.config import load_config
from halgateway.server import Gateway, StartError


def test_client_token_lifecycle(tmp_path):
    tokens = ClientTokens(tmp_path / "tokens.json")
    secret = tokens.issue("laptop", allow_remote=True, cls="service")
    assert secret.startswith("hal-gwc-laptop.")
    found = tokens.check(secret)
    assert (found.name, found.allow_remote, found.cls) == ("laptop", True, "service")
    assert tokens.check("hal-gwc-laptop.wrong") is None and tokens.check("hal-gwc-other.x") is None
    assert oct(os.stat(tmp_path / "tokens.json").st_mode & 0o777) == "0o600"


def test_only_the_client_token_hash_is_stored(tmp_path):
    tokens = ClientTokens(tmp_path / "tokens.json")
    secret = tokens.issue("laptop")
    assert secret.split(".", 1)[1] not in (tmp_path / "tokens.json").read_text()


def test_issuing_an_existing_name_needs_rotate(tmp_path):
    tokens = ClientTokens(tmp_path / "tokens.json")
    old = tokens.issue("laptop")
    with pytest.raises(TokenError, match="--rotate"):
        tokens.issue("laptop")
    new = tokens.issue("laptop", rotate=True)
    assert tokens.check(old) is None and tokens.check(new) is not None


def test_bad_names_and_classes_are_refused(tmp_path):
    tokens = ClientTokens(tmp_path / "tokens.json")
    for name in ("", "Laptop", "-x", "a" * 33, "a.b"):
        with pytest.raises(TokenError):
            tokens.issue(name)
    with pytest.raises(TokenError, match="class"):
        tokens.issue("ok", cls="batch")


def test_revoked_client_token_fails_without_restart(tmp_path):
    reader = ClientTokens(tmp_path / "tokens.json")  # the gateway's instance
    secret = ClientTokens(tmp_path / "tokens.json").issue("laptop")  # the CLI's instance
    assert reader.check(secret) is not None
    assert ClientTokens(tmp_path / "tokens.json").revoke("laptop")
    assert reader.check(secret) is None
    assert not reader.revoke("laptop")


def test_token_list_has_no_secrets(tmp_path):
    tokens = ClientTokens(tmp_path / "tokens.json")
    secret = tokens.issue("laptop")
    listing = json.dumps([t.public() for t in tokens.list()])
    assert "laptop" in listing and secret.split(".", 1)[1] not in listing and "hash" not in listing


def test_a_damaged_token_file_fails_closed(tmp_path):
    (tmp_path / "tokens.json").write_text("{not json")
    assert ClientTokens(tmp_path / "tokens.json").check("hal-gwc-a.b") is None


# --- gateway and CLI ------------------------------------------------------------------------


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        out = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def gw(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=lambda: server.serve_forever(0.05), daemon=True).start()
    token = tmp_path / "token"
    token.write_text("hal-gw-owner\n")
    token.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n'
        '[gateway.gpu]\ncoordinator = "none"\n'
        f'[gateway.backends.local]\ntype = "llama-router"\nmanaged = false\nurl = "http://127.0.0.1:{server.server_address[1]}"\n'
        f'[gateway.backends.far]\ntype = "openai"\nurl = "https://example.invalid"\nallow_remote = true\n'
        '[gateway.aliases.near]\nbackend = "local"\nmodel = "m"\n'
        '[gateway.aliases.away]\nbackend = "far"\nmodel = "m"\n'
    )
    gateway = Gateway(load_config(cfg))
    gateway.start()
    gateway.cfg_path, gateway.token_path = cfg, token
    yield gateway
    gateway.stop()
    server.shutdown()
    server.server_close()


def call(gw, method, path, doc=None, token=""):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    conn.request(method, path, body=None if method == "GET" else json.dumps(doc or {}), headers=headers)
    resp = conn.getresponse()
    out = resp.read()
    return resp.status, (json.loads(out) if out else {})


def test_client_scope(gw):
    plain = gw.clients.issue("plain")
    remote = gw.clients.issue("far-away", allow_remote=True)
    chat = {"messages": []}
    assert call(gw, "POST", "/v1/chat/completions", {**chat, "model": "near"}, plain)[0] == 200
    status, doc = call(gw, "POST", "/v1/chat/completions", {**chat, "model": "away"}, plain)
    assert status == 403 and "--allow-remote" in doc["error"]["message"]
    assert call(gw, "POST", "/v1/chat/completions", {**chat, "model": "away"}, remote)[0] != 403
    ids = [m["id"] for m in call(gw, "GET", "/v1/models", token=plain)[1]["data"]]
    assert "near" in ids and "away" not in ids


def test_client_cannot_call_admin_routes(gw):
    token = gw.clients.issue("plain")
    assert call(gw, "GET", "/hal/v1/status", token=token)[0] == 403
    assert call(gw, "POST", "/hal/v1/gpu/release", token=token)[0] == 403


def test_client_token_narrows_unix_owner_and_unknown_is_401(gw):
    status, doc = call(gw, "GET", "/hal/v1/status", token="hal-gwc-nobody.nope")
    assert status == 401 and doc["error"]["code"] == "invalid_api_key"
    secret = gw.clients.issue("plain")
    gw.clients.revoke("plain")
    assert call(gw, "GET", "/v1/models", token=secret)[0] == 401


def test_client_name_is_logged(gw):
    token = gw.clients.issue("laptop")
    call(gw, "POST", "/v1/chat/completions", {"model": "near", "messages": []}, token)
    log_file = gw.state_dir / "requests.jsonl"
    for _ in range(100):  # the record is written just after the reply
        if log_file.exists() and log_file.read_text():
            break
        time.sleep(0.02)
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert records[-1]["principal"] == "client:laptop"


def test_owner_token_rotation_needs_no_restart(gw):
    assert gw.owner_token == "hal-gw-owner"
    gw.token_path.write_text("hal-gw-new\n")
    os.utime(gw.token_path, ns=(1, 1))
    assert gw.owner_token == "hal-gw-new"


def test_serve_refuses_in_client_mode(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[gateway]\nurl = "http://127.0.0.1:8741"\nunix_path = "{tmp_path}/g.sock"\n')
    with pytest.raises(StartError, match=r"\[gateway\]\.url"):
        Gateway(load_config(cfg)).start()


@pytest.mark.parametrize(
    "url", ["http://example.com:80", "https://127.0.0.1:1", "http://127.0.0.1", "unix:/x.sock"]
)
def test_gateway_url_must_be_loopback(tmp_path, url):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[gateway]\nurl = "{url}"\n')
    with pytest.raises(Exception, match="url"):
        load_config(cfg)


def test_gateway_url_accepts_the_tunnel_ends(tmp_path):
    for url in ("http://127.0.0.1:8741", "http://[::1]:8741"):
        cfg = tmp_path / "c.toml"
        cfg.write_text(f'[gateway]\nurl = "{url}"\n')
        assert load_config(cfg).gateway["url"] == url


def test_token_cli_issue_list_revoke(gw, capsys):
    base = ["token", "--config", str(gw.cfg_path)]
    assert cli.main([*base, "issue", "--name", "laptop", "--json"]) == cli.EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["name"] == "laptop" and gw.clients.check(doc["token"]) is not None
    assert cli.main([*base, "issue", "--name", "laptop"]) == cli.EXIT_CONFIG  # exists
    assert "--rotate" in capsys.readouterr().err
    assert cli.main([*base, "list", "--json"]) == cli.EXIT_OK
    assert doc["token"].split(".", 1)[1] not in capsys.readouterr().out
    assert cli.main([*base, "revoke", "--name", "laptop"]) == cli.EXIT_OK
    assert cli.main([*base, "revoke", "--name", "laptop"]) == cli.EXIT_REFUSED


def test_token_cli_shows_and_rotates_the_owner_token(gw, capsys):
    base = ["token", "--config", str(gw.cfg_path)]
    assert cli.main([*base, "show"]) == cli.EXIT_OK and capsys.readouterr().out.strip() == "hal-gw-owner"
    assert cli.main([*base, "rotate"]) == cli.EXIT_OK
    new = capsys.readouterr().out.strip()
    assert new.startswith("hal-gw-") and new != "hal-gw-owner" and gw.owner_token == new
