"""Tests for `halcore.settings` (`agent.json` and run files, REQ-CLI-99)."""

import json

import pytest

from halcore import settings
from halkit.testing import TempHalHome

SID = "3f9a1c0b7e2d"


def test_agent_settings_rendered_into_run_files():
    with TempHalHome() as home:
        (home.config.parent / "AGENTS.md").write_text("be brief\n")
        (home.config.parent / "skills" / "git").mkdir(parents=True)
        (home.config.parent / "skills" / "git" / "SKILL.md").write_text("# git\n")
        files = settings.run_files(host_root="/p", context_window=16384)(SID)
    assert set(files) == {"agent.json", "user/AGENTS.md", "skills/git/SKILL.md"}
    data = json.loads(files["agent.json"])
    assert data["session_id"] == SID and data["context"]["window"] == 16384
    assert data["endpoint"]["url"] == settings.MODEL_SOCKET and data["broker"] == settings.BROKER_SOCKET


def test_rendered_file_passes_the_agents_strict_loader(tmp_path):
    agentfile = pytest.importorskip("halagent.agentfile")
    path = tmp_path / "agent.json"
    path.write_bytes(settings.render_agent_json(SID, host_root="/p", context_window=32768))
    assert agentfile.load_agent_file(path).data["toolset"] == "coding"
    path.write_bytes(settings.render_agent_json(SID, mode="assistant", channel="voice"))
    assert agentfile.load_agent_file(path).data["workspace"] == ""


def test_unknown_override_key_fails_loudly():
    with pytest.raises(ValueError, match="not a known key"):
        settings.render_agent_json(SID, overrides={"tools": {"typo_key": 1}})


def test_overrides_change_a_nested_value_without_leaking_into_the_next_render():
    data = json.loads(settings.render_agent_json(SID, overrides={"tools": {"run_timeout_s": 30}}))
    assert data["tools"]["run_timeout_s"] == 30
    assert json.loads(settings.render_agent_json(SID))["tools"]["run_timeout_s"] == 120


def test_unknown_mode_and_negative_window_are_rejected():
    with pytest.raises(ValueError, match="unknown mode"):
        settings.render_agent_json(SID, mode="chatty")
    with pytest.raises(ValueError, match="context_window"):
        settings.render_agent_json(SID, context_window=-1)


def test_oversized_and_linked_user_files_are_skipped(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"x" * (settings.MAX_RUN_FILE_BYTES + 1))
    (tmp_path / "skills").mkdir()
    real = tmp_path / "real.md"
    real.write_text("hi")
    (tmp_path / "skills" / "link.md").symlink_to(real)
    assert settings.user_files(tmp_path) == {}
