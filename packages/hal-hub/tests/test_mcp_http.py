"""Tests for the hub's streamable HTTP transport (REQ-HUB-43 to REQ-HUB-45).

The guard, the host check and the token file are tested in-process. The cases marked `slow`
start `hal-hub mcp --transport http --port 0` as a subprocess on an ephemeral loopback port
and check each guard rule over real HTTP, plus one SDK client session with the token.
"""

from __future__ import annotations

import os
import select
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

import anyio
import pytest

from halhub import cli
from halhub._audit_standin import AuditLog, tail
from halhub._policy_standin import AuditSettings
from halhub.mcp import AUTH_FAIL_LIMIT, _Guard, check_http_host, load_token, rotate_token, token_path
from halkit.config import ConfigError

TOKEN = "test-token-not-a-secret"  # synthetic
PORT = 8742
START_TIMEOUT_S = 60  # a cold start imports the SDK and uvicorn; far above the usual 2 s
REQUEST_TIMEOUT_S = 10


class Inner:
    """Stands in for the SDK app: records that it was reached and answers 200."""

    def __init__(self) -> None:
        self.reached = 0

    async def __call__(self, scope, receive, send):
        self.reached += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def request(guard, headers: dict[str, str]) -> tuple[int, dict[bytes, bytes]]:
    """Sends one HTTP scope through `guard`; returns the status and response headers."""
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
    }
    anyio.run(guard, scope, receive, send)
    start = sent[0]
    return start["status"], dict(start["headers"])


@pytest.fixture
def guard():
    inner = Inner()
    audit = AuditLog(settings=AuditSettings())
    return _Guard(inner, port=PORT, token=TOKEN, allow_origins=["http://127.0.0.1:3000"], audit=audit), inner


GOOD = {"Host": f"127.0.0.1:{PORT}", "Authorization": f"Bearer {TOKEN}"}


def test_request_with_token_reaches_the_server(guard):
    g, inner = guard
    assert request(g, GOOD)[0] == 200 and inner.reached == 1
    assert request(g, {**GOOD, "Host": f"localhost:{PORT}"})[0] == 200
    assert request(g, {**GOOD, "Origin": "http://127.0.0.1:3000"})[0] == 200


def test_missing_or_wrong_token_is_401_with_bearer_challenge(guard):
    g, inner = guard
    for headers in ({"Host": GOOD["Host"]}, {**GOOD, "Authorization": "Bearer wrong"}):
        status, response = request(g, headers)
        assert status == 401 and response[b"www-authenticate"] == b"Bearer"
    assert inner.reached == 0


def test_foreign_origin_or_host_is_403(guard):
    g, inner = guard
    assert request(g, {**GOOD, "Origin": "http://localhost:3000"})[0] == 403
    assert request(g, {**GOOD, "Host": "evil.example"})[0] == 403
    assert request(g, {**GOOD, "Host": "127.0.0.1:9999"})[0] == 403
    assert inner.reached == 0


def test_refusals_are_audited_and_rate_limited(guard):
    g, _ = guard
    for _ in range(AUTH_FAIL_LIMIT + 5):
        request(g, {"Host": GOOD["Host"]})
    records = [r for r in tail(n=0) if r["event"] == "listener_auth_failed"]
    assert len(records) == AUTH_FAIL_LIMIT + 1
    assert {r["reason"] for r in records} == {"token"} and records[0]["listener"] == "hub-http"
    assert "summary" in records[-1] and TOKEN not in str(records)


def test_non_loopback_host_is_a_config_error(capsys):
    for host in ("0.0.0.0", "192.168.1.10", "localhost"):
        with pytest.raises(ConfigError):
            check_http_host(host)
    assert cli.main(["mcp", "--transport", "http", "--host", "0.0.0.0"]) == 2
    assert "127.0.0.1" in capsys.readouterr().err


def test_http_options_need_the_http_transport(capsys):
    assert cli.main(["mcp", "--port", "9000"]) == 2


def test_token_file_is_created_private_and_refused_when_others_can_read_it():
    token = load_token()
    path = token_path()
    assert len(token) >= 32 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_token() == token
    path.chmod(0o640)
    with pytest.raises(ConfigError, match="mode 0640"):
        load_token()


def test_rotate_writes_a_new_private_token():
    old = load_token()
    rotate_token()
    assert load_token() != old and stat.S_IMODE(token_path().stat().st_mode) == 0o600


def test_token_command_prints_the_path_not_the_token(capsys):
    assert cli.main(["token"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == str(token_path()) and load_token() not in out


# ---- a real listener (slow) -------------------------------------------------------------------


@pytest.fixture
def listener(hal_home, demo_token, fixtures):
    """`hal-hub mcp --transport http --port 0` over the demo pack; yields (url, port, token)."""
    with hal_home.config.open("a") as fh:
        fh.write(f'[hub]\nmodule_paths = ["{fixtures / "demo"}"]\nuse_entry_points = false\n')
    token = load_token()
    proc = subprocess.Popen(
        [sys.executable, "-m", "halhub", "mcp", "--transport", "http", "--port", "0"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    try:
        url = _wait_for_url(proc)
        port = int(url.rsplit(":", 1)[1].split("/", 1)[0])
        yield url, port, token
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=REQUEST_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=REQUEST_TIMEOUT_S)


def _wait_for_url(proc: subprocess.Popen) -> str:
    assert proc.stderr is not None
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stderr], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        line = proc.stderr.readline()
        if not line:
            break
        if "serving MCP on " in line:
            return line.split("serving MCP on ", 1)[1].split()[0]
    raise AssertionError(f"hal-hub mcp --transport http did not start (exit {proc.poll()})")


def _post(url: str, headers: dict[str, str]) -> int:
    body = (
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25",'
        b'"capabilities":{},"clientInfo":{"name":"t","version":"0"}}}'
    )
    base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    req = urllib.request.Request(url, data=body, headers={**base, **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.mark.slow
def test_listener_applies_each_guard_rule(listener):
    url, port, token = listener
    auth = {"Authorization": f"Bearer {token}"}
    assert _post(url, {}) == 401
    assert _post(url, {**auth, "Origin": "http://localhost:3000"}) == 403
    assert _post(url, {**auth, "Host": "evil.example"}) == 403
    assert _post(url, auth) == 200


@pytest.mark.slow
def test_sdk_client_calls_a_tool_over_http_with_the_token(listener):
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    url, _, token = listener

    async def main():
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
        async with http, Client(streamable_http_client(url, http_client=http), mode="legacy") as client:
            return await client.call_tool("hal_call", {"tool": "demo_echo", "arguments": {"text": "hi"}})

    result = anyio.run(main)
    assert result.content[0].text == "hi" and result.meta["hal"]["status"] == "ok"
