"""Tests for `halgateway.aliases` and the `/v1/models` routes."""

import http.client
import http.server
import json
import socket
import threading

import pytest

from halgateway.aliases import Resolver
from halgateway.backends import make_backend
from halgateway.config import load_config
from halgateway.errors import Conflict, NotFound
from halgateway.server import Gateway
from halkit.config import ConfigError

BASE = """
[hal]
chat_model = "chat-m"
classifier_model = "fast-m"
code_model = ""
embed_model = "embed-m"
[gateway]
default_model = ""
main = ""
agent = ""
[gateway.backends.a]
type = "openai"
url = "http://127.0.0.1:1"
[gateway.backends.b]
type = "openai"
url = "http://127.0.0.1:2"
[gateway.aliases.coder]
backend = "a"
model = "coder-up"
context_window = 4096
slots = 2
[gateway.aliases.catch]
backend = "b"
model = "catch-up"
patterns = ["claude-*"]
"""
LISTS = {"a": ["chat-m", "both", "only-a"], "b": ["fast-m", "both", "embed-m"]}


def build(tmp_path, text=BASE, lists=LISTS):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(text)
    cfg = load_config(cfg_file)
    backends = {n: make_backend(n, e, cfg.ollama_url) for n, e in cfg.backends.items()}
    return Resolver(cfg, backends, lambda b: list(lists.get(b.name, [])))


def pair(target):
    return target.backend.name, target.model


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("coder", ("a", "coder-up")),  # 1 user alias
        ("hal-chat", ("a", "chat-m")),  # 2 role alias via the model lists
        ("hal-classifier", ("b", "fast-m")),
        ("b/anything", ("b", "anything")),  # 4 backend/model
        ("only-a", ("a", "only-a")),  # 5 bare name in one list
        ("claude-sonnet-x", ("b", "catch-up")),  # 6 pattern
    ],
)
def test_resolution_order(tmp_path, requested, expected):
    assert pair(build(tmp_path).resolve(requested)) == expected


def test_ambiguous_bare_name_is_409(tmp_path):
    with pytest.raises(Conflict, match="a/both"):
        build(tmp_path).resolve("both")


def test_unknown_name_is_404_and_default_model_catches_it(tmp_path):
    with pytest.raises(NotFound):
        build(tmp_path).resolve("zzz")
    resolver = build(tmp_path, BASE.replace('default_model = ""', 'default_model = "b/dflt"'))
    assert pair(resolver.resolve("zzz")) == ("b", "dflt")
    assert pair(resolver.resolve(None)) == ("b", "dflt")


def test_backend_named_hal_cannot_be_addressed_by_slash_form(tmp_path):
    with pytest.raises(NotFound):
        build(tmp_path).resolve("hal/main")


def test_mode_aliases_resolve_before_backend_slash_names(tmp_path):
    resolver = build(tmp_path)
    resolver.mode_aliases["hal/main"] = ("a", "main-preset")
    assert pair(resolver.resolve("hal/main")) == ("a", "main-preset")


def test_role_alias_reports_the_window_the_active_mode_started_it_with(tmp_path):
    # regression: `hal-code` reported no context, so hal-agent fell back to 32k under a 128k mode
    resolver = build(tmp_path)
    resolver.mode_windows[("a", "chat-m")] = 131072
    assert resolver.resolve("hal-code").context_length == 131072
    listed = {e.id: e.context_length for e in resolver.listing()}
    assert listed["hal-code"] == 131072 and listed["hal-classifier"] is None


ROLES = """
[models.sources.local]
kind = "ollama"
[models.roles.chat]
source = "local"
model = "chat-m"
context = 65536
[models.roles.classifier]
source = "local"
model = "fast-m"
context = 16384
[models.roles.big]
source = "local"
model = "only-a"
context = 200704
[gateway.aliases.hal-big]
backend = "a"
model = "only-a"
"""


def test_a_role_alias_reports_the_context_hal_tune_set_for_any_backend(tmp_path):
    # regression: an Ollama or vLLM role reported no context, so hal code assumed 32k
    resolver = build(tmp_path, BASE + ROLES)
    assert resolver.resolve("hal-classifier").context_length == 16384
    assert resolver.resolve("hal-code").context_length == 65536  # empty code_model serves chat
    assert resolver.resolve("hal-big").context_length == 200704  # a named role's alias
    listed = {e.id: e.context_length for e in resolver.listing()}
    assert (listed["hal-chat"], listed["hal-embed"]) == (65536, None)


def test_the_role_context_wins_over_the_mode_window(tmp_path):
    resolver = build(tmp_path, BASE + ROLES)
    resolver.mode_windows[("a", "chat-m")] = 131072
    assert resolver.resolve("hal-chat").context_length == 65536


def test_alias_context_window_wins_over_the_mode_window(tmp_path):
    resolver = build(tmp_path)
    resolver.mode_windows[("a", "coder-up")] = 131072
    assert resolver.resolve("coder").context_length == 4096


