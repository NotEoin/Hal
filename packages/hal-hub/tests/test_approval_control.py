"""Tests for the approval control socket (`halhub.approvals.control`, REQ-SEC-26, ATK-17).

Real unix sockets in the temporary runtime directory; peer credentials and the peer's
environment come from `FakePeer`.
"""

from __future__ import annotations

import socket
import stat
import threading
import time
from datetime import timedelta

import pytest
from hubkit import FakePeer

from halhub.approvals import ApprovalRequest, control
from halhub.policy.model import Origin, Scope, Taint, Tier
from halkit.timeutil import iso_z, utc_now

SESSION = "0123456789ab"
RID = "apr_0123456789abcdef"


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


def request(
    *, rid: str = RID, expires_in: float = 120, scopes=(Scope.ONCE, Scope.SESSION)
) -> ApprovalRequest:
    now = utc_now()
    return ApprovalRequest(
        id=rid,
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


def make_server(fake: FakePeer | None = None) -> control.ControlServer:
    fake = fake or FakePeer()
    srv = control.ControlServer(SESSION, audit=Sink(), peer=fake.peer, environ=fake.environ)
    return srv


@pytest.fixture
def server():
    srv = make_server()
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
    while (srv.path is None or not srv._pending) and time.monotonic() < deadline:
        time.sleep(0.02)
    box["thread"] = thread
    return box


def test_socket_is_private_and_listed_request_is_the_wire_json(server):
    box = pending_soon(server, request())
    listed = control.list_pending()
    assert [r["id"] for r in listed] == [RID]
    assert listed[0]["type"] == "approval_request" and listed[0]["scopes"] == ["once", "session"]
    assert stat.S_IMODE(server.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(server.path.parent.stat().st_mode) == 0o700
    assert "/s/" not in str(server.path)
    server.withdraw(RID)
    box["thread"].join(2)


def test_answer_allow_once_reaches_the_waiting_call_with_control_as_source(server):
    box = pending_soon(server, request())
    control.answer({**control.list_pending()[0], "source": "tui"}, "allow", scope="once", note="fine")
    box["thread"].join(2)
    d = box["answer"]
    assert (d.verdict, d.scope, d.source, d.note) == ("allow", Scope.ONCE, "control", "fine")
    assert control.list_pending() == []


def test_wrong_key_scope_or_id_is_refused(server):
    box = pending_soon(server, request(scopes=(Scope.ONCE,)))
    listed = control.list_pending()[0]
    with pytest.raises(control.ControlError) as info:
        control.answer({**listed, "key": "sha256:" + "b" * 64}, "allow", scope="once")
    assert info.value.code == "key_mismatch" and server._audit.events[-1][0] == "approval_mismatch"
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


@pytest.mark.parametrize("environ", [b"PATH=/bin\0HAL_HARNESS=claude\0", None], ids=["harness", "unreadable"])
def test_decide_from_a_harness_peer_is_refused_and_audited(environ):
    srv = make_server(FakePeer(environ=environ))
    try:
        box = pending_soon(srv, request(), timeout_s=1)
        with pytest.raises(control.ControlError) as info:
            control.answer(control.list_pending()[0], "allow", scope="once")
        assert info.value.code == "harness" and srv._audit.events[-1][0] == "approval_refused"
        box["thread"].join(2)
        assert box["answer"].verdict == "deny"
    finally:
        srv.close()


def test_empty_hal_harness_does_not_count():
    assert not control.environ_has_harness(b"HAL_HARNESS=\0PATH=/bin\0")
    assert control.environ_has_harness(b"PATH=/bin\0HAL_HARNESS=1\0")


def test_peer_with_another_uid_is_closed_before_reading_atk_17():
    srv = make_server(FakePeer(uid=12345))
    try:
        box = pending_soon(srv, request())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(2)
            conn.connect(str(srv.path))
            assert conn.recv(1) == b""  # closed before reading or answering
        assert control.list_pending() == []
        srv.withdraw(RID)
        box["thread"].join(2)
    finally:
        srv.close()


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


def test_watch_pushes_pending_and_closed_events(server):
    box = pending_soon(server, request())
    events = control.watch(server.path, timeout=2)
    assert next(events)["id"] == RID
    second = request(rid="apr_fedcba9876543210")
    threading.Thread(target=server.decide, args=(second, 5), daemon=True).start()
    assert next(events)["id"] == second.id
    server.withdraw(RID)
    assert next(events) == {"type": "approval_closed", "id": RID}
    server.withdraw(second.id)
    events.close()
    box["thread"].join(2)


def test_unique_prefix_finds_one_request():
    pending = [{"id": "apr_0123456789abcdef"}, {"id": "apr_0123ff0000000000"}]
    assert control.find_request("012345", pending)["id"] == "apr_0123456789abcdef"
    assert control.find_request("apr_0123ff", pending)["id"] == "apr_0123ff0000000000"
    for ambiguous_or_short in ("0123", "apr_01234", "999999"):
        with pytest.raises(control.ControlError):
            control.find_request(ambiguous_or_short, pending)


def test_answer_refuses_a_session_that_is_not_a_session_id():
    with pytest.raises(control.ControlError):
        control.answer({"id": RID, "key": "k", "session": "../x"}, "deny")
