"""Tests for `halagent.tools`: confinement, caps, edit tiers and each built-in tool."""

import json

import pytest

from halagent.tools import ToolError, Toolset, Workspace, files, plan, run, search


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


def put(ws, name, text):
    return files.write(ws, {"path": name, "content": text})


def test_write_then_read_returns_numbered_lines(ws):
    put(ws, "d/a.txt", "one\ntwo\n")
    assert files.read(ws, {"path": "d/a.txt"}) == "     1\tone\n     2\ttwo"


def test_path_outside_root_is_refused(ws):
    with pytest.raises(ToolError) as exc:
        files.read(ws, {"path": "../etc/passwd"})
    assert exc.value.error_type == "outside_workspace"


def test_symlink_escape_is_refused(ws, tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("x")
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(ToolError, match="outside_workspace"):
        files.read(ws, {"path": "link"})


def test_git_directory_is_protected_from_writes(ws, tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ToolError, match="protected"):
        put(ws, ".git/config", "x")


def test_readonly_directory_is_refused_for_writes_but_readable(tmp_path):
    ro = tmp_path / "skills"
    ro.mkdir()
    (ro / "s.md").write_text("hi")
    root = tmp_path / "proj"
    root.mkdir()
    w = Workspace(root, readonly=(ro,))
    assert "hi" in files.read(w, {"path": str(ro / "s.md")})
    with pytest.raises(ToolError, match="protected"):
        files.write(w, {"path": str(ro / "n.md"), "content": "x"})


def test_read_caps_lines_and_hints_continuation(ws):
    put(ws, "big", "\n".join(str(i) for i in range(500)))
    out = files.read(ws, {"path": "big"})
    assert "Continue with start_line=201" in out and out.count("\n") == 200


def test_read_start_past_the_end_reports_the_line_count(ws):
    put(ws, "f", "a\nb\n")
    with pytest.raises(ToolError, match="The file has 2 lines"):
        files.read(ws, {"path": "f", "start_line": 9})


def test_binary_file_is_refused(ws, tmp_path):
    (tmp_path / "b").write_bytes(b"\0\1\2")
    with pytest.raises(ToolError, match="binary"):
        files.read(ws, {"path": "b"})


def test_edit_replaces_unique_text_and_shows_a_diff(ws):
    put(ws, "f", "a b c\n")
    out = files.edit(ws, {"path": "f", "old_string": "b", "new_string": "X"})
    assert out.startswith("Edited f.") and "+a X c" in out


def test_edit_refuses_missing_and_ambiguous_text(ws):
    put(ws, "f", "aa\nz\naa\n")
    with pytest.raises(ToolError, match="ambiguous"):
        files.edit(ws, {"path": "f", "old_string": "aa", "new_string": "b"})
    with pytest.raises(ToolError, match="not_found"):
        files.edit(ws, {"path": "f", "old_string": "qqq", "new_string": "b"})


def test_edit_strips_line_number_prefixes_copied_from_read(ws):
    put(ws, "f", "x = 1\ny = 2\n")
    files.edit(ws, {"path": "f", "old_string": "     2\ty = 2", "new_string": "     2\ty = 3"})
    assert "y = 3" in (ws.root / "f").read_text()


def test_edit_matches_despite_indentation_and_reindents(ws):
    put(ws, "f", "def f():\n    a = 1\n    return a\n")
    out = files.edit(ws, {"path": "f", "old_string": "a = 1\nreturn a", "new_string": "a = 2\nreturn a"})
    assert "tier: whitespace" in out
    assert (ws.root / "f").read_text() == "def f():\n    a = 2\n    return a\n"


def test_edit_decodes_double_encoded_newlines(ws):
    put(ws, "f", "one\ntwo\n")
    files.edit(ws, {"path": "f", "old_string": "one\\ntwo", "new_string": "1\\n2"})
    assert (ws.root / "f").read_text() == "1\n2\n"


def test_edit_that_breaks_python_syntax_is_refused_and_file_kept(ws):
    put(ws, "m.py", "x = (1)\n")
    with pytest.raises(ToolError, match="syntax"):
        files.edit(ws, {"path": "m.py", "old_string": "(1)", "new_string": "(1"})
    assert (ws.root / "m.py").read_text() == "x = (1)\n"


def test_write_reports_created_and_replaced(ws):
    assert put(ws, "n.txt", "a\nb\n") == "Created n.txt (2 lines)."
    assert put(ws, "n.txt", "a\n") == "Replaced n.txt (1 lines, was 2)."


def test_write_over_the_size_cap_is_refused(ws):
    with pytest.raises(ToolError, match="too_large"):
        put(ws, "big", "x" * 200_001)


def test_grep_reports_path_line_and_text(ws):
    put(ws, "a.txt", "foo\nbar\n")
    out = search.grep(ws, {"pattern": "ba."})
    assert out.startswith("1 matches in 1 files") and "a.txt:2: bar" in out


def test_grep_without_a_match_is_a_success(ws):
    assert search.grep(ws, {"pattern": "nope"}).startswith("No matches for 'nope'")


def test_grep_invalid_regex_falls_back_to_exact_text(ws):
    put(ws, "a.txt", "f(x\n")
    assert "searched for the exact text" in search.grep(ws, {"pattern": "f(x"})


def test_grep_python_fallback_matches_the_same_format(ws, monkeypatch):
    monkeypatch.setattr(search.shutil, "which", lambda name: None)
    put(ws, "a.py", "foo\nbar\n")
    (ws.root / ".git").mkdir()
    (ws.root / ".git" / "x.py").write_text("bar\n")
    out = search.grep(ws, {"pattern": "bar", "glob": "*.py"})
    assert "a.py:2: bar" in out and ".git" not in out


def test_glob_lists_matching_files_sorted(ws, monkeypatch):
    monkeypatch.setattr(search.shutil, "which", lambda name: None)
    put(ws, "x/a.py", "")
    put(ws, "b.txt", "")
    assert search.glob(ws, {"pattern": "*.py"}) == "1 files\nx/a.py"


def test_run_returns_exit_code_and_output(ws):
    out = run.run(ws, {"command": "echo hi; exit 3"})
    assert out.startswith("exit code 3 after") and out.endswith("hi")


def test_run_timeout_kills_process_group(ws):
    out = run.run(ws, {"command": "echo start; sleep 30 & sleep 30", "timeout": 1})
    assert out.startswith("stopped after 1 s (timeout)") and "start" in out


def test_run_output_keeps_head_and_tail(ws):
    out = run.run(ws, {"command": "echo HEAD; head -c 50000 /dev/zero | tr '\\0' a; echo; echo TAIL"})
    assert "HEAD" in out and out.endswith("TAIL") and "characters cut" in out and len(out) < 9000


def test_run_environment_drops_hal_variables_and_progress_lines_collapse(ws, monkeypatch):
    monkeypatch.setenv("HAL_SECRET_SOCKET", "/x")
    out = run.run(ws, {"command": "printf 'a\\rb\\n'; echo ${HAL_SECRET_SOCKET:-unset}"})
    assert "\nb\n" in out and "unset" in out and "a\rb" not in out


def test_todo_normalises_items_and_rejects_long_plans():
    assert (
        plan.todo({"plan": "- [x] one\n2. [>] two\nthree"})
        == "Plan recorded:\n- [x] one\n- [~] two\n- [ ] three"
    )
    with pytest.raises(ToolError, match="invalid_args"):
        plan.todo({"plan": "\n".join("- [ ] s" for _ in range(21))})


def test_toolset_offers_spec_order_and_finish_only_in_task_mode(tmp_path):
    assert list(Toolset(tmp_path).tools) == [
        "read",
        "grep",
        "glob",
        "repo_map",
        "edit",
        "patch",
        "write",
        "run",
        "git",
        "todo",
        "subagent",
    ]
    assert list(Toolset(tmp_path, task_mode=True).tools)[-1] == "finish"
    assert "run" not in Toolset(tmp_path, with_run=False).tools


def test_tool_schemas_are_flat(tmp_path):
    for tool in Toolset(tmp_path).tools.values():
        json.dumps(tool.spec())
        for prop in tool.properties.values():
            assert prop["type"] in ("string", "integer")
