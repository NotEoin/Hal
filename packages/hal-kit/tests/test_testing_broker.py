"""Tests for `halkit.testing.broker`."""

import json
import socket
import stat

import pytest

from halkit.testing import FakeBroker, TempHalHome


class Client:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.file = self.sock.makefile("rwb")
        self.next_id = 0

    def call(self, method, params=None):
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        self.file.write(json.dumps(message).encode() + b"\n")
        self.file.flush()
        return json.loads(self.file.readline())

    def notify(self, method):
        self.file.write(json.dumps({"jsonrpc": "2.0", "method": method}).encode() + b"\n")
        self.file.flush()

    def close(self):
        self.file.close()
        self.sock.close()


@pytest.fixture
def broker():
    with TempHalHome() as home, FakeBroker(home.runtime / "broker.sock") as b:
        yield b


@pytest.fixture
def client(broker):
    c = Client(broker.socket_path)
    yield c
    c.close()


def test_socket_is_mode_0600(broker):
    assert stat.S_IMODE(broker.socket_path.stat().st_mode) == 0o600


def test_initialize_echoes_the_requested_protocol_version(client):
    reply = client.call("initialize", {"protocolVersion": "2025-03-26"})
    assert reply["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in reply["result"]["capabilities"]


def test_tools_list_returns_the_registered_tools(broker, client):
    broker.add_tool("echo", "x", description="Echo", input_schema={"type": "object"})
    tools = client.call("tools/list")["result"]["tools"]
    assert [(t["name"], t["description"]) for t in tools] == [("echo", "Echo")]


def test_tools_call_returns_text_and_records_the_call(broker, client):
    broker.add_tool("echo", "hello")
    result = client.call("tools/call", {"name": "echo", "arguments": {"a": 1}})["result"]
    assert result == {"content": [{"type": "text", "text": "hello"}], "isError": False}
    assert broker.calls == [("echo", {"a": 1})]


def test_callable_result_receives_the_arguments(broker, client):
    broker.add_tool("add", lambda args: str(args["a"] + args["b"]))
    result = client.call("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}})["result"]
    assert result["content"][0]["text"] == "5"


def test_mapping_result_is_returned_whole_so_meta_can_be_set(broker, client):
    broker.add_tool("t", {"content": [], "isError": False, "_meta": {"hal": {"status": "allowed"}}})
    result = client.call("tools/call", {"name": "t"})["result"]
    assert result["_meta"]["hal"]["status"] == "allowed"


def test_exception_result_becomes_an_error_result(broker, client):
    broker.add_tool("bad", RuntimeError("nope"))
    result = client.call("tools/call", {"name": "bad"})["result"]
    assert result["isError"] is True and result["content"][0]["text"] == "nope"


def test_unknown_tool_is_a_jsonrpc_error_and_not_recorded(broker, client):
    reply = client.call("tools/call", {"name": "ghost"})
    assert reply["error"]["code"] == -32602
    assert broker.calls == []


def test_unknown_method_is_a_jsonrpc_error(client):
    assert client.call("nope")["error"]["code"] == -32601


def test_notifications_get_no_reply(client):
    client.notify("notifications/initialized")
    assert client.call("ping")["result"] == {}


def test_malformed_json_gets_a_parse_error(client):
    client.file.write(b"{not json\n")
    client.file.flush()
    assert json.loads(client.file.readline())["error"]["code"] == -32700


def test_stop_removes_the_socket(tmp_path):
    b = FakeBroker(tmp_path / "b.sock").start()
    b.stop()
    assert not (tmp_path / "b.sock").exists()


def test_a_socket_path_over_107_bytes_is_refused(tmp_path):
    from halkit.paths import SocketPathTooLong

    with pytest.raises(SocketPathTooLong):
        FakeBroker(tmp_path / ("x" * 120) / "b.sock")
