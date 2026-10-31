"""Tests for the console approval surface and its routes (REQ-CLI-29, REQ-CLI-109)."""

import http.client
import json
import threading

import pytest

from halcore import console
from halcore.consolesurface import ConsoleSurface, UnknownRequest
from halhub.approvals import ApprovalRequest
from halhub.policy.model import Origin, Scope, Taint, Tier
from halkit.testing import TempHalHome

TOKEN = "synthetic-console-token"


def make_request(scopes=(Scope.ONCE, Scope.SESSION)) -> ApprovalRequest:
    return ApprovalRequest(
        "apr_0123456789abcdef", "0123456789ab", next(iter(Origin)), None, "life", "1.0", "send_mail",
        next(iter(Tier)), "sha256:" + "ab" * 32, "send tier", {"to": "a@example.org"},
        "to: a@example.org", "send mail", False, None, tuple(scopes), next(iter(Taint)), None,
        "2026-10-31T00:00:00Z", "2026-10-31T00:05:00Z",
    )


def decide_in_thread(surface, request, timeout=5):
    out = []
    thread = threading.Thread(target=lambda: out.append(surface.decide(request, timeout)))
    thread.start()
    while not surface.pending():
        thread.join(0.01)
    return thread, out


def test_an_allow_with_every_argument_shown_returns_the_requests_key():
    surface, request = ConsoleSurface(), make_request()
    thread, out = decide_in_thread(surface, request)
    surface.answer(request.id, request.key, "allow", "once", shown_all=True)
    thread.join()
    assert (out[0].verdict, out[0].key, out[0].source) == ("allow", request.key, "console")


def test_an_allow_before_all_arguments_were_shown_is_refused():
    surface, request = ConsoleSurface(), make_request()
    thread, out = decide_in_thread(surface, request)
    with pytest.raises(ValueError, match="shown"):
        surface.answer(request.id, request.key, "allow", "once", shown_all=False)
    surface.answer(request.id, request.key, "deny")
    thread.join()
    assert out[0].verdict == "deny"


def test_an_answer_with_another_requests_key_is_refused():
    surface, request = ConsoleSurface(), make_request()
    thread, _ = decide_in_thread(surface, request)
    with pytest.raises(ValueError, match="key"):
        surface.answer(request.id, "sha256:" + "00" * 32, "deny")
    surface.withdraw(request.id)
    thread.join()


def test_a_scope_the_request_does_not_offer_is_refused():
    surface, request = ConsoleSurface(), make_request(scopes=(Scope.ONCE,))
    thread, _ = decide_in_thread(surface, request)
    with pytest.raises(ValueError, match="scope"):
        surface.answer(request.id, request.key, "allow", "session", shown_all=True)
    surface.withdraw(request.id)
    thread.join()


def test_an_unanswered_request_times_out_as_a_deny():
    decision = ConsoleSurface().decide(make_request(), 0.05)
    assert (decision.verdict, decision.source) == ("deny", "timeout")


def test_answering_an_unknown_request_raises():
    with pytest.raises(UnknownRequest):
        ConsoleSurface().answer("apr_missing", "k", "deny")


@pytest.fixture
def server():
    with TempHalHome():
        srv = console.make_server(0, TOKEN)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        yield srv
        srv.shutdown()
        srv.server_close()


def call(srv, method, path, body=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {TOKEN}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = conn.getresponse()
    data = json.loads(response.read())
    conn.close()
    return response.status, data


def test_the_page_lists_and_answers_a_pending_request_over_http(server):
    request = make_request()
    thread, out = decide_in_thread(server.approvals, request)
    status, listed = call(server, "GET", "/api/approvals")
    assert status == 200 and listed["approvals"][0]["id"] == request.id
    status, _ = call(server, "POST", f"/api/approvals/{request.id}",
                     {"key": request.key, "verdict": "allow", "scope": "once", "shown_all": True})
    thread.join()
    assert status == 200 and out[0].verdict == "allow"


def test_an_http_answer_for_an_unknown_request_gets_404(server):
    status, _ = call(server, "POST", "/api/approvals/apr_nope", {"key": "k", "verdict": "deny"})
    assert status == 404
