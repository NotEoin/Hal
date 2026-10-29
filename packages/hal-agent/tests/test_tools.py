"""Tests for `halagent.tools`: confinement, caps and each built-in tool."""

import pytest

from halagent.tools import ToolError, Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


def test_write_then_read_returns_numbered_lines(ws):
    ws.write_file({"path": "d/a.txt", "content": "one\ntwo\n"})
    assert ws.read_file({"path": "d/a.txt"}) == "1\tone\n2\ttwo"


def test_path_outside_root_is_refused(ws):
    with pytest.raises(ToolError, match="outside_workspace"):
        ws.read_file({"path": "../etc/passwd"})


def test_symlink_escape_is_refused(ws, tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("x")
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(ToolError, match="outside_workspace"):
        ws.read_file({"path": "link"})


def test_read_caps_lines_and_hints_continuation(ws):
    ws.write_file({"path": "big", "content": "\n".join(str(i) for i in range(500))})
    out = ws.read_file({"path": "big"})
    assert "offset=201" in out and out.count("\n") == 200


def test_binary_file_is_refused(ws, tmp_path):
    (tmp_path / "b").write_bytes(b"\0\1\2")
    with pytest.raises(ToolError, match="binary"):
        ws.read_file({"path": "b"})


def test_edit_replaces_unique_text(ws):
    ws.write_file({"path": "f", "content": "a b c"})
    ws.edit_file({"path": "f", "old_text": "b", "new_text": "X"})
    assert ws.read_file({"path": "f"}) == "1\ta X c"


def test_edit_refuses_missing_and_ambiguous_text(ws):
    ws.write_file({"path": "f", "content": "aa"})
    with pytest.raises(ToolError, match="ambiguous"):
        ws.edit_file({"path": "f", "old_text": "a", "new_text": "b"})
    with pytest.raises(ToolError, match="not_found"):
        ws.edit_file({"path": "f", "old_text": "z", "new_text": "b"})


def test_list_files_filters_by_glob_and_skips_git(ws):
    ws.write_file({"path": "x/a.py", "content": ""})
    ws.write_file({"path": "b.txt", "content": ""})
    ws.write_file({"path": ".git/c.py", "content": ""})
    assert ws.list_files({"pattern": "*.py"}) == "x/a.py"


def test_grep_reports_path_line_and_text(ws):
    ws.write_file({"path": "a.txt", "content": "foo\nbar\n"})
    assert ws.grep({"pattern": "ba."}) == "a.txt:2:bar"


def test_run_command_returns_output_and_exit_code(ws):
    assert ws.run_command({"command": "echo hi; exit 3"}) == "hi\n\n[exit code 3]"


def test_run_command_timeout_kills_process_group(ws):
    out = ws.run_command({"command": "sleep 30 & sleep 30", "timeout": 1})
    assert "timed out after 1s" in out


def test_run_command_output_is_capped(ws):
    out = ws.run_command({"command": "head -c 50000 /dev/zero | tr '\\0' a"})
    assert "truncated" in out and len(out) < 9000


def test_tool_schemas_are_flat():
    for tool in Workspace(".").tools.values():
        for prop in tool.properties.values():
            assert prop["type"] in ("string", "integer")
