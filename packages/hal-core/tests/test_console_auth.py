"""Tests for the console's access checks: token, Host, Origin, content type (REQ-SEC-71, ATK-08 to ATK-10)."""

import http.client
import stat
import threading

import pytest

from halcore import console
from halkit.testing import TempHalHome

TOKEN = "synthetic-console-token"


@pytest.fixture
def server():
    with TempHalHome():
        srv = console.make_server(0, TOKEN)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        yield srv
        srv.shutdown()
        srv.server_close()


def request(srv, method="GET", path="/", headers=None, body=None, host=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    sent = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    conn.request(method, path, body=body, headers=sent)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


def bearer():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_a_request_without_the_token_is_refused_with_401(server):
    assert request(server)[0].status == 401


def test_a_wrong_token_is_refused_with_401(server):
    assert request(server, headers={"Authorization": "Bearer nope"})[0].status == 401


def test_the_bearer_token_opens_the_page(server):
    response, data = request(server, headers=bearer())
    assert response.status == 200 and b"<html" in data.lower()


@pytest.mark.parametrize("host", ["evil.example", "evil.example:1", "127.0.0.1", "localhost:1"])
def test_a_foreign_host_header_is_refused_with_403_even_with_the_token(server, host):
    assert request(server, headers=bearer(), host=host)[0].status == 403


def test_localhost_host_header_is_accepted(server):
    port = server.server_address[1]
    assert request(server, headers=bearer(), host=f"localhost:{port}")[0].status == 200


def test_a_cross_site_origin_is_refused_with_403(server):
    headers = {**bearer(), "Origin": "https://evil.example"}
    assert request(server, "POST", "/api/nope", headers, b"{}")[0].status == 403


def test_a_same_origin_post_passes_the_checks(server):
    port = server.server_address[1]
    headers = {**bearer(), "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}
    assert request(server, "POST", "/api/nope", headers, b"{}")[0].status == 404


def test_a_post_that_is_not_json_is_refused_with_415(server):
    headers = {**bearer(), "Content-Type": "text/plain"}
    assert request(server, "POST", "/api/ask", headers, b"{}")[0].status == 415


def test_a_post_without_the_token_gets_401_before_anything_is_parsed(server):
    headers = {"Content-Type": "application/json"}
    assert request(server, "POST", "/api/ask", headers, b"{")[0].status == 401


def test_no_response_carries_cors_headers_and_every_one_has_a_csp(server):
    response, _ = request(server, headers=bearer())
    names = {k.lower() for k, _ in response.getheaders()}
    assert not any(n.startswith("access-control-") for n in names)
    assert "img-src 'self' data:" in response.getheader("Content-Security-Policy")


def test_the_open_link_sets_a_strict_http_only_cookie_once(server):
    code = server.auth.new_open_code()
    response, _ = request(server, path=f"/open?t={code}")
    cookie = response.getheader("Set-Cookie")
    assert response.status == 303 and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert code not in cookie  # the code is not the token
    again, _ = request(server, path=f"/open?t={code}")
    assert again.status == 401
    page, _ = request(server, headers={"Cookie": cookie.split(";")[0]})
    assert page.status == 200


def test_an_expired_open_code_is_refused(server):
    now = [0.0]
    auth = console.Auth(TOKEN, 1, clock=lambda: now[0])
    code = auth.new_open_code()
    now[0] += console.OPEN_CODE_TTL_S + 1
    assert auth.redeem(code) is False


def test_the_open_link_is_refused_for_a_foreign_host(server):
    code = server.auth.new_open_code()
    assert request(server, path=f"/open?t={code}", host="evil.example")[0].status == 403


def test_the_token_file_is_created_private_with_32_bytes_of_randomness():
    with TempHalHome():
        token = console.load_token()
        mode = stat.S_IMODE(console.token_file().stat().st_mode)
        assert mode == 0o600 and len(token) >= 43 and console.load_token() == token


def test_console_help_and_banner_say_it_is_unboxed(capsys, monkeypatch):
    # regression: the console ran on the host without saying so
    with pytest.raises(SystemExit):
        console.main(["--help"])
    assert "UNBOXED" in capsys.readouterr().out

    class Stop:
        server_address = ("127.0.0.1", 1)
        auth = console.Auth(TOKEN, 1)

        def serve_forever(self):
            raise KeyboardInterrupt

    with TempHalHome():
        monkeypatch.setattr(console, "make_server", lambda port: Stop())
        assert console.main([]) == 0
    assert "UNBOXED" in capsys.readouterr().out
    from halcore.cli import COMMANDS

    assert "UNBOXED" in COMMANDS["console"].help
