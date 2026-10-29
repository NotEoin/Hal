"""Tests for `halkit.netguard`: URL rules (REQ-SEC-10), `canonical_url` and the guarded fetch (REQ-SEC-11).

Every server is local (127.0.0.1) and every name resolves through `FakeResolver`, so nothing
here touches the network. The TLS case makes a throwaway certificate with the `openssl`
command and is skipped where it is missing.
"""

from __future__ import annotations

import shutil
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from halkit.netguard import BlockedURL, canonical_url, check_url, fetch
from halkit.proc import run
from halkit.testing import FakeResolver

OPENSSL_TIMEOUT_S = 30  # key generation takes well under a second; this only bounds a hang
STRICT = {"domains": (), "allow_http": False, "allow_ip_literals": False, "allow_private": False}


@pytest.mark.parametrize(
    ("url", "rule"),
    [
        ("file:///etc/passwd", "url:scheme"),
        ("ftp://example.org/x", "url:scheme"),
        ("http://example.org/", "url:scheme"),
        ("https://u:p@example.org/", "url:userinfo"),
        ("https://example.org:99999/", "url:host"),
        ("https:///path", "url:host"),
        ("https://127.0.0.1/", "url:ip-literal"),
        ("https://2130706433/", "url:ip-literal"),
        ("https://0177.0.0.1/", "url:ip-literal"),
        ("https://0x7f.1/", "url:ip-literal"),
        ("https://127.1/", "url:ip-literal"),
        ("https://[::ffff:127.0.0.1]/", "url:ip-literal"),
        ("https://[::1]/", "url:ip-literal"),
        ("https://localhost/", "url:private"),
        ("https://localhost./", "url:private"),
        ("https://printer.local/", "url:private"),
        ("https://api.localhost/", "url:private"),
        ("https://svc.internal/", "url:private"),
        ("https://nas.home.arpa/", "url:private"),
        ("https://intranet/", "url:private"),
    ],
)
def test_strict_rules_refuse_each_dangerous_url_form(url, rule):
    assert check_url(url, **STRICT) == (rule, False)


def test_public_https_url_passes_and_reports_whether_it_is_listed():
    assert check_url("https://docs.python.org/3/", **STRICT) == (None, False)
    listed = {**STRICT, "domains": ["docs.python.org"]}
    assert check_url("https://Docs.Python.org/3/", **listed) == (None, True)


def test_wildcard_domain_matches_subdomains_but_not_the_apex():
    rules = {**STRICT, "domains": ["*.example.com"]}
    assert check_url("https://a.example.com/", **rules) == (None, True)
    assert check_url("https://example.com/", **rules) == (None, False)
    assert check_url("https://badexample.com/", **rules) == (None, False)


def test_settings_relax_only_the_rule_they_name():
    assert check_url("http://example.org/", **{**STRICT, "allow_http": True}) == (None, False)
    literals = {**STRICT, "allow_ip_literals": True}
    assert check_url("https://93.184.215.14/", **literals) == (None, False)
    assert check_url("https://[::ffff:10.0.0.5]/", **literals) == ("url:private", False)
    everything = {**literals, "allow_private": True}
    assert check_url("https://10.0.0.5/", **everything) == (None, False)


def test_unicode_hostnames_are_compared_in_idna_form():
    host = "b" + chr(0xFC) + "cher.example"
    rules = {**STRICT, "domains": ["xn--bcher-kva.example"]}
    assert check_url(f"https://{host}/", **rules) == (None, True)


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("HTTPS://Example.ORG:443/A?b=C#frag", "https://example.org/A?b=C"),
        ("https://example.org:8443/x", "https://example.org:8443/x"),
        ("http://example.org:80/%7Ex", "http://example.org/%7Ex"),
        ("https://b" + chr(0xFC) + "cher.example/", "https://xn--bcher-kva.example/"),
        ("https://[::1]:443/", "https://[::1]/"),
    ],
)
def test_canonical_url_normalises_scheme_host_port_and_fragment_only(url, canonical):
    assert canonical_url(url) == canonical


def test_canonical_url_keeps_path_and_query_byte_for_byte():
    assert canonical_url("https://e.org/a%2Fb?x=1&y=2") != canonical_url("https://e.org/a/b?x=1&y=2")


# ---- fetch ---------------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
    seen: list[tuple[str, str, dict[str, str]]] = []

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).seen.append((self.command, self.path, dict(self.headers)))
        status, headers, body = type(self).routes.get(self.path, (404, {}, b"missing"))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *args: object) -> None:
        """Keeps test output quiet."""


@pytest.fixture
def server() -> Iterator[HTTPServer]:
    _Handler.routes = {}
    _Handler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


LOCAL = {"allow_http": True, "allow_private": True, "timeout": 5, "max_bytes": 1000}


def test_fetch_connects_to_the_resolved_address_with_the_name_as_host(server):
    port = server.server_address[1]
    _Handler.routes["/hello"] = (200, {"X-Test": "1"}, b"hi")
    resolver = FakeResolver({"good.example": ["127.0.0.1"]})
    response = fetch(f"http://good.example:{port}/hello", domains=(), resolver=resolver, **LOCAL)
    assert (response.status, response.body, response.truncated) == (200, b"hi", False)
    assert response.headers["x-test"] == "1"
    assert _Handler.seen[0][2]["Host"] == f"good.example:{port}"
    assert resolver.lookups == ["good.example"]


