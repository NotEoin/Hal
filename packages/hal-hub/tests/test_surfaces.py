"""Tests for `halhub.surfaces`: the `tty` approval surface and the `--yes` rules (REQ-HUB-53, REQ-HUB-58)."""

from __future__ import annotations

import io
import os
import threading

import pytest

from halhub._approvals_standin import ApprovalRequest, ApprovalRouter
from halhub._policy_standin import Origin, Scope, Taint, Tier
from halhub.surfaces import HARNESS_ENV, TtySurface, in_harness, person_at_terminal, yes_allowed


def request(*, scopes=(Scope.ONCE, Scope.SESSION), content: bool = False) -> ApprovalRequest:
    return ApprovalRequest(
        id="apr_0123456789abcdef",
        session="0123456789ab",
        origin=Origin.CLI,
        channel=None,
        pack="demo",
        pack_version="0",
        tool="demo_wipe",
        tier=Tier.ACT,
        key="sha256:" + "0" * 64,
        reason="tier:act - act tools ask",
        args={"path": "/tmp/x"},
        display="demo_wipe  [act]  demo\n  path: /tmp/x",
        summary="Allow demo_wipe (act, pack demo)?",
        content=content,
        challenge=None,
        scopes=tuple(scopes),
        taint=Taint.UNTRUSTED,
        guard=None,
        created_at="2026-10-29T08:00:00.000Z",
        expires_at="2026-10-29T08:02:00.000Z",
    )


@pytest.fixture
def pipe():
    """A readable stream with a writer, standing in for the terminal's stdin."""
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "r")
    writer = os.fdopen(write_fd, "w")
    yield reader, writer
    reader.close()
    if not writer.closed:
        writer.close()


def surface(reader, prompt: io.StringIO, **kwargs) -> TtySurface:
    return TtySurface(stdin=reader, stdout=io.StringIO(), prompt=prompt, **kwargs)


def test_tty_surface_offers_once_only(pipe):
    reader, writer = pipe
    prompt = io.StringIO()
    session_only = surface(reader, prompt).decide(request(scopes=(Scope.SESSION,)), 1)
    assert session_only.verdict == "deny" and prompt.getvalue() == ""
    writer.write("y\n")
    writer.flush()
    answer = surface(reader, prompt).decide(request(), 2)
    assert (answer.verdict, answer.scope, answer.source) == ("allow", Scope.ONCE, "tty")
    assert "path: /tmp/x" in prompt.getvalue() and "[y/N]" in prompt.getvalue()


def test_anything_but_yes_is_a_deny(pipe):
    reader, writer = pipe
    writer.write("sure\n")
    writer.flush()
    assert surface(reader, io.StringIO()).decide(request(), 2).verdict == "deny"


def test_no_answer_in_time_is_a_timeout_deny(pipe):
    reader, _ = pipe
    answer = surface(reader, io.StringIO()).decide(request(), 0.3)
    assert (answer.verdict, answer.source) == ("deny", "timeout")


def test_withdraw_stops_a_waiting_prompt(pipe):
    reader, _ = pipe
    tty = surface(reader, io.StringIO())
    threading.Timer(0.1, tty.withdraw, args=("apr_0123456789abcdef",)).start()
    assert tty.decide(request(), 30).verdict == "deny"


def test_yes_answers_once_and_still_shows_the_request(pipe):
    reader, _ = pipe
    prompt = io.StringIO()
    answer = surface(reader, prompt, yes=True).decide(request(), 1)
    assert (answer.verdict, answer.scope) == ("allow", Scope.ONCE)
    assert "demo_wipe" in prompt.getvalue() and "--yes" in prompt.getvalue()


def test_tty_is_unavailable_without_a_terminal(pipe):
    reader, _ = pipe
    assert not surface(reader, io.StringIO()).available()
    assert not surface(reader, io.StringIO(), yes=True).available()


def test_yes_needs_a_terminal_and_no_harness():
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert yes_allowed(Terminal(), {})
    assert not yes_allowed(io.StringIO(), {})
    assert not yes_allowed(Terminal(), {HARNESS_ENV: "1"})
    assert in_harness({HARNESS_ENV: "opencode"}) and not in_harness({HARNESS_ENV: " "})


def test_person_at_terminal_needs_both_terminals_and_no_harness():
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert person_at_terminal(Terminal(), Terminal(), {})
    assert not person_at_terminal(Terminal(), io.StringIO(), {})
    assert not person_at_terminal(io.StringIO(), Terminal(), {})
    assert not person_at_terminal(Terminal(), Terminal(), {HARNESS_ENV: "1"})


def test_content_call_never_reaches_a_surface_without_all_args():
    class Notifier:
        name = "notify"
        shows_all_args = False
        asked: list[str] = []

        def available(self) -> bool:
            return True

        def decide(self, req, timeout_s):
            self.asked.append(req.id)
            raise AssertionError("a content call reached a surface that cannot show every argument")

        def withdraw(self, request_id: str) -> None:
            pass

    notifier = Notifier()
    answer = ApprovalRouter([notifier]).decide(request(content=True), 1)
    assert (answer.verdict, answer.source) == ("deny", "no-approver") and notifier.asked == []
