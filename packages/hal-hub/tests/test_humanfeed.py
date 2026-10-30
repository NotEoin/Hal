"""Tests for the human-message feed that carries the broker's `on_human_message` into a hub child."""

import os
import time

import pytest

from halhub import humanfeed


class FakeSession:
    def __init__(self):
        self.calls = []

    def human_message(self, text, *, surface):
        if surface == "bogus":
            raise ValueError("unknown surface")
        self.calls.append((text, surface))


def test_a_valid_line_reaches_the_session():
    s = FakeSession()
    assert humanfeed.apply_line(s, humanfeed.encode("hi", "tui"))
    assert s.calls == [("hi", "tui")]


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b'{"text": 1, "surface": "tui"}\n',
        b'{"text": "x"}\n',
        b'{"text":"x","surface":"bogus"}\n',
    ],
)
def test_a_malformed_line_is_dropped_and_reported(line):
    s = FakeSession()
    assert not humanfeed.apply_line(s, line)
    assert s.calls == []


def test_feed_fd_reads_the_environment_and_rejects_junk():
    assert humanfeed.feed_fd({}) is None
    assert humanfeed.feed_fd({humanfeed.ENV_FD: "7"}) == 7
    with pytest.raises(ValueError, match="descriptor"):
        humanfeed.feed_fd({humanfeed.ENV_FD: "x"})


def test_serve_applies_lines_and_skips_an_overlong_one():
    s = FakeSession()
    r, w = os.pipe()
    humanfeed.serve(s, r)
    os.write(w, b"x" * (humanfeed.MAX_LINE_BYTES + 10) + b"\n")
    os.write(w, humanfeed.encode("after", "acp"))
    os.close(w)
    deadline = time.monotonic() + 5
    while not s.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert s.calls == [("after", "acp")]
