"""Tests for the lifeline socket: hello, stop, EOF on close, mode, path limit."""

import json
import socket
import stat
import time

import pytest

from halkit import paths
from halsandbox.errors import SandboxError
from halsandbox.lifeline import Lifeline

SID = "0123456789ab"


def _connect(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    s.connect(str(path))
    return s


def _line(s):
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(1)
        if not chunk:
            return None
        buf += chunk
    return json.loads(buf)


@pytest.fixture
def life(sockdir):
    lf = Lifeline(sockdir / "lifeline.sock", SID)
    lf.start()
    yield lf
    lf.close()


def test_socket_is_private_and_greets_with_the_session(life):
    assert stat.S_IMODE(life.path.stat().st_mode) == 0o600
    with _connect(life.path) as s:
        assert _line(s) == {"hal": "lifeline", "v": 1, "session": SID}


def test_request_stop_sends_the_grace_period(life):
    with _connect(life.path) as s:
        _line(s)
        deadline = time.monotonic() + 2
        while life.connections() < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        life.request_stop(3.0)
        assert _line(s) == {"hal": "stop", "grace_s": 3.0}


def test_close_gives_the_box_end_of_file(life):
    s = _connect(life.path)
    _line(s)
    deadline = time.monotonic() + 2
    while life.connections() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    life.close()
    assert _line(s) is None
    assert not life.path.exists()
    s.close()


def test_close_twice_is_fine(life):
    life.close()
    life.close()


def test_start_twice_is_refused(life):
    with pytest.raises(SandboxError):
        life.start()


def test_too_long_socket_path_fails_before_binding(tmp_path):
    with pytest.raises(paths.SocketPathTooLong):
        Lifeline(tmp_path / ("d" * 120) / "lifeline.sock", SID)
