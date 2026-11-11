"""Tests for the route and delegate policies (REQ-GW-42..45), unit and through the handler."""

import http.client
import http.server
import json
import socket
import threading
from pathlib import Path

import pytest

from halgateway.config import load_config
from halgateway.modes import HalSettings
from halgateway.policies import RequestContext, run_rewrites
from halgateway.policies.delegate import DelegatePolicy, add_system_note, strip_tools
from halgateway.policies.route import RoutePolicy, TurnMemory
from halgateway.server import Gateway

USER = {"role": "user", "content": "refactor the parser"}
TOOLS = [{"name": "Bash"}, {"name": "Read"}, {"name": "Edit"}]
STRIP = ("Bash", "Edit")


def hal(**kw):
    base = dict(
        description="", main="big", agent="small", classifier="router", models_max=3, autoload=True,
        route=True, effort=False, effort_note=Path("n"), delegate_note=Path("d"), strip_tools=STRIP,
        swap=False, swap_slots=2, context_window=1000, client_timeout_ms=None,
    )  # fmt: skip
    return HalSettings(**{**base, **kw})


def ctx(messages, tools=TOOLS, fmt="anthropic"):
    body = {"messages": messages}
    if tools is not None:
        body["tools"] = tools
    return RequestContext(fmt, body, "hal/main", "big")


def policy(classify, memory=None):
    return RoutePolicy(hal(), classify, memory if memory is not None else TurnMemory())


def test_route_choice_holds_for_turn():
    calls = []

    def classify(model, text):
        calls.append((model, text))
        return "fast", 0.1

    memory = TurnMemory()
    first = ctx([USER])
    run_rewrites([policy(classify, memory)], first)
    follow = ctx([USER, {"role": "assistant", "content": "x"}, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}])  # fmt: skip
    run_rewrites([policy(classify, memory)], follow)
    assert first.model == follow.model == "small" and calls == [("router", "refactor the parser")]
    assert first.decisions["route"] == "fast p_strong=0.10" and follow.decisions["route"] == "fast (held)"


def test_new_user_text_is_classified_again():
    answers = iter([("fast", 0.1), ("strong", 0.9)])
    memory = TurnMemory()
    one, two = ctx([USER]), ctx([USER])
    run_rewrites([policy(lambda m, t: next(answers), memory)], one)
    run_rewrites([policy(lambda m, t: next(answers), memory)], two)
    # the same first message means the same conversation: the second text replaces the held choice
    assert (one.model, two.model) == ("small", "big")


def test_side_request_goes_fast():
    side = ctx([USER], tools=None)
    run_rewrites([policy(lambda m, t: pytest.fail("no classifier call"))], side)
    assert side.model == "small" and side.decisions == {"route": "side -> fast"}


def test_classifier_failure_uses_default():
    def broken(model, text):
        raise TimeoutError("30 s")

    c = ctx([USER])
    run_rewrites([policy(broken)], c)
    assert c.model == "big" and c.decisions["route"].startswith("strong p_strong=nan")


def test_route_memory_is_bounded():
    memory = TurnMemory(size=3)
    for n in range(5):
        memory.put(str(n), "fast")
    assert len(memory) == 3 and memory.get("0") is None and memory.get("4") == "fast"
    memory.get("2")
    memory.put("5", "fast")
    assert memory.get("2") == "fast" and memory.get("3") is None


@pytest.mark.parametrize("fmt", ["anthropic", "openai"])
def test_delegate_strips_tools_and_adds_note(fmt):
    tools = (
        TOOLS
        if fmt == "anthropic"
        else [{"type": "function", "function": {"name": t["name"]}} for t in TOOLS]
    )
    c = ctx([USER], tools=tools, fmt=fmt)
    run_rewrites([DelegatePolicy(STRIP, "DELEGATE", "big")], c)
    names = [t.get("name") or t["function"]["name"] for t in c.body["tools"]]
    assert names == ["Read"] and c.decisions == {"delegate": "tools stripped"}
    assert "DELEGATE" in json.dumps(c.body)


def test_delegate_leaves_a_fast_model_request_alone():
    c = ctx([USER])
    c.model = "small"
    run_rewrites([DelegatePolicy(STRIP, "DELEGATE", "big")], c)
    assert c.body["tools"] == TOOLS and "system" not in c.body


def test_system_note_joins_each_system_shape():
    block = [{"type": "text", "text": "S"}]
    cases = [(None, "N"), ("S", "S\n\nN"), (block, [*block, {"type": "text", "text": "N"}])]
    for system, expected in cases:
        body = {"system": system} if system else {}
        add_system_note(body, "anthropic", "N")
        assert body["system"] == expected
    body = {"messages": [{"role": "system", "content": "S"}, USER]}
    add_system_note(body, "openai", "N")
    assert body["messages"][0]["content"] == "S\n\nN"
    bare = {"messages": [USER]}
    add_system_note(bare, "openai", "N")
    assert bare["messages"][0] == {"role": "system", "content": "N"}
    strip_tools(bare, ["x"])
    assert bare["tools"] == []


# --- through the handler -----------------------------------------------------------------------

SEEN = []


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        SEEN.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture
def gateway(tmp_path):
    SEEN.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = tmp_path / "token"
    token.write_text("t\n")
    token.chmod(0o600)
    note = tmp_path / "d.txt"
    note.write_text("DELEGATE NOTE")
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\nmain = ""\nagent = ""\n'
        f'[gateway.backends.local]\ntype = "llama-router"\nmanaged = false\n'
        f'url = "http://127.0.0.1:{server.server_address[1]}"\n'
    )
    gw = Gateway(load_config(cfg))
    router = gw.backends["local"]
    router.hal = hal(delegate_note=note)
    router.classify = lambda model, text: ("fast", 0.2) if "rename" in text else ("strong", 0.8)
    gw.resolver.mode_aliases["hal/main"] = ("local", "big")
    gw.start()
    yield gw
    gw.stop()
    server.shutdown()
    server.server_close()


def post(gw, doc):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    conn.request("POST", "/v1/messages", body=json.dumps(doc))
    return conn.getresponse().status


def test_handler_routes_a_fast_turn_to_the_agent_model_with_its_tools(gateway):
    body = {"model": "hal/main", "tools": TOOLS, "messages": [{"role": "user", "content": "rename x"}]}
    assert post(gateway, body) == 200
    assert SEEN[0]["model"] == "small" and SEEN[0]["tools"] == TOOLS


def test_handler_keeps_a_strong_turn_on_main_with_delegation(gateway):
    body = {"model": "hal/main", "tools": TOOLS, "messages": [USER]}
    assert post(gateway, body) == 200
    assert SEEN[0]["model"] == "big" and [t["name"] for t in SEEN[0]["tools"]] == ["Read"]
    assert SEEN[0]["system"] == "DELEGATE NOTE"
