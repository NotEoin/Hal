"""Tests for the `patch` tool and the compact tool set."""

import pytest

from halagent.loop import Agent
from halagent.settings import AgentSettings
from halagent.tools import ToolError, Toolset, Workspace
from halagent.tools.patch import patch

SRC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


def block(old, new):
    return f"<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE"


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "m.py").write_text(SRC)
    return Workspace(tmp_path)


def test_search_replace_blocks_are_all_applied(ws, tmp_path):
    text = (
        block("    return a + b", "    return b + a") + "\n" + block("    return a - b", "    return -b + a")
    )
    out = patch(ws, {"path": "m.py", "patch": text})
    assert "2 blocks" in out
    assert "return b + a" in (tmp_path / "m.py").read_text() and "-b + a" in (tmp_path / "m.py").read_text()


def test_block_with_wrong_indentation_still_lands_by_whitespace_matching(ws, tmp_path):
    patch(ws, {"path": "m.py", "patch": block("return a + b", "return 42")})
    assert "    return 42" in (tmp_path / "m.py").read_text()


def test_unified_diff_hunks_are_applied(ws, tmp_path):
    diff = (
        "--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a + b + 0\n"
    )
    patch(ws, {"path": "m.py", "patch": diff})
    assert "a + b + 0" in (tmp_path / "m.py").read_text()


def test_a_failing_block_leaves_the_file_untouched_and_names_the_block(ws, tmp_path):
    text = block("    return a + b", "    return 1") + "\n" + block("not in file", "x")
    with pytest.raises(ToolError, match="block 2 of 2") as exc:
        patch(ws, {"path": "m.py", "patch": text})
    assert "Read the file again" in exc.value.message
    assert (tmp_path / "m.py").read_text() == SRC


def test_patch_that_breaks_python_syntax_is_refused(ws, tmp_path):
    with pytest.raises(ToolError, match="break"):
        patch(ws, {"path": "m.py", "patch": block("    return a + b", "    return (")})
    assert (tmp_path / "m.py").read_text() == SRC


def test_text_without_blocks_gets_a_format_reminder(ws):
    with pytest.raises(ToolError, match="SEARCH"):
        patch(ws, {"path": "m.py", "patch": "just change it please"})


def test_compact_toolset_offers_fewer_simpler_tools(tmp_path):
    assert set(Toolset(tmp_path, compact=True).tools) == {"read", "grep", "edit", "write", "run"}
    assert "patch" in Toolset(tmp_path).tools


def test_compact_profile_key_trims_the_agents_tools(server, tmp_path):
    from halagent.profiles import resolve_profile

    profile = resolve_profile("m", overrides={"tool_set": "compact"})
    agent = Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path), profile=profile)
    assert "glob" not in agent.toolset.tools and "read" in agent.toolset.tools
