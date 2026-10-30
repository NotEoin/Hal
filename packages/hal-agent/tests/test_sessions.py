"""Tests for `halagent.sessions` and its use by the loop and the CLI."""

import json

import pytest

from halagent import cli
from halagent.loop import Agent
from halagent.sessions import (
    SessionError,
    SessionStore,
    SessionWriter,
    check_session_id,
    new_session_id,
    read_records,
    replay,
)
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.model_server import Reply

SID = "3f9a1c0b7e2d"


def header(sid=SID, cwd="/w"):
    return {
        "id": sid,
        "agent": "halagent test",
        "mode": "headless",
        "toolset": "coding",
        "cwd": cwd,
        "model": "m",
    }


def test_new_ids_are_twelve_lowercase_hex_characters():
    assert check_session_id(new_session_id())
    with pytest.raises(SessionError):
        check_session_id("../etc")


def test_writer_masks_secret_arguments_and_escapes_non_ascii(tmp_path):
    w = SessionWriter(tmp_path / SID / "transcript.jsonl", header())
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "run", "arguments": '{"api_key": "hunter2", "n": 1}'},
    }
    w.write(
        {
            "type": "message",
            "turn": 1,
            "i": 0,
            "msg": {"role": "assistant", "content": "caf\u00e9", "tool_calls": [call]},
        }
    )
    w.close()
    raw = (tmp_path / SID / "transcript.jsonl").read_text()
    assert "hunter2" not in raw and "***" in raw and raw.isascii()


def test_truncated_last_line_is_skipped_but_a_bad_middle_line_fails(tmp_path):
    path = tmp_path / "t.jsonl"
    good = json.dumps({"type": "notice", "turn": 1})
    path.write_text(f'{good}\n{{"type": "mess')
    assert len(read_records(path)) == 1
    path.write_text(f"{good}\nnot json\n{good}\n")
    with pytest.raises(SessionError, match=":2:"):
        read_records(path)


def test_replay_applies_mask_and_summary_compactions():
    recs = [
        {"type": "message", "i": 0, "msg": {"role": "system", "content": "s"}},
        {"type": "message", "i": 1, "msg": {"role": "user", "content": "u"}},
        {"type": "message", "i": 2, "msg": {"role": "tool", "content": "big"}},
        {"type": "message", "i": 3, "msg": {"role": "user", "content": "sum"}},
        {"type": "compaction", "kind": "mask", "masked": {"2": "[hidden]"}},
        {"type": "compaction", "kind": "summary", "replaces": [1, 2], "summary_i": 3},
    ]
    assert [m["content"] for m in replay(recs)] == ["s", "sum"]


def test_store_lists_newest_first_filters_by_cwd_and_exports(tmp_path):
    for sid, cwd, created in (
        ("aaaaaaaaaaaa", "/a", "2026-10-01T00:00:00Z"),
        ("bbbbbbbbbbbb", "/b", "2026-10-02T00:00:00Z"),
    ):
        w = SessionWriter(tmp_path / sid / "transcript.jsonl", header(sid, cwd))
        w.write(
            {"type": "message", "turn": 1, "i": 0, "msg": {"role": "user", "content": "hi"}, "ts": created}
        )
        w.close()
    store = SessionStore(tmp_path)
    assert [s.id for s in store.list()] == ["bbbbbbbbbbbb", "aaaaaaaaaaaa"]
    assert [s.id for s in store.list(cwd="/a")] == ["aaaaaaaaaaaa"]
    assert store.latest(cwd="/b").id == "bbbbbbbbbbbb"
    assert "## User\n\nhi" in store.export("aaaaaaaaaaaa", "md")
    assert json.loads(store.export("aaaaaaaaaaaa", "jsonl").splitlines()[1])["msg"]["content"] == "hi"


def test_agent_writes_a_transcript_and_resume_continues_it(server, tmp_path):
    path = tmp_path / SID / "transcript.jsonl"
    server.script(Reply(tool_calls=[("glob", {"pattern": "*"})]), "first", "second")
    a = Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path), session=SessionWriter(path, header()))
    a.run("one")
    a.session.close()
    kinds = [r["type"] for r in read_records(path)]
    assert kinds[0] == "session" and kinds[-1] == "turn_end" and kinds.count("message") == 5
    w = SessionWriter(path, header())
    b = Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path), session=w)
    b.resume(read_records(path))
    b.run("two")
    w.close()
    sent = server.requests[-1]["body"]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert [r["i"] for r in read_records(path) if r["type"] == "message"][-2:] == [5, 6]


def test_cli_continue_reuses_the_latest_session_and_export_prints_it(server, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    server.script("one", "two")
    base = ["-C", str(tmp_path), "--endpoint", server.url]
    assert cli.main(["-p", "first", *base]) == 0
    assert cli.main(["-p", "second", "--continue", *base]) == 0
    capsys.readouterr()
    assert cli.main(["-C", str(tmp_path), "sessions"]) == 0
    listed = capsys.readouterr().out.split()
    assert len(listed) // 4 == 1
    assert cli.main(["export", listed[0]]) == 0
    out = capsys.readouterr().out
    assert "first" in out and "second" in out
