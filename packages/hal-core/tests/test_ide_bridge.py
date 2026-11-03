"""Tests for `halcore.ide.bridge` and the `/ide` command: editor input becomes prompt text, nothing else."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from halcore.ide.bridge import MAX_SELECTION_BYTES, IdeBridge
from halcore.ide.protocol import Message
from halcore.tui.slash import TuiState, dispatch


class FakeServer:
    connected = False
    sock_path = "/run/x.sock"

    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


@pytest.fixture
def parts():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "a.py").write_text("".join(f"line{i}\n" for i in range(1, 11)))
        says, server = [], FakeServer()
        bridge = IdeBridge(project, server, says.append, lambda: ["a.py"], lambda: Path("/copy"))
        yield bridge, says, server, project


def run(bridge, kind, **fields):
    asyncio.run(bridge.handle(Message(kind, fields)))


def test_selection_becomes_a_text_block_with_a_chip(parts):
    bridge, says, _, _ = parts
    run(bridge, "add_selection", path="a.py", start=2, end=3)
    blocks = bridge.blocks()
    assert "line2\nline3" in blocks[0]["text"] and "@a.py#L2-3" in blocks[0]["text"]
    assert says == ["ide: added @a.py#L2-3"] and bridge.blocks() == []


def test_selection_is_capped(parts):
    bridge, _, _, project = parts
    (project / "big.txt").write_text("x" * (MAX_SELECTION_BYTES * 3) + "\n")
    run(bridge, "add_selection", path="big.txt", start=1, end=1)
    assert len(bridge.blocks()[0]["text"]) < MAX_SELECTION_BYTES + 200


def test_selection_through_a_symlink_is_refused(parts):
    bridge, says, _, project = parts
    (project / "ln").symlink_to("/etc/hostname")
    run(bridge, "add_selection", path="ln", start=1, end=1)
    assert bridge.blocks() == [] and "cannot read" in says[0]


def test_ambient_context_is_off_until_enabled(parts):
    bridge, _, _, _ = parts
    run(bridge, "context", path="a.py", selection=(4, 6))
    assert bridge.blocks() == []
    bridge.ambient_on = True
    assert bridge.blocks()[0]["text"] == "Editor has open: @a.py#L4-6"
    assert bridge.blocks()[0]["text"].endswith("@a.py#L4-6")


def test_diagnostics_are_queued_for_the_next_prompt(parts):
    bridge, _, _, _ = parts
    run(bridge, "diagnostics", path="a.py", items=[{"line": 3, "severity": "error", "message": "bad"}])
    assert "a.py:3 error: bad" in bridge.blocks()[0]["text"]


def test_show_changes_answers_with_both_roots(parts):
    bridge, _, server, project = parts
    run(bridge, "show_changes")
    assert server.sent == [{"type": "changes", "project": str(project), "copy": "/copy", "files": ["a.py"]}]


def test_accept_request_only_prints_a_notice(parts):
    bridge, says, server, _ = parts
    run(bridge, "accept")
    assert bridge.accept_requested and "/accept" in says[0] and server.sent == []


def test_ide_command_toggles_ambient_sharing(parts):
    bridge, _, _, _ = parts
    state = TuiState(client=None, sid="x", cwd="/", renderer=SimpleNamespace(), ide=bridge)
    result = asyncio.run(dispatch("/ide on", state, {}))
    assert bridge.ambient_on and "ambient context on" in result.message
    assert "not open" in asyncio.run(dispatch("/ide", TuiState(client=None, sid="x", cwd="/", renderer=None), {})).message


def test_open_ide_needs_an_isolated_copy_and_registers_the_bridge(monkeypatch):
    from halcore.tui.controller import open_ide

    with tempfile.TemporaryDirectory(prefix="hal-ide-") as tmp:
        monkeypatch.setenv("HAL_RUNTIME_DIR", str(Path(tmp, "run")))
        project = Path(tmp, "p")
        project.mkdir()
        renderer = SimpleNamespace(info=lambda text: None)
        state = TuiState(client=None, sid="x", cwd="/", renderer=renderer)
        assert asyncio.run(open_ide(state, SimpleNamespace(iso=None), project, "0123456789ab", renderer)) is None

        async def go():
            server = await open_ide(state, SimpleNamespace(iso=SimpleNamespace(work=project)), project, "0123456789ab", renderer)
            ok = server.sock_path.exists()
            await server.close()
            return ok

        assert asyncio.run(go()) and state.ide is not None
