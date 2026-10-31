"""Tests for the app helpers: welcome panel, summary line, notes, shell, notifications and the loop prefixes."""

import asyncio
import io
import os
import sys
import types

from halcore.tui import app, slash, theme
from halcore.tui.render import Renderer, TurnStats
from halcore.acp.transport import SubprocessTransport
from test_acp_client import FAKE_AGENT
from test_tui import FakeSession, NullClient

LOOK = theme.load_look({"unicode": False}, stream=io.StringIO(), environ={})


class Tty(io.StringIO):
    def isatty(self):
        return True


def shown(renderable):
    out = io.StringIO()
    theme.make_console(LOOK, out).print(renderable)
    return out.getvalue()


def test_welcome_panel_has_the_header_facts():
    st = slash.TuiState(NullClient(), "s", "/work", Renderer(io.StringIO(), LOOK))
    st.status.model, st.status.size, st.status.lockdown, st.status.paranoia = "qwen3-coder", 32768, "standard", "balanced"
    iso = types.SimpleNamespace(work="/state/isolated/abc/work")
    session = types.SimpleNamespace(runtime="runsc", backend="docker", project="/home/u/proj", iso=iso)
    text = shown(app.welcome(st, session, LOOK, "9.9.9"))
    for part in ("Hal", "9.9.9", "qwen3-coder", "32k context", "lockdown standard", "paranoia balanced", "box runsc", "/home/u/proj", "/state/isolated/abc/work"):
        assert part in text
    assert text.isascii()


def test_turn_line_lists_files_lines_tokens_and_time():
    s = TurnStats()
    s.files, s.added, s.removed, s.out_chars = {"a", "b"}, 10, 3, 400
    line = app.turn_line(s, "end_turn", "plan", unsandboxed=True)
    assert line.startswith("[end_turn] mode plan UNSANDBOXED | 2 files +10 -3 | ~100 tok |")


def test_notify_rings_the_bell_or_sends_osc9_only_on_a_terminal():
    for mode, expected in (("bell", "\a"), ("osc9", "\x1b]9;Hal: turn finished\x07"), ("off", "")):
        tty = Tty()
        app.notify(tty, mode)
        assert tty.getvalue() == expected
    plain = io.StringIO()
    app.notify(plain, "bell")
    assert plain.getvalue() == ""


def test_note_is_appended_to_agents_md_in_the_copy(tmp_path):
    assert "noted" in app.append_note(tmp_path, " use pytest -x \n and ruff ")
    assert (tmp_path / "AGENTS.md").read_text() == "- use pytest -x and ruff\n"


def test_note_does_not_follow_a_planted_symlink(tmp_path):
    outside = tmp_path / "host-file"
    outside.write_text("keep")
    work = tmp_path / "work"
    work.mkdir()
    os.symlink(outside, work / "AGENTS.md")
    assert "refused" in app.append_note(work, "x")
    assert outside.read_text() == "keep"


def test_note_without_a_copy_says_so():
    assert "no working copy" in app.append_note(None, "x")


def test_shell_needs_a_docker_box():
    ok, text = asyncio.run(app.shell_in_box(types.SimpleNamespace(backend="none", container=""), "ls", "/work"))
    assert not ok and "docker" in text


def test_shell_runs_through_docker_exec_with_a_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, *, timeout, **kw):
        seen.update(argv=argv, timeout=timeout)
        return types.SimpleNamespace(returncode=1, stdout=b"out", stderr=b"err")

    monkeypatch.setattr(app.proc, "run", fake_run)
    session = types.SimpleNamespace(backend="docker", container="box1", docker="docker")
    ok, text = asyncio.run(app.shell_in_box(session, "pytest -q", "/work"))
    assert not ok and text == "exit 1\nouterr"
    assert seen["argv"] == ["docker", "exec", "-w", "/work", "box1", "sh", "-c", "pytest -q"] and seen["timeout"] == app.SHELL_TIMEOUT_S


def test_loop_handles_note_shell_and_mention_prefixes(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    script = tmp_path / "agent.py"
    script.write_text(FAKE_AGENT)
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("print(1)\n")
    session = FakeSession()
    session.iso = types.SimpleNamespace(work=work)
    session.backend = "docker"
    session.container = "box1"
    session.docker = "docker"
    monkeypatch.setattr(app.proc, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=b"3 passed", stderr=b""))
    monkeypatch.setattr(app, "refresh_status", lambda state, s: asyncio.sleep(0))
    sent = []

    async def go():
        transport = await SubprocessTransport.spawn([sys.executable, str(script), "ok"], env=None, stderr_log=tmp_path / "e.log")
        box = types.SimpleNamespace(session=session, transport=transport, close=transport.close)
        lines = iter(["# prefer pytest", "!pytest -q", "fix @a.py please", "/quit"])

        async def read(prompt=""):
            return next(lines)

        out = io.StringIO()
        real = app.AcpClient.prompt

        async def spy(self, sid, blocks):
            sent.extend(blocks)
            return await real(self, sid, blocks)

        monkeypatch.setattr(app.AcpClient, "prompt", spy)
        await app.run_tui(box, cwd="/work", out=out, stdin_line=read, look=LOOK)
        return out.getvalue()

    text = asyncio.run(go())
    assert (work / "AGENTS.md").read_text() == "- prefer pytest\n"
    assert "3 passed" in text and "attached @a.py" in text
    bodies = [b["text"] for b in sent]
    assert any("3 passed" in b for b in bodies) and any('<file path="a.py">' in b for b in bodies)


def test_note_refuses_a_planted_fifo_without_blocking(tmp_path):
    os.mkfifo(tmp_path / "AGENTS.md")
    assert "refused" in app.append_note(tmp_path, "x")
