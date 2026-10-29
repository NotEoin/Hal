"""Tests for the model forwarder allowlist, using a fake upstream over a real unix socket."""

import http.client
import socket

from halsandbox.forwarder import MAX_BODY_BYTES, ModelForwarder


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self._path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def call(fw, method, path, body=None):
    c = UnixConn(fw.socket_path)
    c.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def make(sockdir, upstream):
    fw = ModelForwarder(sockdir / "m.sock", upstream.url)
    fw.start()
    return fw


def test_chat_completions_post_reaches_the_upstream(sockdir, upstream):
    fw = make(sockdir, upstream)
    try:
        status, _ = call(fw, "POST", "/v1/chat/completions", b'{"model":"m"}')
    finally:
        fw.stop()
    assert status == 200
    assert upstream.seen == [("POST", "/v1/chat/completions", b'{"model":"m"}')]


def test_models_get_reaches_the_upstream(sockdir, upstream):
    fw = make(sockdir, upstream)
    try:
        status, _ = call(fw, "GET", "/v1/models")
    finally:
        fw.stop()
    assert status == 200 and upstream.seen[0][:2] == ("GET", "/v1/models")


def test_other_routes_are_refused_and_never_forwarded(sockdir, upstream):
    fw = make(sockdir, upstream)
    try:
        codes = [
            call(fw, "GET", "/props")[0],
            call(fw, "POST", "/v1/completions", b"{}")[0],
            call(fw, "POST", "/v1/embeddings", b"{}")[0],
            call(fw, "GET", "/v1/models?x=1")[0],
            call(fw, "GET", "/v1/models/../props")[0],
            call(fw, "DELETE", "/api/delete")[0],
            call(fw, "POST", "/api/pull", b"{}")[0],
        ]
        wrong_method = [call(fw, "GET", "/v1/chat/completions")[0], call(fw, "PUT", "/v1/models")[0]]
    finally:
        fw.stop()
    assert codes == [404] * 7
    assert wrong_method == [404, 404] or wrong_method == [405, 405]
    assert upstream.seen == []


def test_oversized_body_is_refused(sockdir, upstream):
    fw = make(sockdir, upstream)
    try:
        c = UnixConn(fw.socket_path)
        c.putrequest("POST", "/v1/chat/completions")
        c.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        c.endheaders()
        status = c.getresponse().status
    finally:
        fw.stop()
    assert status == 413 and upstream.seen == []


def test_unreachable_upstream_gives_502(sockdir):
    fw = ModelForwarder(sockdir / "m.sock", "http://127.0.0.1:1")
    fw.start()
    try:
        status, _ = call(fw, "GET", "/v1/models")
    finally:
        fw.stop()
    assert status == 502


def test_socket_file_is_private_and_removed_on_stop(sockdir, upstream):
    fw = make(sockdir, upstream)
    mode = fw.socket_path.stat().st_mode & 0o777
    fw.stop()
    assert mode == 0o600 and not fw.socket_path.exists()