def test_fetch_refuses_a_bad_url_before_any_dns_lookup():
    resolver = FakeResolver({})
    with pytest.raises(BlockedURL) as info:
        fetch("file:///etc/passwd", domains=(), resolver=resolver, timeout=5, max_bytes=10)
    assert info.value.rule == "url:scheme" and resolver.lookups == []


def test_fetch_refuses_a_public_name_that_resolves_to_a_private_address():
    resolver = FakeResolver({"evil.example": ["10.0.0.5"]})
    with pytest.raises(BlockedURL) as info:
        fetch("https://evil.example/", domains=(), resolver=resolver, timeout=5, max_bytes=10)
    assert info.value.rule == "url:private"


def test_fetch_refuses_when_any_resolved_address_is_private():
    resolver = FakeResolver({"mixed.example": ["93.184.215.14", "127.0.0.1"]})
    with pytest.raises(BlockedURL, match="non-global"):
        fetch("https://mixed.example/", domains=(), resolver=resolver, timeout=5, max_bytes=10)


def test_redirect_to_a_metadata_address_is_stopped_before_connecting(server):
    port = server.server_address[1]
    _Handler.routes["/r"] = (302, {"Location": "http://169.254.169.254/latest/"}, b"")
    resolver = FakeResolver({"good.example": ["127.0.0.1"]})
    with pytest.raises(BlockedURL) as info:
        fetch(f"http://good.example:{port}/r", domains=(), resolver=resolver, **LOCAL)
    assert info.value.rule == "url:ip-literal" and len(_Handler.seen) == 1


def test_redirect_to_an_unlisted_host_is_refused_when_domains_are_set(server):
    port = server.server_address[1]
    _Handler.routes["/r"] = (301, {"Location": f"http://other.example:{port}/x"}, b"")
    resolver = FakeResolver({"good.example": ["127.0.0.1"], "other.example": ["127.0.0.1"]})
    with pytest.raises(BlockedURL) as info:
        fetch(f"http://good.example:{port}/r", domains=["good.example"], resolver=resolver, **LOCAL)
    assert info.value.rule == "url:unlisted"


def test_redirects_stop_after_the_limit(server):
    port = server.server_address[1]
    _Handler.routes["/loop"] = (302, {"Location": "/loop"}, b"")
    resolver = FakeResolver({"good.example": ["127.0.0.1"]})
    with pytest.raises(BlockedURL) as info:
        fetch(f"http://good.example:{port}/loop", domains=(), resolver=resolver, max_redirects=2, **LOCAL)
    assert info.value.rule == "url:redirects" and len(_Handler.seen) == 3


def test_cross_host_redirect_drops_credentials_and_303_turns_post_into_get(server):
    port = server.server_address[1]
    _Handler.routes["/post"] = (303, {"Location": f"http://other.example:{port}/done"}, b"")
    _Handler.routes["/done"] = (200, {}, b"ok")
    resolver = FakeResolver({"good.example": ["127.0.0.1"], "other.example": ["127.0.0.1"]})
    response = fetch(
        f"http://good.example:{port}/post",
        domains=(),
        resolver=resolver,
        method="POST",
        body=b"x=1",
        headers={"Authorization": "Bearer synthetic", "X-Keep": "yes"},
        **LOCAL,
    )
    assert response.body == b"ok"
    (_, _, first), (method, _, second) = _Handler.seen
    assert "Authorization" in first and "Authorization" not in second and second["X-Keep"] == "yes"
    assert method == "GET"


def test_body_over_max_bytes_is_cut_and_marked(server):
    port = server.server_address[1]
    _Handler.routes["/big"] = (200, {}, b"a" * 50)
    resolver = FakeResolver({"good.example": ["127.0.0.1"]})
    response = fetch(
        f"http://good.example:{port}/big", domains=(), resolver=resolver, **{**LOCAL, "max_bytes": 10}
    )
    assert response.body == b"a" * 10 and response.truncated


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[HTTPServer, Path]]:
    if shutil.which("openssl") is None:
        pytest.skip("needs the openssl command to make a test certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=good.example", "-addext", "subjectAltName=DNS:good.example",
        ],
        timeout=OPENSSL_TIMEOUT_S,
    )  # fmt: skip
    _Handler.routes = {"/tls": (200, {}, b"secure")}
    _Handler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    srv.socket = server_ctx.wrap_socket(srv.socket, server_side=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, cert
    srv.shutdown()
    srv.server_close()


def test_tls_fetch_uses_the_name_for_sni_and_certificate_checks(tls_server):
    srv, cert = tls_server
    port = srv.server_address[1]
    ctx = ssl.create_default_context(cafile=str(cert))
    resolver = FakeResolver({"good.example": ["127.0.0.1"], "other.example": ["127.0.0.1"]})
    common = {"domains": (), "resolver": resolver, "allow_private": True, "timeout": 5, "max_bytes": 100}
    assert fetch(f"https://good.example:{port}/tls", context=ctx, **common).body == b"secure"
    with pytest.raises(ssl.SSLCertVerificationError):
        fetch(f"https://other.example:{port}/tls", context=ctx, **common)
