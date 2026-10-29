"""Tests for the hub broker splice, using a fake child command instead of the real hal-hub."""

import socket
import sys

from halsandbox.broker import Broker

ECHO = (sys.executable, "-c", "import sys\nfor l in sys.stdin: sys.stdout.write('re:'+l); sys.stdout.flush()")


def ask(path, line):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(path))
    s.sendall(line)
    s.shutdown(socket.SHUT_WR)
    data = b""
    while chunk := s.recv(4096):
        data += chunk
    s.close()
    return data


def test_each_connection_gets_its_own_child_that_answers(sockdir):
    b = Broker(sockdir / "h.sock", ECHO)
    b.start()
    try:
        assert ask(b.socket_path, b"one\n") == b"re:one\n"
        assert ask(b.socket_path, b"two\n") == b"re:two\n"
    finally:
        b.stop()


def test_socket_is_private_and_removed_on_stop(sockdir):
    b = Broker(sockdir / "h.sock", ECHO)
    mode = b.socket_path.stat().st_mode & 0o777
    b.stop()
    assert mode == 0o600 and not b.socket_path.exists()


def test_default_command_serves_hal_hub_mcp_out_of_band():
    from halsandbox.broker import DEFAULT_COMMAND

    assert (
        DEFAULT_COMMAND[-4:] == ("mcp", "--approvals", "out-of-band")[-4:] or "out-of-band" in DEFAULT_COMMAND
    )
