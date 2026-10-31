"""Tests for ACP `fs/*` served from the isolated copy and for diff content on edits."""

import os
from pathlib import Path

import pytest

from halcore.acp.hostfs import CopyFs
from halcore.acp.proxy import ProxyFilter


@pytest.fixture
def setup(tmp_path):
    project, work = tmp_path / "proj", tmp_path / "copy"
    project.mkdir()
    work.mkdir()
    (work / "a.py").write_text("one\ntwo\nthree\n")
    (tmp_path / "secret").write_text("host secret")
    (work / "evil").symlink_to(tmp_path / "secret")
    rules = ProxyFilter(project=project, box_root="/work", copy=CopyFs(work, project, "/work"))
    return rules, work, project


def call(rules, rid, method, **params):
    return rules.to_client({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def test_read_is_served_from_the_copy_by_box_path(setup):
    rules, _, _ = setup
    routed = call(rules, 1, "fs/read_text_file", path="/work/a.py", line=2, limit=1)
    assert routed.forward is None and routed.reply["result"] == {"content": "two\n"}


def test_write_lands_in_the_copy_and_not_in_the_project(setup):
    rules, work, project = setup
    routed = call(rules, 2, "fs/write_text_file", path="/work/b.txt", content="hi")
    assert routed.reply["result"] == {} and (work / "b.txt").read_text() == "hi"
    assert not (project / "b.txt").exists()


@pytest.mark.parametrize("path", ["/etc/passwd", "/work/../secret", "/work/evil", "relative.py"])
def test_paths_outside_the_copy_and_symlinks_are_refused(setup, path):
    rules, work, _ = setup
    for method, extra in (("fs/read_text_file", {}), ("fs/write_text_file", {"content": "x"})):
        routed = call(rules, 3, method, path=path, **extra)
        assert "error" in routed.reply, (method, path)
    assert (work.parent / "secret").read_text() == "host secret"


def test_terminal_is_still_refused(setup):
    rules, _, _ = setup
    assert call(rules, 4, "terminal/create", command="id").reply["error"]["code"] == -32601


def test_initialize_offers_the_agent_the_copy_backed_fs(setup):
    rules, _, _ = setup
    sent = rules.to_agent({"id": 1, "method": "initialize", "params": {"clientCapabilities": {"fs": {"readTextFile": False}}}}).forward
    assert sent["params"]["clientCapabilities"]["fs"] == {"readTextFile": True, "writeTextFile": True}


def update(kind, raw):
    return {"method": "session/update", "params": {"sessionId": "s", "update": {
        "sessionUpdate": "tool_call", "toolCallId": "c", "title": "edit", "kind": kind, "rawInput": raw}}}  # fmt: skip


def test_edit_call_carries_acp_diff_content_with_the_project_path(setup):
    rules, _, project = setup
    out = rules.to_client(update("edit", {"path": "a.py", "old_string": "two", "new_string": "2"})).forward
    up = out["params"]["update"]
    assert up["content"] == [{"type": "diff", "path": str(project / "a.py"), "oldText": "two", "newText": "2"}]
    assert up["locations"] == [{"path": str(project / "a.py")}]


def test_write_call_diffs_against_the_current_copy_and_new_files_have_no_old_text(setup):
    rules, _, _ = setup
    old = rules.to_client(update("edit", {"path": "a.py", "content": "x"})).forward["params"]["update"]["content"][0]
    new = rules.to_client(update("edit", {"path": "new.py", "content": "x"})).forward["params"]["update"]["content"][0]
    assert old["oldText"] == "one\ntwo\nthree\n" and new["oldText"] is None


def test_edit_outside_the_project_gets_no_diff(setup):
    rules, _, _ = setup
    out = rules.to_client(update("edit", {"path": "../../etc/x", "content": "x"})).forward
    assert "content" not in out["params"]["update"]


def test_session_load_maps_the_project_cwd_to_the_box_path(setup):
    rules, _, project = setup
    sent = rules.to_agent({"id": 5, "method": "session/load", "params": {"sessionId": "0123456789ab", "cwd": str(project / "sub"), "mcpServers": [{"x": 1}]}}).forward
    assert sent["params"]["cwd"] == "/work/sub" and sent["params"]["mcpServers"] == []


def test_a_fifo_in_the_copy_is_refused_without_hanging_the_proxy(setup):
    # regression: an agent-made FIFO blocked the host proxy forever on fs/read_text_file
    rules, work, _ = setup
    os.mkfifo(work / "pipe")
    for method, extra in (("fs/read_text_file", {}), ("fs/write_text_file", {"content": "x"})):
        assert "error" in call(rules, 4, method, path="/work/pipe", **extra).reply
