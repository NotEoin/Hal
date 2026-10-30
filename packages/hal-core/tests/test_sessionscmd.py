"""Tests for `halcore.sessionscmd` and the TUI /resume and /fork commands."""

import asyncio
import io
import json

import pytest

from halcore import sessionscmd
from halcore.acp.records import RecordStore
from halcore.tui import slash
from halcore.tui.render import Renderer


def seed(store, sid, project, text="fix the parser", extra=None):
    store.append(sid, {"type": "session", "v": 1, "id": sid})
    store.append(sid, {"type": "message", "i": 0, "msg": {"role": "system", "content": "sys"}})
    store.append(sid, {"type": "message", "i": 1, "msg": {"role": "user", "content": text}})
    for rec in extra or []:
        store.append(sid, rec)
    sessionscmd.write_meta(store, sid, project=str(project), model="m", mode="code", title=text)


@pytest.fixture
def store(tmp_path):
    return RecordStore(tmp_path / "sessions")


def test_list_shows_newest_first_and_filters_by_project(store, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    seed(store, "aaaaaaaaaaaa", a)
    seed(store, "bbbbbbbbbbbb", b)
    sessionscmd.write_meta(store, "aaaaaaaaaaaa", title="newer")
    assert [m["id"] for m in sessionscmd.list_sessions(store, a)] == ["aaaaaaaaaaaa"]
    assert len(sessionscmd.list_sessions(store)) == 2


def test_resume_refuses_a_session_whose_project_is_gone(store, tmp_path):
    seed(store, "cccccccccccc", tmp_path / "gone")
    with pytest.raises(sessionscmd.SessionsError, match="no longer exists"):
        sessionscmd.load_for_resume(store, "cccccccccccc")


def test_resume_refuses_a_session_of_another_project_and_a_bad_id(store, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    seed(store, "dddddddddddd", a)
    with pytest.raises(sessionscmd.SessionsError, match="belongs to"):
        sessionscmd.load_for_resume(store, "dddddddddddd", tmp_path)
    with pytest.raises(sessionscmd.SessionsError, match="not a session id"):
        sessionscmd.load_for_resume(store, "../etc")


def test_fork_copies_the_records_under_a_new_id_and_leaves_the_original(store, tmp_path):
    seed(store, "eeeeeeeeeeee", tmp_path)
    new = sessionscmd.fork(store, "eeeeeeeeeeee")
    assert new != "eeeeeeeeeeee" and len(store.load(new)) == len(store.load("eeeeeeeeeeee")) == 3
    assert sessionscmd.read_meta(store, new)["fork_of"] == "eeeeeeeeeeee"


def test_export_redacts_secrets_and_hides_tool_output_by_default(store, tmp_path):
    tool = {"type": "message", "i": 2, "msg": {"role": "tool", "content": "tool-body"}}
    seed(store, "ffffffffffff", tmp_path, text="use sk-abcdefghijklmnopqrstuvwxyz123456", extra=[tool])
    md = sessionscmd.export(store, "ffffffffffff")
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in md and "tool-body" not in md
    assert "tool-body" in sessionscmd.export(store, "ffffffffffff", tool_output=True)
    assert all(json.loads(line) for line in sessionscmd.export(store, "ffffffffffff", "jsonl").splitlines())


class LoadClient:
    def __init__(self):
        self.loaded = []

    async def load_session(self, sid, cwd, records):
        self.loaded.append((sid, len(records)))


def state_for(store, project, sid):
    state = slash.TuiState(LoadClient(), sid, "/work", Renderer(io.StringIO()), project=project)
    state.records = store
    return state


def test_resume_command_loads_the_stored_records_and_switches_session(store, tmp_path):
    seed(store, "111111111111", tmp_path)
    state = state_for(store, tmp_path, "222222222222")
    msg = asyncio.run(slash.dispatch("/resume 111111111111", state, {})).message
    assert state.sid == "111111111111" and state.client.loaded == [("111111111111", 3)] and "resumed" in msg


def test_resume_command_without_an_id_lists_this_projects_sessions(store, tmp_path):
    seed(store, "333333333333", tmp_path, text="fix the parser")
    msg = asyncio.run(slash.dispatch("/resume", state_for(store, tmp_path, "x"), {})).message
    assert "333333333333" in msg and "fix the parser" in msg


def test_fork_command_moves_to_a_copy_of_the_current_session(store, tmp_path):
    seed(store, "444444444444", tmp_path)
    state = state_for(store, tmp_path, "444444444444")
    asyncio.run(slash.dispatch("/fork", state, {}))
    assert state.sid != "444444444444" and state.client.loaded[0][0] == state.sid