def test_role_aliases_follow_hal_table(tmp_path):
    resolver = build(tmp_path, BASE.replace('chat_model = "chat-m"', 'chat_model = "only-a"'))
    assert pair(resolver.resolve("hal-chat")) == ("a", "only-a")


def test_empty_code_model_means_chat_model(tmp_path):
    assert build(tmp_path).role_value("hal-code") == "chat-m"


def test_role_alias_cannot_name_role_alias(tmp_path):
    with pytest.raises(ConfigError, match="hal-classifier"):
        build(tmp_path, BASE.replace('chat_model = "chat-m"', 'chat_model = "hal-classifier"'))


def test_user_alias_cannot_shadow_a_role_alias(tmp_path):
    with pytest.raises(ConfigError, match="role alias"):
        build(tmp_path, BASE + '[gateway.aliases.hal-chat]\nbackend = "a"\nmodel = "x"\n')


def test_unlisted_role_model_falls_back_to_the_only_ollama_backend(tmp_path):
    text = '[hal]\nchat_model = "not-pulled"\n'
    resolver = build(tmp_path, text, lists={})
    assert pair(resolver.resolve("hal-chat")) == ("ollama", "not-pulled")


def test_role_aliases_are_listed(tmp_path):
    ids = [e.id for e in build(tmp_path).listing()]
    assert {"hal-chat", "hal-classifier", "hal-code", "hal-embed", "coder"} <= set(ids)


def test_models_listing_has_no_duplicates_and_carries_context_length(tmp_path):
    entries = build(tmp_path).listing()
    ids = [e.id for e in entries]
    assert len(ids) == len(set(ids))
    assert next(e for e in entries if e.id == "coder").context_length == 4096


def test_alias_slots_are_listed_and_unset_slots_are_zero(tmp_path):
    entries = {e.id: e for e in build(tmp_path).listing()}
    assert entries["coder"].slots == 2 and entries["catch"].slots == 0


def test_model_lists_are_cached(tmp_path):
    calls = []
    resolver = build(tmp_path)
    resolver._list_models = lambda b: calls.append(b.name) or ["x"]
    resolver.models_of(resolver.backends["a"])
    resolver.models_of(resolver.backends["a"])
    assert calls == ["a"]


class ListingBackend(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        out = json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def served(tmp_path):
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ListingBackend)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    token = tmp_path / "t"
    token.write_text("hal-gw-t\n")
    token.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n'
        f'[gateway.backends.up]\ntype = "openai"\nurl = "http://127.0.0.1:{upstream.server_address[1]}"\n'
    )
    gw = Gateway(load_config(cfg))
    gw.start()
    yield gw
    gw.stop()
    upstream.shutdown()
    upstream.server_close()


def fetch(gw, path, headers=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


def test_models_listing_shape_follows_anthropic_header(served):
    _, openai = fetch(served, "/v1/models")
    _, anthropic = fetch(served, "/v1/models", {"anthropic-version": "2023-06-01"})
    assert openai["object"] == "list" and "up/m1" in [m["id"] for m in openai["data"]]
    assert anthropic["has_more"] is False and anthropic["data"][0]["type"] == "model"


def test_single_model_route_returns_the_entry_or_404(served):
    assert fetch(served, "/v1/models/up/m1")[1]["id"] == "up/m1"
    assert fetch(served, "/v1/models/nope")[0] == 404


def test_openai_row_carries_meta_slots_next_to_the_profile():
    from halgateway import handler
    from halgateway.aliases import ModelEntry

    row = handler._openai_entry(ModelEntry("m", "a", 4096, "gemma4", 3))
    assert row["meta"] == {"hal_profile": "gemma4", "slots": 3}
    assert "meta" not in handler._openai_entry(ModelEntry("m", "a", None))


def test_an_old_fast_model_still_feeds_the_classifier_alias(tmp_path):
    # 0.2.0 renamed hal-fast to hal-classifier; a config from before keeps working
    text = BASE.replace('classifier_model = "fast-m"', 'classifier_model = ""\nfast_model = "fast-m"')
    assert pair(build(tmp_path, text).resolve("hal-classifier")) == ("b", "fast-m")


def test_hal_main_and_agent_serve_the_aliases_the_gateway_names(tmp_path):
    # hal/main and hal/agent follow the roles hal tune sets, not a bundled mode
    text = BASE.replace('main = ""\nagent = ""', 'main = "hal-code"\nagent = "coder"') + ROLES
    resolver = build(tmp_path, text)
    main, agent = resolver.resolve("hal/main"), resolver.resolve("hal/agent")
    assert (main.alias, pair(main), main.context_length) == ("hal/main", ("a", "chat-m"), 65536)
    assert (agent.alias, pair(agent), agent.context_length) == ("hal/agent", ("a", "coder-up"), 4096)
    listed = {e.id: e.context_length for e in resolver.listing()}
    assert listed["hal/main"] == 65536 and listed["hal/agent"] == 4096


def test_a_mode_alias_cannot_point_at_itself(tmp_path):
    with pytest.raises(ConfigError, match="points a mode alias at itself"):
        build(tmp_path, BASE.replace('main = ""', 'main = "hal/agent"'))
