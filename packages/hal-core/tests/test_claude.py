"""Tests for `hal claude`: the gateway env, the privacy block and the permission mode (REQ-CLI-44, 45)."""

from __future__ import annotations

from halcore import claude


def test_claude_env_and_permission_mode():
    env = claude.gateway_env("hal-client-x", 131072, {})
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8741"
    assert env["ANTHROPIC_AUTH_TOKEN"] == env["ANTHROPIC_API_KEY"] == "hal-client-x"
    assert (env["ANTHROPIC_MODEL"], env["CLAUDE_CODE_SUBAGENT_MODEL"]) == ("hal/main", "hal/agent")
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "131072"
    assert claude.with_permission_mode(["-c"]) == ["--permission-mode", "default", "-c"]
    assert claude.with_permission_mode(["--permission-mode", "plan"]) == ["--permission-mode", "plan"]
    assert claude.with_permission_mode(["--dangerously-skip-permissions"]) == ["--dangerously-skip-permissions"]


def test_claude_privacy_env_block_matches_legacy_list():
    # the names the old bash `hal` exported before `exec claude` (git show 9e899b6:hal, lines 286-293)
    legacy = (
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC DISABLE_TELEMETRY DISABLE_ERROR_REPORTING DISABLE_AUTOUPDATER "
        "DISABLE_UPDATES DISABLE_GROWTHBOOK DISABLE_BUG_COMMAND DISABLE_FEEDBACK_COMMAND DISABLE_INSTALLATION_CHECKS "
        "ENABLE_CLAUDEAI_MCP_SERVERS CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY "
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL CLAUDE_CODE_DISABLE_WORKING_SYNC "
        "CLAUDE_CODE_DISABLE_ARTIFACT CLAUDE_CODE_DISABLE_WEB_FETCH CLAUDE_CODE_DISABLE_VITALS_EMITTER "
        "CLAUDE_CODE_DISABLE_ORG_MEMORY CLAUDE_CODE_DISABLE_MEMORY_PERIODIC_RESYNC CLAUDE_CODE_SKIP_REPO_UPLOAD "
        "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK CLAUDE_CODE_DISABLE_FAST_MODE"
    ).split()
    assert sorted(claude.PRIVACY_ENV) == sorted(legacy)
    env = claude.gateway_env("t", 0, {})
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9" and env["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env  # no reported window: Claude Code keeps its own


def test_allow_network_lifts_the_proxy_backstop_and_extra_env_is_added():
    env = claude.gateway_env("t", 0, {"allow_network": True, "env": {"HAL_UNIVERSITY": "Example U"}})
    assert "HTTPS_PROXY" not in env and env["HAL_UNIVERSITY"] == "Example U"


def test_the_main_context_is_read_from_the_gateway_aliases():
    class Owner:
        def aliases(self):
            return {"aliases": [{"name": "hal-code", "context_window": 1}, {"name": "hal/main", "context_window": 98304}]}

    assert claude.main_context(Owner()) == 98304


def test_without_claude_on_the_path_it_says_how_to_fix_it(monkeypatch, capsys):
    monkeypatch.setattr(claude.shutil, "which", lambda name: None)
    assert claude.main([]) == claude.EXIT_REFUSED
    err = capsys.readouterr().err
    assert err.startswith("hal: Claude Code runs unsandboxed") and "not on your PATH" in err
