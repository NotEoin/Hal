"""Tests for the worker script `halhub/_worker/hal_worker.py`, driven over its real protocol.

Each test starts the worker as the runner will (`python -P -X utf8 hal_worker.py ...`) on the
synthetic `probe` pack and exchanges JSON lines with it (REQ-HUB-26, REQ-HUB-47, REQ-HUB-69).
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import halhub

WORKER = Path(halhub.__file__).resolve().parent / "_worker" / "hal_worker.py"
FUNCTIONS = "shout,boom,fail,when,nan,later,reveal,nested,crash,env"
READ_TIMEOUT_S = 10.0  # a worker answers in milliseconds; this only bounds a broken test


class Worker:
    """A worker process and its protocol pipes."""

    def __init__(self, root: Path, module: str, functions: str, cwd: Path) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root), "HAL_HUB_WORKER": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-P", "-X", "utf8", str(WORKER), "--module", module, "--functions", functions],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        self._buffer = b""

    def send(self, message: dict) -> None:
        self.proc.stdin.write((json.dumps({"v": 1, **message}) + "\n").encode())
        self.proc.stdin.flush()

    def send_raw(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def receive(self) -> dict:
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                if not selector.select(READ_TIMEOUT_S):
                    raise TimeoutError("worker sent nothing")
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise EOFError("worker closed its protocol stream")
                self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line)

    def call(
        self, function: str, args: dict | None = None, secrets: dict | None = None, call_id: str = "c-1"
    ) -> dict:
        self.send(
            {"op": "call", "id": call_id, "function": function, "args": args or {}, "secrets": secrets or {}}
        )
        return self.receive()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=READ_TIMEOUT_S)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()


@pytest.fixture
def worker(fixtures, tmp_path) -> Iterator[Worker]:
    w = Worker(fixtures / "probe", "halprobe.tools", FUNCTIONS + ",absent", tmp_path)
    try:
        yield w
    finally:
        w.stop()


@pytest.fixture
def ready(worker) -> dict:
    return worker.receive()


def test_worker_announces_ready_with_signatures_and_missing_names(worker, ready):
    assert ready["v"] == 1 and ready["op"] == "ready" and ready["module"] == "halprobe.tools"
    assert ready["pid"] == worker.proc.pid and ready["missing"] == ["absent"]
    assert ready["functions"]["shout"] == {
        "params": [{"name": "text", "required": True}],
        "var_keyword": False,
        "async": False,
    }
    assert ready["functions"]["later"]["async"] is True


def test_print_in_tool_does_not_corrupt_protocol(worker, ready):
    reply = worker.call("shout", {"text": "hi"})
    assert reply == {"v": 1, "op": "result", "id": "c-1", "ok": True, "result": "HI"}
    worker.send({"op": "shutdown"})
    assert worker.proc.wait(timeout=READ_TIMEOUT_S) == 0
    stderr = worker.proc.stderr.read()
    assert b"printed by the tool" in stderr and b"written to fd 1" in stderr


def test_exception_returns_class_and_message_and_traceback_only_on_stderr(worker, ready):
    reply = worker.call("boom")
    assert reply["ok"] is False
    assert reply["error"] == {"type": "ValueError", "message": "no event matches 'dentist'"}
    assert "Traceback" not in json.dumps(reply)


def test_tool_error_keeps_its_type_name(worker, ready):
    assert worker.call("fail")["error"] == {"type": "ToolError", "message": "quota reached"}


def test_arguments_that_do_not_bind_are_a_manifest_mismatch(worker, ready):
    reply = worker.call("shout", {"text": "a", "volume": 3})
    assert reply["error"]["type"] == "ArgumentsMismatch"


def test_result_uses_str_for_dates_and_refuses_nan(worker, ready):
    assert worker.call("when")["result"] == "2026-10-29 10:00:00+00:00"
    assert worker.call("nan", call_id="c-2")["error"]["type"] == "UnserialisableResult"


def test_async_function_runs_to_completion(worker, ready):
    assert worker.call("later", {"text": "x"})["result"] == "later x"


def test_secret_is_available_only_during_its_call(worker, ready):
    assert (
        worker.call("reveal", {"name": "probe_key"}, {"probe_key": "s3cret"})["result"] == "the key is s3cret"
    )
    reply = worker.call("reveal", {"name": "probe_key"}, call_id="c-2")
    assert reply["ok"] is False and reply["error"]["type"] == "KeyError"


def test_secret_is_not_in_the_worker_environment(worker, ready):
    reply = worker.call("env", secrets={"probe_key": "s3cret"})
    assert "s3cret" not in json.dumps(reply["result"])


def test_unknown_function_and_unknown_op_are_protocol_errors(worker, ready):
    assert worker.call("absent")["error"]["type"] == "ProtocolError"
    worker.send({"op": "dance", "id": "x"})
    reply = worker.receive()
    assert reply["id"] == "x" and reply["error"]["type"] == "ProtocolError"


def test_malformed_line_is_a_protocol_error_and_the_worker_goes_on(worker, ready):
    worker.send_raw(b"{not json\n")
    assert worker.receive()["error"]["type"] == "ProtocolError"
    worker.send({"op": "ping", "id": "p1"})
    assert worker.receive() == {"v": 1, "op": "pong", "id": "p1"}


def test_nested_call_round_trip(worker, ready):
    worker.send(
        {"op": "call", "id": "c-9", "function": "nested", "args": {"tool": "demo_echo"}, "secrets": {}}
    )
    request = worker.receive()
    assert request["op"] == "tool_call" and request["parent"] == "c-9"
    assert request["tool"] == "demo_echo" and request["args"] == {"text": "hi"}
    worker.send({"op": "ping", "id": "p"})  # the hub may ping while a nested call is pending
    assert worker.receive()["op"] == "pong"
    worker.send({"op": "tool_result", "id": request["id"], "ok": True, "result": "hi"})
    assert worker.receive() == {"v": 1, "op": "result", "id": "c-9", "ok": True, "result": "hi"}


def test_refused_nested_call_raises_inside_the_tool(worker, ready):
    worker.send({"op": "call", "id": "c-9", "function": "nested", "args": {"tool": "x_y"}, "secrets": {}})
    request = worker.receive()
    error = {"type": "policy_denied", "message": "x_y is not in calls"}
    worker.send({"op": "tool_result", "id": request["id"], "ok": False, "error": error})
    reply = worker.receive()
    assert reply["error"] == {"type": "ToolCallError", "message": "policy_denied: x_y is not in calls"}


def test_crashing_tool_ends_the_process_without_a_result(worker, ready):
    worker.send({"op": "call", "id": "c-1", "function": "crash", "args": {"code": 3}, "secrets": {}})
    with pytest.raises(EOFError):
        worker.receive()
    assert worker.proc.wait(timeout=READ_TIMEOUT_S) == 3


def test_end_of_input_exits_cleanly(worker, ready):
    worker.proc.stdin.close()
    assert worker.proc.wait(timeout=READ_TIMEOUT_S) == 0


def test_import_failure_exits_before_ready(fixtures, tmp_path):
    w = Worker(fixtures / "broken", "halbroken.tools", "anything", tmp_path)
    try:
        with pytest.raises(EOFError):
            w.receive()
        assert w.proc.wait(timeout=READ_TIMEOUT_S) != 0
        assert b"halbroken cannot be imported" in w.proc.stderr.read()
    finally:
        w.stop()


def test_worker_imports_nothing_from_halhub():
    source = WORKER.read_text(encoding="utf-8")
    assert "halhub" not in [
        line.split()[1] for line in source.splitlines() if line.startswith(("import ", "from "))
    ]
    assert [p.name for p in WORKER.parent.iterdir() if p.name != "__pycache__"] == ["hal_worker.py"]
