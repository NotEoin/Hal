"""Tests for `halcore.ide`: socket permissions, peer check, size cap, schema and path confinement."""

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from halcore.ide import protocol, server
from halcore.ide.protocol import IdeRefused, chip, confine, parse

SID = "0123456789ab"


@pytest.fixture
def env():
    with tempfile.TemporaryDirectory(prefix="hal-ide-") as tmp:
        ws = Path(tmp, "ws")
        ws.mkdir()
        (ws / "a.py").write_text("x = 1\n")
        Path(tmp, "secret.txt").write_text("s")
        (ws / "link").symlink_to(Path(tmp, "secret.txt"))
        run = Path(tmp, "run")
        yield ws, run


def talk(ws, run, lines, *, handler_log=None, read=True, patch=None):
    """Starts a server, sends raw `lines`, and returns (replies, handled messages)."""
    seen = []

    async def handler(msg):
        seen.append(msg)

    async def go():
        srv = server.IdeServer(SID, ws, handler, directory=run)
        sock = await srv.start()
        reader, writer = await asyncio.open_unix_connection(str(sock))
        for line in lines:
            writer.write(line)
        await writer.drain()
        replies = []
        while read:
            try:
                raw = await asyncio.wait_for(reader.readline(), 0.5)
            except (TimeoutError, ConnectionResetError):
                break
            if not raw:
                break
            replies.append(json.loads(raw))
        writer.close()
        await srv.close()
        return replies

    return asyncio.run(go()), seen


def j(**kw):
    return json.dumps(kw).encode() + b"\n"


def test_socket_is_private_and_lock_file_names_the_workspace(env):
    ws, run = env

    async def go():
        srv = server.IdeServer(SID, ws, None, directory=run)
        sock = await srv.start()
        mode = stat.S_IMODE(sock.stat().st_mode), stat.S_IMODE(run.stat().st_mode)
        found = server.find_session(ws, run)
        await srv.close()
        return mode, found, sock.exists()

    (smode, dmode), found, left = asyncio.run(go())
    assert (smode, dmode) == (0o600, 0o700)
    assert found["session"] == SID and found["workspace"] == os.path.realpath(ws)
    assert not left


def test_hello_is_answered_and_a_selection_reaches_the_handler(env):
    ws, run = env
    replies, seen = talk(ws, run, [j(type="hello", client="vscode"), j(type="add_selection", path=str(ws / "a.py"), start=3, end=5)])
    assert replies[0]["type"] == "welcome"
    assert seen[0].type == "add_selection" and seen[0].fields == {"path": "a.py", "start": 3, "end": 5}


def test_unknown_message_type_is_rejected_and_not_handled(env):
    ws, run = env
    replies, seen = talk(ws, run, [j(type="hello"), j(type="run_command", cmd="id")])
    assert replies[-1]["type"] == "error" and "unknown message type" in replies[-1]["message"]
    assert seen == []


def test_message_before_hello_is_rejected(env):
    ws, run = env
    replies, seen = talk(ws, run, [j(type="accept")])
    assert replies[0]["type"] == "error" and seen == []


def test_path_escape_is_rejected(env):
    ws, run = env
    for bad in ("../secret.txt", "/etc/passwd", "link"):
        replies, seen = talk(ws, run, [j(type="hello"), j(type="add_selection", path=bad, start=1, end=1)])
        assert replies[-1]["type"] == "error" and seen == [], bad


def test_oversized_message_closes_the_connection(env):
    ws, run = env
    big = b'{"type":"hello","client":"' + b"a" * (protocol.MAX_MESSAGE_BYTES + 10) + b'"}\n'
    replies, seen = talk(ws, run, [big])
    assert seen == [] and not any(r["type"] == "welcome" for r in replies)


def test_connection_from_another_uid_is_dropped(env, monkeypatch):
    ws, run = env
    monkeypatch.setattr(server, "peer_uid", lambda sock: os.getuid() + 1)
    replies, seen = talk(ws, run, [j(type="hello")])
    assert replies == [] and seen == []


def test_silent_client_is_dropped_after_the_hello_timeout(env, monkeypatch):
    ws, run = env
    monkeypatch.setattr(server, "HELLO_TIMEOUT_S", 0.1)

    async def go():
        srv = server.IdeServer(SID, ws, None, directory=run)
        sock = await srv.start()
        reader, writer = await asyncio.open_unix_connection(str(sock))
        data = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        await srv.close()
        return data

    assert asyncio.run(go()) == b""


def test_host_cannot_send_an_editor_type():
    with pytest.raises(IdeRefused):
        protocol.encode({"type": "accept"})


def test_diagnostics_are_validated_and_capped(env):
    ws, _ = env
    items = [{"line": 1, "severity": "error", "message": "m" * 900}] * 300
    msg = parse(j(type="diagnostics", path="a.py", items=items), ws)
    assert len(msg.fields["items"]) == protocol.MAX_DIAGNOSTICS
    assert len(msg.fields["items"][0]["message"]) == protocol.MAX_TEXT
    with pytest.raises(IdeRefused):
        parse(j(type="diagnostics", path="a.py", items=[{"line": 0, "severity": "error", "message": "x"}]), ws)


def test_context_allows_no_file_and_checks_the_range(env):
    ws, _ = env
    assert parse(j(type="context", path=None, selection=None), ws).fields["path"] is None
    with pytest.raises(IdeRefused):
        parse(j(type="context", path="a.py", selection={"start": 5, "end": 2}), ws)


def test_confine_gives_relative_posix_paths(env):
    ws, _ = env
    assert confine(ws, "sub/../a.py") == "a.py"


def test_chip_collapses_a_single_line():
    assert chip("a.py", 4, 4) == "@a.py#L4" and chip("a.py", 10, 20) == "@a.py#L10-20"
