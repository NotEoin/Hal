"""Tests for `halgateway.hints`: the effort hint and role defaults, unit and through the gateway."""

import http.client
import http.server
import json
import socket
import threading

import pytest

from halgateway import hints
from halgateway.config import load_config
from halgateway.server import Gateway
from halkit.modelconf import Role


def role(**kw):
    return Role("chat", "local", "qwen3:8b", **kw)


def apply(body, *, header=None, alias="hal-chat", model="qwen3:8b", backend="ollama", chat=True, roles=None):
    return hints.apply_hints(
        body,
        header=header,
        alias=alias,
        upstream_model=model,
        backend_type=backend,
        is_chat=chat,
        roles=roles or {},
    )


def test_header_beats_the_body_field_and_the_field_never_goes_upstream():
    body = {"hal_effort": "low", "messages": []}
    apply(body, header="off")
    assert body["think"] is False and "hal_effort" not in body


def test_body_field_is_used_without_a_header():
    body = {"hal_effort": "high"}
    apply(body)
    assert body["think"] is True


def test_role_default_effort_and_temperature_apply_to_a_role_alias_only():
    roles = {"chat": role(effort="medium", temperature=0.3)}
    body = {}
    apply(body, roles=roles)
    assert body == {"think": True, "temperature": 0.3}
    other = {}
    apply(other, alias="my-alias", roles=roles)
    assert other == {}


def test_a_field_the_client_set_is_not_overwritten():
    body = {"think": False, "temperature": 1.0}
    apply(body, header="high", roles={"chat": role(temperature=0.3)})
    assert body == {"think": False, "temperature": 1.0}


def test_template_kwargs_are_merged_not_replaced():
    body = {"chat_template_kwargs": {"custom": 1}}
    apply(body, header="off", backend="openai")
    assert body["chat_template_kwargs"] == {"custom": 1, "enable_thinking": False}


def test_a_model_that_cannot_think_is_left_alone_with_a_note():
    body = {}
    decided = apply(body, header="high", model="llama3.1:8b")
    assert body == {} and "does not think" in str(decided["effort"])


def test_an_unknown_level_is_ignored_with_a_note():
    body = {"messages": []}
    assert "unknown level" in str(apply(body, header="extreme")["effort"])
    assert body == {"messages": []}


def test_effort_is_ignored_off_the_chat_completions_route():
    body = {"hal_effort": "high"}
    assert "chat completions only" in str(apply(body, chat=False)["effort"])
    assert body == {}


def test_llama_router_gets_template_kwargs_and_a_reasoning_budget():
    body = {}
    apply(body, header="low", backend="llama-router")
    assert body["reasoning_budget"] == 1024 and body["chat_template_kwargs"] == {"enable_thinking": True}


def test_effort_off_disables_thinking_on_a_managed_openai_server():
    body = {"messages": []}
    decided = apply(
        body, header="off", alias="qwen/qwen3.8-27b", model="qwen3.8-27b", backend="managed-openai"
    )
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and decided == {"effort": "off"}


def test_role_family_overrides_name_detection():
    body = {}
    apply(body, header="high", model="my-finetune", roles={"chat": role(family="qwen3")})
    assert body["think"] is True


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        doc = {"models": [{"name": "qwen3:8b", "digest": "d" * 64, "size": 5}]}
        out = json.dumps(doc if self.path == "/api/tags" else {"data": [{"id": "qwen3:8b"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        Upstream.seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        out = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def gateway(tmp_path):
    Upstream.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    (tmp_path / "userconfig").mkdir()
    (tmp_path / "userconfig" / "models.toml").write_text(
        f'schema = 1\n[[model]]\nname = "qwen3:8b"\nsource = "ollama:qwen3:8b@sha256:{"d" * 64}"\n'
    )
    token = tmp_path / "t"
    token.write_text("hal-gw-t\n")
    token.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[hal]\nollama_url = "http://127.0.0.1:{port}"\nchat_model = "qwen3:8b"\n'
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n'
        f'[models.sources.local]\nkind = "ollama"\nurl = "http://127.0.0.1:{port}"\n'
        '[models.roles.chat]\nsource = "local"\nmodel = "qwen3:8b"\neffort = "high"\ntemperature = 0.2\n'
    )
    gw = Gateway(load_config(cfg))
    gw.start()
    yield gw
    gw.stop()
    server.shutdown()
    server.server_close()


def post(gw, body, headers=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    conn.request("POST", "/v1/chat/completions", body=json.dumps(body), headers=headers or {})
    resp = conn.getresponse()
    resp.read()
    return resp.status


def test_gateway_applies_the_role_default_when_the_request_has_no_hint(gateway):
    assert post(gateway, {"model": "hal-chat", "messages": []}) == 200
    assert Upstream.seen[-1]["think"] is True and Upstream.seen[-1]["temperature"] == 0.2


def test_gateway_header_overrides_the_role_default_and_is_not_forwarded(gateway):
    assert post(gateway, {"model": "hal-chat", "messages": []}, {"X-Hal-Effort": "off"}) == 200
    assert Upstream.seen[-1]["think"] is False and "hal_effort" not in Upstream.seen[-1]
