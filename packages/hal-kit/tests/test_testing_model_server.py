"""Tests for `halkit.testing.model_server`."""

import http.client
import json
import socket

import pytest

from halkit.testing import FakeModelServer, Reply, TempHalHome
from halkit.testing.model_server import fake_embedding


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        self.sock.connect(str(self._path))


def post(conn, path, body):
    conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    return resp.status, resp.read()


@pytest.fixture
def server():
    with FakeModelServer() as s:
        yield s


@pytest.fixture
def conn(server):
    c = http.client.HTTPConnection("127.0.0.1", int(server.url.rsplit(":", 1)[1]), timeout=5)
    yield c
    c.close()


def test_chat_completion_returns_the_scripted_text(server, conn):
    server.script("hi there")
    status, raw = post(conn, "/v1/chat/completions", {"model": "m", "messages": []})
    body = json.loads(raw)
    assert status == 200
    assert body["choices"][0]["message"]["content"] == "hi there"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["model"] == "m"


def test_chat_completion_tool_call_has_json_string_arguments(server, conn):
    server.script(Reply(tool_calls=[("read", {"path": "a.txt"})]))
    _, raw = post(conn, "/v1/chat/completions", {"messages": []})
    choice = json.loads(raw)["choices"][0]
    call = choice["message"]["tool_calls"][0]["function"]
    assert (call["name"], json.loads(call["arguments"])) == ("read", {"path": "a.txt"})
    assert choice["finish_reason"] == "tool_calls"


def test_replies_are_served_in_order_across_apis(server, conn):
    server.script("one", "two", "three")
    _, a = post(conn, "/v1/chat/completions", {})
    _, b = post(conn, "/v1/responses", {})
    _, c = post(conn, "/v1/messages", {})
    assert json.loads(a)["choices"][0]["message"]["content"] == "one"
    assert json.loads(b)["output"][0]["content"][0]["text"] == "two"
    assert json.loads(c)["content"][0]["text"] == "three"


def test_responses_api_function_call_shape(server, conn):
    server.script(Reply(tool_calls=[("run", {"cmd": "ls"})]))
    _, raw = post(conn, "/v1/responses", {})
    item = json.loads(raw)["output"][0]
    assert (item["type"], item["name"], json.loads(item["arguments"])) == (
        "function_call",
        "run",
        {"cmd": "ls"},
    )


def test_anthropic_messages_tool_use_shape(server, conn):
    server.script(Reply(text="ok", tool_calls=[("run", {"cmd": "ls"})]))
    _, raw = post(conn, "/v1/messages", {})
    body = json.loads(raw)
    assert [b["type"] for b in body["content"]] == ["text", "tool_use"]
    assert body["content"][1]["input"] == {"cmd": "ls"} and body["stop_reason"] == "tool_use"


def test_unscripted_request_fails_loudly_with_500_and_is_recorded(server, conn):
    status, raw = post(conn, "/v1/chat/completions", {"x": 1})
    assert status == 500 and b"no scripted reply" in raw
    assert server.requests[-1]["body"] == {"x": 1}


def test_scripted_error_status_is_returned(server, conn):
    server.script(Reply(status=503))
    status, _ = post(conn, "/v1/chat/completions", {})
    assert status == 503


def test_requests_record_path_headers_and_body(server, conn):
    server.script("x")
    conn.request("POST", "/v1/chat/completions", json.dumps({"a": 1}), {"Authorization": "Bearer t"})
    conn.getresponse().read()
    seen = server.requests[0]
    assert seen["path"] == "/v1/chat/completions" and seen["body"] == {"a": 1}
    assert seen["headers"]["Authorization"] == "Bearer t"


def test_embeddings_are_deterministic_and_distinct(server, conn):
    _, raw = post(conn, "/v1/embeddings", {"input": ["a", "b", "a"]})
    vectors = [d["embedding"] for d in json.loads(raw)["data"]]
    assert vectors[0] == vectors[2] != vectors[1]
    assert vectors[0] == fake_embedding("a")


def test_embeddings_accept_a_single_string(server, conn):
    _, raw = post(conn, "/v1/embeddings", {"input": "solo"})
    assert len(json.loads(raw)["data"]) == 1


def test_models_list_carries_context_length(server, conn):
    conn.request("GET", "/v1/models")
    body = json.loads(conn.getresponse().read())
    assert body["data"][0]["context_length"] == 8192


def test_health_and_unknown_routes(server, conn):
    conn.request("GET", "/health")
    assert conn.getresponse().read() == b'{"status": "ok"}'
    conn.request("GET", "/api/tags")
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 404


def test_count_tokens_endpoints_answer(server, conn):
    status, raw = post(conn, "/v1/messages/count_tokens", {"messages": []})
    assert status == 200 and json.loads(raw)["input_tokens"] >= 1


def test_streaming_chat_sends_sse_chunks_then_done(server, conn):
    server.script("alpha beta")
    conn.request("POST", "/v1/chat/completions", json.dumps({"stream": True}))
    resp = conn.getresponse()
    text = resp.read().decode()
    assert resp.getheader("Content-Type") == "text/event-stream"
    events = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    pieces = [json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1]]
    assert "".join(pieces) == "alpha beta "


def test_non_json_body_is_a_400(server, conn):
    conn.request("POST", "/v1/chat/completions", b"not json")
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 400


def test_unix_socket_mode_serves_the_same_api():
    with TempHalHome() as home:
        with FakeModelServer(home.runtime / "model.sock") as server:
            server.script("over a socket")
            conn = UnixConnection(server.socket_path)
            status, raw = post(conn, "/v1/chat/completions", {})
            conn.close()
        assert status == 200 and json.loads(raw)["choices"][0]["message"]["content"] == "over a socket"
        assert not server.socket_path.exists()


def test_url_is_unavailable_before_start():
    with pytest.raises(RuntimeError, match="started TCP server"):
        _ = FakeModelServer().url
