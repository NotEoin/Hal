"""Tests for session tokens, scopes, leases and the session admin routes (REQ-GW-60 to 62, 54)."""

import http.client
import http.server
import json
import socket
import threading

import pytest

from halgateway.config import load_config
from halgateway.errors import BadRequest, Forbidden, TooManyRequests
from halgateway.server import Gateway
from halgateway.sessions import SessionExpired, SessionRegistry, SessionUnknown

DEFAULTS = {"max_request_bytes": 1000, "max_concurrent": 2, "ttl_s": 100}


class Clock:
    now = 5000.0

    def __call__(self):
        return self.now


def registry(**kw):
    return SessionRegistry(DEFAULTS, clock=Clock(), **kw)


def test_session_token_lifecycle():
    reg = registry()
    session, token = reg.mint({"label": "hal code"})
    assert token.startswith(f"hal-sess-{session.id}.") and len(session.id) == 8
    assert reg.find(token) is session
    assert reg.revoke(session.id) and not reg.revoke(session.id)
    with pytest.raises(SessionUnknown):
        reg.find(token)


def test_only_token_hash_is_stored():
    reg = registry()
    session, token = reg.mint({"label": "x"})
    secret = token.split(".", 1)[1]
    assert secret not in repr(session.__dict__) and secret not in json.dumps(session.public())
    assert len(session.secret_hash) == 64


def test_expired_session_is_refused_and_forgotten():
    reg = registry()
    session, token = reg.mint({"label": "x", "ttl_s": 10})
    reg.clock.now += 11
    with pytest.raises(SessionExpired) as err:
        reg.find(token)
    assert err.value.status == 401 and err.value.code == "session_expired"
    assert len(reg) == 0


def test_wrong_secret_is_session_unknown():
    reg = registry()
    session, _ = reg.mint({"label": "x"})
    with pytest.raises(SessionUnknown) as err:
        reg.find(f"hal-sess-{session.id}.nope")
    assert err.value.code == "session_unknown"


def test_defaults_come_from_the_sessions_table_and_specs_are_validated():
    session, _ = registry().mint({"label": "x"})
    assert (session.max_request_bytes, session.max_concurrent, session.aliases) == (1000, 2, ("*",))
    assert session.cls == "interactive" and not session.lease and not session.allow_remote
    bad_specs = [{}, {"label": "x", "class": "batch"}, {"label": "x", "ttl_s": 0}]
    bad_specs += [{"label": "x", "aliases": []}, {"label": "x", "lease": "yes"}]
    for bad in bad_specs:
        with pytest.raises(BadRequest):
            registry().mint(bad)


def test_session_alias_scope():
    session, _ = registry().mint({"label": "x", "aliases": ["hal-*"]})
    SessionRegistry.check_scope(session, "hal-test", False)
    with pytest.raises(Forbidden, match="may not use model"):
        SessionRegistry.check_scope(session, "gpt-4", False)


def test_session_cannot_reach_remote_by_default():
    reg = registry()
    plain, _ = reg.mint({"label": "x"})
    remote, _ = reg.mint({"label": "y", "allow_remote": True})
    with pytest.raises(Forbidden, match="remote"):
        SessionRegistry.check_scope(plain, "any", True)
    SessionRegistry.check_scope(remote, "any", True)


def test_session_concurrency_cap_is_429():
    reg = registry()
    session, _ = reg.mint({"label": "x", "max_concurrent": 1})
    reg.enter(session)
    with pytest.raises(TooManyRequests):
        reg.enter(session)
    reg.leave(session)
    reg.enter(session)


def test_lease_follows_the_session():
    events = []
    reg = registry(
        on_lease=lambda s: events.append(("add", s.id)), on_lease_end=lambda i: events.append(("end", i))
    )
    plain, _ = reg.mint({"label": "a"})
    leased, _ = reg.mint({"label": "b", "lease": True})
    reg.revoke(plain.id)
    reg.clock.now += 1000
    reg.reap()
    assert events == [("add", leased.id), ("end", leased.id)]


# --- through the gateway --------------------------------------------------------------------


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    gate = threading.Event()
    arrived = threading.Event()

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("slow"):
            Upstream.arrived.set()
            Upstream.gate.wait(5)
        out = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def gw(tmp_path):
    Upstream.gate.clear()
    Upstream.arrived.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = tmp_path / "token"
    token.write_text("t\n")
    token.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n'
        '[gateway.gpu]\ncoordinator = "none"\n'
        f'[gateway.backends.local]\ntype = "llama-router"\nmanaged = false\nurl = "http://127.0.0.1:{server.server_address[1]}"\n'
        '[gateway.aliases.hal-test]\nbackend = "local"\nmodel = "m"\n'
        '[gateway.aliases.other]\nbackend = "local"\nmodel = "m"\n'
    )
    gateway = Gateway(load_config(cfg))
    gateway.start()
    yield gateway
    Upstream.gate.set()
    gateway.stop()
    server.shutdown()
    server.server_close()


