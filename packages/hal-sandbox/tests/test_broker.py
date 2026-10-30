"""Tests for the hub broker splice, using a fake child command instead of the real hal-hub."""

import os
import socket
import sys
import time

from halsandbox.broker import Broker

ECHO = (sys.executable, "-c", "import sys\nfor l in sys.stdin: sys.stdout.write('re:'+l); sys.stdout.flush()")


def ask(path, line):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(path))
    data = b""
    try:
        s.sendall(line)
        s.shutdown(socket.SHUT_WR)
        while chunk := s.recv(4096):
            data += chunk
    except OSError:
        pass  # the broker hung up on us (reset or broken pipe): same as an empty answer
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


def _connect(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(path))
    return s


def _drain(s):
    data = b""
    while chunk := s.recv(4096):
        data += chunk
    return data


def test_a_peer_with_another_uid_is_dropped_before_a_child_starts(sockdir):
    b = Broker(sockdir / "h.sock", ECHO, allowed_uid=os.getuid() + 1)
    b.start()
    try:
        assert ask(b.socket_path, b"hi\n") == b""
    finally:
        b.stop()


def test_a_frame_over_the_cap_closes_the_connection_without_reaching_the_child(sockdir):
    b = Broker(sockdir / "h.sock", ECHO, max_frame_bytes=100)
    b.start()
    try:
        assert ask(b.socket_path, b"x" * 500 + b"\n") == b""
        assert ask(b.socket_path, b"y" * 500) == b""  # no newline yet: the buffer cap still applies
        assert ask(b.socket_path, b"ok\n") == b"re:ok\n"
    finally:
        b.stop()


def test_the_default_frame_cap_is_four_mebibytes():
    from halsandbox.broker import MAX_FRAME_BYTES

    assert MAX_FRAME_BYTES == 4 * 1024 * 1024


def test_a_stalled_partial_frame_is_closed_after_the_timeout(sockdir):
    b = Broker(sockdir / "h.sock", ECHO, partial_timeout_s=0.3)
    b.start()
    try:
        s = _connect(b.socket_path)
        s.sendall(b"half")
        assert _drain(s) == b""  # the broker hangs up; we did not
        s.close()
    finally:
        b.stop()


def test_a_frame_sent_in_two_pieces_within_the_timeout_is_delivered_whole(sockdir):
    b = Broker(sockdir / "h.sock", ECHO, partial_timeout_s=5)
    b.start()
    try:
        s = _connect(b.socket_path)
        s.sendall(b"he")
        time.sleep(0.2)
        s.sendall(b"llo\n")
        s.shutdown(socket.SHUT_WR)
        assert _drain(s) == b"re:hello\n"
        s.close()
    finally:
        b.stop()


def test_an_idle_connection_with_nothing_pending_is_not_timed_out(sockdir):
    b = Broker(sockdir / "h.sock", ECHO, partial_timeout_s=0.2)
    b.start()
    try:
        s = _connect(b.socket_path)
        time.sleep(0.5)
        s.sendall(b"late\n")
        s.shutdown(socket.SHUT_WR)
        assert _drain(s) == b"re:late\n"
        s.close()
    finally:
        b.stop()


def test_an_unterminated_last_frame_is_never_forwarded(sockdir):
    b = Broker(sockdir / "h.sock", ECHO)
    b.start()
    try:
        assert ask(b.socket_path, b"whole\npartial") == b"re:whole\n"
    finally:
        b.stop()


def test_connections_over_the_limit_are_dropped(sockdir):
    b = Broker(sockdir / "h.sock", ECHO, max_connections=1)
    b.start()
    try:
        first = _connect(b.socket_path)
        first.sendall(b"a\n")
        assert first.recv(100) == b"re:a\n"  # first is now being served
        assert ask(b.socket_path, b"b\n") == b""
        first.close()
    finally:
        b.stop()


FEED_ECHO = (
    sys.executable,
    "-c",
    "import os,sys\n"
    "f=os.fdopen(int(os.environ['HAL_HUMAN_FD']),'rb')\n"
    "sys.stdin.readline()\n"
    "sys.stdout.write('feed:'+f.readline().decode()); sys.stdout.flush()",
)


def test_human_message_reaches_a_live_child_on_the_private_pipe(sockdir):
    b = Broker(sockdir / "h.sock", FEED_ECHO)
    b.start()
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(str(b.socket_path))
        time.sleep(0.3)  # let the child start and register its pipe
        b.on_human_message("hello", "tui")
        s.sendall(b"go\n")
        data = s.recv(4096)
        s.close()
        assert data == b'feed:{"text": "hello", "surface": "tui"}\n'
    finally:
        b.stop()


def test_last_human_message_is_replayed_to_a_later_connection(sockdir):
    b = Broker(sockdir / "h.sock", FEED_ECHO)
    b.start()
    try:
        b.on_human_message("earlier", "acp")
        assert b"earlier" in ask(b.socket_path, b"go\n")
    finally:
        b.stop()


def test_unknown_surface_is_refused(sockdir):
    import pytest

    b = Broker(sockdir / "h.sock", ECHO)
    try:
        with pytest.raises(ValueError, match="unknown surface"):
            b.on_human_message("x", "carrier-pigeon")
    finally:
        b.stop()
