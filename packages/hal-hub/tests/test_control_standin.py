"""Tests for `halhub._control_standin`: the approval control socket (01 REQ-SEC-26, RESOLUTIONS B3).

Real unix sockets in the temporary runtime directory; peer credentials and the harness check
are injected where a test needs another uid or a harness peer.
"""

from __future__ import annotations

import os
import socket
import stat
import threading
import time
from datetime import timedelta

import pytest

from halhub import _control_standin as control
from halhub._approvals_standin import ApprovalRequest
from halhub._policy_standin import Origin, Scope, Taint, Tier
from halkit.timeutil import iso_z, utc_now

SESSION = "0123456789ab"


def request(*, expires_in: float = 120, scopes=(Scope.ONCE, Scope.SESSION)) -> ApprovalRequest:
    now = utc_now()
    return ApprovalRequest(
        id="apr_0123456789abcdef",
        session=SESSION,
        origin=Origin.CLI,
        channel=None,
        pack="demo",
        pack_version="0",
        tool="demo_wipe",
        tier=Tier.ACT,
        key="sha256:" + "a" * 64,
        reason="tier:act - act tools ask",
        args={"path": "/tmp/x"},
        display="demo_wipe  [act]  demo\n  path: /tmp/x",
        summary="Allow demo_wipe (act, pack demo)?",
        content=False,
        challenge=None,
        scopes=tuple(scopes),
        taint=Taint.UNTRUSTED,
        guard=None,
        created_at=iso_z(now, ms=True),
        expires_at=iso_z(now + timedelta(seconds=expires_in), ms=True),
    )


def own_peer(conn: socket.socket) -> control.Peer:
    return control.Peer(os.getpid(), os.getuid(), os.getgid())


@pytest.fixture
def server():
    records: list[tuple[str, dict]] = []
    srv = control.ControlServer(
        SESSION,
        audit=lambda event, **f: records.append((event, f)),
        peer=own_peer,
        in_harness=lambda pid: False,
    )
    srv.records = records
    yield srv
    srv.close()


def pending_soon(srv: control.ControlServer, req: ApprovalRequest, timeout_s: float = 5) -> dict:
    """Starts `srv.decide(req)` on a thread and returns a box that receives its answer."""
    box: dict = {}
    thread = threading.Thread(
        target=lambda: box.setdefault("answer", srv.decide(req, timeout_s)), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not control.list_pending() and time.monotonic() < deadline:
        time.sleep(0.02)
    box["thread"] = thread
    return box


def test_socket_is_private_and_listed_request_is_01s_json(server):
    box = pending_soon(server, request())
    listed = control.list_pending()
    assert [r["id"] for r in listed] == ["apr_0123456789abcdef"]
    assert listed[0]["type"] == "approval_request" and listed[0]["scopes"] == ["once", "session"]
    assert listed[0]["taint"] == "untrusted" and listed[0]["origin"] == "cli"
    assert stat.S_IMODE(server.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(server.path.parent.stat().st_mode) == 0o700
    server.withdraw("apr_0123456789abcdef")
    box["thread"].join(2)


def test_answer_allow_once_reaches_the_waiting_call(server):
    box = pending_soon(server, request())
    control.answer(control.list_pending()[0], "allow", scope="once", note="fine")
    box["thread"].join(2)
    decision = box["answer"]
    assert (decision.verdict, decision.scope, decision.source, decision.note) == (
        "allow",
        Scope.ONCE,
        "control",
        "fine",
    )
    assert control.list_pending() == []


def test_wrong_key_scope_or_id_is_refused(server):
    box = pending_soon(server, request(scopes=(Scope.ONCE,)))
    listed = control.list_pending()[0]
    with pytest.raises(control.ControlError) as info:
        control.answer({**listed, "key": "sha256:" + "b" * 64}, "allow", scope="once")
    assert info.value.code == "key_mismatch" and server.records[-1][0] == "approval_mismatch"
    with pytest.raises(control.ControlError) as info:
        control.answer(listed, "allow", scope="session")
    assert info.value.code == "bad_scope"
    with pytest.raises(control.ControlError) as info:
        control.answer({**listed, "id": "apr_ffffffffffffffff"}, "deny")
    assert info.value.code == "unknown_id"
    control.answer(listed, "deny")
    box["thread"].join(2)
    assert box["answer"].verdict == "deny"


def test_expired_request_cannot_be_answered(server):
    box = pending_soon(server, request(expires_in=-1), timeout_s=1)
    with pytest.raises(control.ControlError) as info:
        control.answer(control.list_pending()[0], "allow", scope="once")
    assert info.value.code == "expired"
    box["thread"].join(2)
    assert box["answer"].source == "timeout"


def test_decide_from_a_harness_peer_is_refused_and_audited():
    records: list[tuple[str, dict]] = []
    srv = control.ControlServer(
        SESSION, audit=lambda e, **f: records.append((e, f)), peer=own_peer, in_harness=lambda pid: True
    )
    try:
        box = pending_soon(srv, request(), timeout_s=1)
        with pytest.raises(control.ControlError) as info:
            control.answer(control.list_pending()[0], "allow", scope="once")
        assert info.value.code == "harness" and records[-1][0] == "approval_refused"
        box["thread"].join(2)
        assert box["answer"].verdict == "deny"
    finally:
        srv.close()


def test_peer_with_another_uid_is_closed_before_reading():
    other = control.ControlServer(
        SESSION, peer=lambda conn: control.Peer(1, os.getuid() + 1, 0), in_harness=lambda pid: False
    )
    try:
        box = pending_soon_quiet(other)
        assert control.list_pending() == []  # the listing connection was closed unanswered
        other.withdraw("apr_0123456789abcdef")
        box.join(2)
    finally:
        other.close()


def pending_soon_quiet(srv: control.ControlServer) -> threading.Thread:
    thread = threading.Thread(target=srv.decide, args=(request(), 5), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while srv.path is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return thread


def test_stale_socket_is_removed_by_the_listing(hal_home):
    path = control.control_dir() / "ffffffffffff.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # bound but never listening: what a crashed process leaves
    assert control.list_pending() == [] and not path.exists()


def test_close_removes_the_socket_and_denies_pending(server):
    box = pending_soon(server, request())
    path = server.path
    server.close()
    box["thread"].join(2)
    assert box["answer"].verdict == "deny" and not path.exists() and not server.available()


def test_peer_whose_environment_cannot_be_read_counts_as_a_harness():
    assert control.peer_in_harness(2**22 + 12345)  # above Linux's largest pid_max: no such process


def test_peer_in_harness_reads_hal_harness_from_proc(monkeypatch, tmp_path):
    environ = {"set": b"PATH=/bin\0HAL_HARNESS=claude\0", "empty": b"HAL_HARNESS=\0", "unset": b"PATH=/bin\0"}
    real_read = control.Path.read_bytes
    monkeypatch.setattr(
        control.Path,
        "read_bytes",
        lambda self: environ[self.parts[2]] if self.parts[1] == "proc" else real_read(self),
    )
    assert control.peer_in_harness("set") and not control.peer_in_harness("empty")
    assert not control.peer_in_harness("unset")
