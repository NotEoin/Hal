"""Tests for the Ollama backend and the native routes, against a scripted fake Ollama."""

import http.client
import http.server
import json
import socket
import threading

import pytest

from halgateway.backends import make_backend
from halgateway.config import load_config
from halgateway.server import Gateway

TAGS = {"models": [{"name": "gemma:1", "model": "gemma:1", "digest": "d" * 64, "size": 5}]}


class FakeOllama(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    seen: list = []

    def log_message(self, *args):
        pass

    def _reply(self, status, doc):
        out = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        FakeOllama.seen.append(("GET", self.path, None))
        routes = {
            "/api/tags": TAGS,
            "/api/ps": {"models": [{"name": "gemma:1"}]},
            "/api/version": {"version": "0.40.1"},
            "/v1/models": {
                "data": [{"id": "gemma:1"}, {"id": "gemma:1"}, {"id": "llamacpp:" + "a" * 8}, {"id": "q:2"}]
            },
        }
        self._reply(200, routes[self.path]) if self.path in routes else self._reply(404, {"error": "x"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.seen.append(("POST", self.path, body))
        if self.path == "/api/chat" and "options" in body:
            reply = {"message": {"role": "assistant", "content": "pong"}, "done_reason": "stop"}
            if body.get("logprobs"):
                reply["logprobs"] = [{"token": "yes", "logprob": -0.1, "top_logprobs": []}]
            return self._reply(200, {**reply, "prompt_eval_count": 7, "eval_count": 2})
        self._reply(200, {"ok": True}) if self.path.startswith("/api/") else self._reply(404, {"error": "x"})


@pytest.fixture
def fake():
    FakeOllama.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def pin(tmp_path, name="gemma:1", digest="d" * 64, profile=None):
    """Writes the user's models.toml with one pinned Ollama model."""
    manifest = tmp_path / "userconfig" / "models.toml"
    manifest.parent.mkdir(exist_ok=True)
    extra = f'profile = "{profile}"\n' if profile else ""
    manifest.write_text(
        f'schema = 1\n[[model]]\nname = "{name}"\nsource = "ollama:{name}@sha256:{digest}"\n{extra}'
    )


@pytest.fixture
def gateway(tmp_path, fake):
    pin(tmp_path, profile="gemma4")
    token = tmp_path / "t"
    token.write_text("hal-gw-t\n")
    token.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[hal]\nollama_url = "http://127.0.0.1:{fake}"\nchat_model = "gemma:1"\n'
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n'
        '[gateway.aliases.coder]\nbackend = "ollama"\nmodel = "gemma:1"\n'
    )
    gw = Gateway(load_config(cfg))
    gw.start()
    yield gw
    gw.stop()


def call(gw, method, path, body=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    return resp, resp.read()


def test_count_tokens_is_estimated_for_ollama(gateway):
    resp, raw = call(gateway, "POST", "/v1/messages/count_tokens", {"model": "coder", "messages": []})
    assert resp.status == 200 and json.loads(raw)["input_tokens"] >= 1
    assert resp.getheader("X-Hal-Estimate") == "chars/4"
    assert not [s for s in FakeOllama.seen if "count_tokens" in s[1]]


@pytest.mark.parametrize("verb", ["pull", "push", "create", "copy", "delete", "blobs/sha256:ab"])
def test_ollama_management_routes_are_absent(gateway, verb):
    assert call(gateway, "POST", f"/api/{verb}", {"model": "coder"})[0].status == 404
    assert call(gateway, "DELETE", f"/api/{verb}")[0].status == 404
    assert not [s for s in FakeOllama.seen if s[1].startswith("/api/" + verb.split("/")[0])]


def test_native_chat_rewrites_the_alias_to_the_upstream_model(gateway):
    resp, _ = call(gateway, "POST", "/api/chat", {"model": "coder", "messages": []})
    assert resp.status == 200
    assert FakeOllama.seen[-1] == ("POST", "/api/chat", {"model": "gemma:1", "messages": []})


def test_tags_include_exposed_models_and_alias_entries(gateway):
    resp, raw = call(gateway, "GET", "/api/tags")
    names = [m["name"] for m in json.loads(raw)["models"]]
    assert resp.status == 200 and names == ["gemma:1", "coder"]
    alias = json.loads(raw)["models"][1]
    assert alias["model"] == "coder" and alias["digest"] == "d" * 64


def test_ps_and_version_pass_through(gateway):
    assert json.loads(call(gateway, "GET", "/api/version")[1]) == {"version": "0.40.1"}
    assert json.loads(call(gateway, "GET", "/api/ps")[1])["models"][0]["name"] == "gemma:1"


def test_models_listing_drops_llamacpp_ids_and_duplicates(gateway):
    ids = [m["id"] for m in json.loads(call(gateway, "GET", "/v1/models")[1])["data"]]
    assert ids.count("ollama/gemma:1") == 1
    assert not [i for i in ids if "llamacpp" in i]


def test_unload_posts_keep_alive_zero_and_loaded_lists_ps(gateway):
    backend = make_backend("ollama", gateway.cfg.backends["ollama"], gateway.cfg.ollama_url)
    assert backend.loaded() == ["gemma:1"]
    backend.unload("gemma:1")
    assert FakeOllama.seen[-1] == ("POST", "/api/generate", {"model": "gemma:1", "keep_alive": 0})


def test_an_unload_waits_until_the_runner_has_left_vram(gateway, monkeypatch):
    # regression: vLLM started while the unloaded model still held 2 GB and refused to start
    backend = make_backend("ollama", gateway.cfg.backends["ollama"], gateway.cfg.ollama_url)
    answers = iter([["gemma:1"], ["gemma:1"], []])
    monkeypatch.setattr(backend, "in_vram", lambda: next(answers))
    assert backend.wait_unloaded(["gemma:1"], poll_s=0)
    monkeypatch.setattr(backend, "in_vram", lambda: ["gemma:1"])
    assert not backend.wait_unloaded(["gemma:1"], timeout_s=0, poll_s=0)


def test_native_route_on_a_backend_without_it_is_404(tmp_path, fake):
    token = tmp_path / "t"
    token.write_text("hal-gw-t\n")
    token.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n'
        f'[gateway.backends.o]\ntype = "openai"\nurl = "http://127.0.0.1:{fake}"\n'
    )
    gw = Gateway(load_config(cfg))
    gw.start()
    try:
        resp, raw = call(gw, "POST", "/api/chat", {"model": "o/x"})
        assert resp.status == 404 and b"ollama_native" in raw
        assert call(gw, "GET", "/api/tags")[0].status == 404
    finally:
        gw.stop()


def test_unpinned_ollama_model_is_refused(tmp_path, fake):
    gw = make_gateway(tmp_path, fake, pinned=False)
    resp, raw = call(gw, "POST", "/v1/chat/completions", {"model": "ollama/gemma:1"})
    assert resp.status == 503 and json.loads(raw)["error"]["code"] == "model_unverified"
    assert not [s for s in FakeOllama.seen if s[0] == "POST"]


def test_moved_ollama_tag_is_refused(tmp_path, fake):
    gw = make_gateway(tmp_path, fake, pinned=True, digest="e" * 64)
    resp, raw = call(gw, "POST", "/api/chat", {"model": "gemma:1"})
    assert resp.status == 503 and "moved" in json.loads(raw)["error"]


def test_a_pin_saved_while_the_gateway_runs_is_used_without_a_restart(tmp_path, fake):
    # regression: a `hal tune` save remade a role's Ollama model and pinned it, and the gateway
    # kept the pins it read at start, answering 503 until it was restarted
    gw = make_gateway(tmp_path, fake, pinned=True, digest="e" * 64)
    assert call(gw, "POST", "/api/chat", {"model": "gemma:1"})[0].status == 503
    pin(tmp_path, digest="d" * 64)
    assert call(gw, "POST", "/api/chat", {"model": "gemma:1"})[0].status == 200


def test_pinned_ollama_digest_is_read_once_per_30_seconds(gateway):
    for _ in range(3):
        assert call(gateway, "POST", "/api/chat", {"model": "gemma:1"})[0].status == 200
    assert [s for s in FakeOllama.seen if s[1] == "/api/tags"].__len__() == 1


def test_manifest_profile_is_listed_as_meta_hal_profile(gateway):
    resp, raw = call(gateway, "GET", "/v1/models")
    entries = {m["id"]: m for m in json.loads(raw)["data"]}
    assert entries["ollama/gemma:1"]["meta"] == {"hal_profile": "gemma4"}
    assert "meta" not in entries["ollama/q:2"]


def make_gateway(tmp_path, fake, *, pinned, digest="d" * 64):
    if pinned:
        pin(tmp_path, digest=digest)
    token = tmp_path / "t"
    token.write_text("hal-gw-t\n")
    token.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[hal]\nollama_url = "http://127.0.0.1:{fake}"\nchat_model = "gemma:1"\n'
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n'
    )
    gw = Gateway(load_config(cfg))
    gw.start()
    built.append(gw)
    return gw


built: list = []


@pytest.fixture(autouse=True)
def _stop_built():
    yield
    while built:
        built.pop().stop()


def test_chat_options_reach_ollama_through_the_native_route(gateway):
    body = {"model": "coder", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}
    resp, raw = call(gateway, "POST", "/v1/chat/completions", {**body, "options": {"num_gpu": 0}})
    reply = json.loads(raw)
    assert resp.status == 200 and reply["choices"][0]["message"]["content"] == "pong"
    assert reply["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    assert FakeOllama.seen[-1] == (
        "POST",
        "/api/chat",
        {
            "model": "gemma:1",
            "messages": body["messages"],
            "stream": False,
            "options": {"num_gpu": 0, "num_predict": 5},
        },
    )


def test_chat_logprobs_ride_the_native_route_and_come_back_in_openai_shape(gateway):
    body = {"model": "coder", "messages": [{"role": "user", "content": "x"}], "options": {"num_gpu": 0}}
    resp, raw = call(gateway, "POST", "/v1/chat/completions", {**body, "logprobs": True, "top_logprobs": 5})
    choice = json.loads(raw)["choices"][0]
    assert resp.status == 200
    assert choice["logprobs"]["content"][0]["token"] == "yes"
    sent = FakeOllama.seen[-1][2]
    assert (sent["logprobs"], sent["top_logprobs"]) == (True, 5)


def test_think_false_reaches_the_native_chat_route(gateway):
    # regression: the native route dropped `think`, so Granite Guardian reasoned through its
    # 16-token budget and the classifier never saw a yes/no score
    body = {"model": "coder", "messages": [{"role": "user", "content": "x"}], "options": {"num_gpu": 0}}
    call(gateway, "POST", "/v1/chat/completions", {**body, "think": False})
    assert FakeOllama.seen[-1][2]["think"] is False


def test_keep_alive_option_is_sent_as_ollamas_top_level_field(gateway):
    body = {"model": "coder", "messages": [{"role": "user", "content": "x"}]}
    call(gateway, "POST", "/v1/chat/completions", {**body, "options": {"num_gpu": 0, "keep_alive": "30m"}})
    sent = FakeOllama.seen[-1][2]
    assert sent["keep_alive"] == "30m" and sent["options"] == {"num_gpu": 0}


@pytest.mark.parametrize(
    "extra",
    [
        {"options": {"model_path": "/etc"}},
        {"options": [1]},
        {"options": {"num_gpu": 0}, "tools": [{}]},
        {"options": {"keep_alive": "forever"}},
        {"options": {"keep_alive": -1}},
    ],
)
def test_chat_options_that_are_not_allowed_are_400(gateway, extra):
    body = {"model": "coder", "messages": [{"role": "user", "content": "x"}], **extra}
    resp, _ = call(gateway, "POST", "/v1/chat/completions", body)
    assert resp.status == 400 and not [s for s in FakeOllama.seen if s[1] == "/api/chat"]