def call(gw, method, path, doc=None, token="", raw=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = None if method in ("GET", "DELETE") else (raw if raw is not None else json.dumps(doc or {}))
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    out = resp.read()
    return resp.status, (json.loads(out) if out else {})


def mint(gw, **spec):
    status, doc = call(gw, "POST", "/hal/v1/sessions", {"label": "t", **spec})
    assert status == 201
    return doc


def test_admin_mints_lists_and_revokes_sessions(gw):
    doc = mint(gw, aliases=["hal-*"], lease=True)
    assert doc["token"].startswith("hal-sess-") and doc["lease"] is True and doc["expires"].endswith("Z")
    status, listing = call(gw, "GET", "/hal/v1/sessions")
    assert status == 200 and "token" not in json.dumps(listing) and listing["sessions"][0]["id"] == doc["id"]
    assert call(gw, "GET", "/hal/v1/leases")[1]["leases"][0]["session_id"] == doc["id"]
    assert call(gw, "DELETE", f"/hal/v1/sessions/{doc['id']}")[0] == 204
    assert call(gw, "GET", "/hal/v1/leases")[1]["leases"] == []
    assert call(gw, "DELETE", f"/hal/v1/sessions/{doc['id']}")[0] == 404


def test_session_token_narrows_unix_owner(gw):
    token = mint(gw, aliases=["hal-*"])["token"]
    chat = {"model": "hal-test", "messages": []}
    assert call(gw, "POST", "/v1/chat/completions", chat, token)[0] == 200
    assert call(gw, "POST", "/v1/chat/completions", {**chat, "model": "other"}, token)[0] == 403
    assert call(gw, "POST", "/v1/chat/completions", {**chat, "model": "other"})[0] == 200  # the bare owner


def test_session_cannot_call_admin_or_native_routes(gw):
    token = mint(gw)["token"]
    for method, path in (("GET", "/hal/v1/status"), ("POST", "/hal/v1/sessions"), ("GET", "/api/tags"),
                         ("POST", "/hal/v1/gpu/release")):  # fmt: skip
        assert call(gw, method, path, {"label": "x"}, token)[0] == 403, path


def test_unknown_session_token_is_401(gw):
    status, doc = call(gw, "POST", "/v1/chat/completions", {"model": "hal-test"}, "hal-sess-deadbeef.nope")
    assert status == 401 and doc["error"]["code"] == "session_unknown"


def test_session_body_cap_is_413(gw):
    token = mint(gw, max_request_bytes=50)["token"]
    big = {"model": "hal-test", "messages": [{"role": "user", "content": "x" * 200}]}
    assert call(gw, "POST", "/v1/chat/completions", big, token)[0] == 413


def test_session_model_list_is_filtered(gw):
    token = mint(gw, aliases=["hal-*"])["token"]
    ids = [m["id"] for m in call(gw, "GET", "/v1/models", token=token)[1]["data"]]
    assert "hal-test" in ids and "other" not in ids


def test_session_concurrency_cap_through_the_gateway(gw):
    token = mint(gw, max_concurrent=1)["token"]
    chat = {"model": "hal-test", "messages": []}
    first = []
    thread = threading.Thread(
        target=lambda: first.append(
            call(gw, "POST", "/v1/chat/completions", {**chat, "slow": True}, token)[0]
        )
    )
    thread.start()
    assert Upstream.arrived.wait(5)
    assert call(gw, "POST", "/v1/chat/completions", chat, token)[0] == 429
    Upstream.gate.set()
    thread.join(5)
    assert first == [200]
    assert call(gw, "POST", "/v1/chat/completions", chat, token)[0] == 200  # the slot was given back


def test_leased_session_works_while_hal_is_off(gw):
    gw.gpu.off_flag.parent.mkdir(parents=True, exist_ok=True)
    gw.gpu.off_flag.write_text("")
    chat = {"model": "hal-test", "messages": []}
    assert call(gw, "POST", "/v1/chat/completions", chat)[0] == 503
    assert call(gw, "POST", "/v1/chat/completions", chat, mint(gw, lease=True)["token"])[0] == 200
    assert call(gw, "POST", "/v1/chat/completions", chat, mint(gw)["token"])[0] == 503


def test_aliases_document_lists_names_with_their_backend(gw):
    status, doc = call(gw, "GET", "/hal/v1/aliases")
    row = next(r for r in doc["aliases"] if r["name"] == "hal-test")
    assert status == 200 and row["backend"] == "local" and row["model"] == "m" and row["remote"] is False
