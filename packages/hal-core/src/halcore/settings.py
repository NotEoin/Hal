"""Renders `agent.json` and the other run files that a box session starts with (REQ-CLI-99).

The box mounts the run files read-only, so the agent never reads `config.toml`: this module
resolves every value on the host and writes it down. The key set follows the agent's strict
loader (`halagent.agentfile`), which rejects a missing or unknown key; a test round-trips the
output through that loader. Design decisions:

- The context window is passed through as given, so the agent's masking and summary thresholds
  scale with the real window of a 16k or 64k local model, not a default.
- `run_files` returns a callable because the session id is created inside `Session.start`.
- Only regular files below the size caps are copied, and symlinks are skipped: the sandbox
  refuses a run file that passes through a link, and a copy of a huge file would waste the
  small context of a local model anyway.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from halkit import paths

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MODEL_SOCKET = "unix:///run/hal/model.sock"
BROKER_SOCKET = "unix:///run/hal/broker.sock"
BROKER_CALL_TIMEOUT_S = 3900  # a long tool call plus the human approval wait (03-agent 3.16.3)
MAX_RUN_FILE_BYTES = 64 * 1024
MAX_RUN_FILES_BYTES = 1024 * 1024
MAX_SKILL_FILES = 200
TOOLSET_FOR_MODE = {"code": "coding", "assistant": "assistant", "headless": "coding"}

# Defaults from the example in 03-agent.md section 3.16.3.
_BUDGET: dict[str, Any] = {
    "interactive": {"max_tool_calls": 100, "max_model_requests": 200, "max_wall_s": 1800},
    "task": {"max_tool_calls": 50, "max_model_requests": 120, "max_wall_s": 900},
    "warn_calls": 10,
    "max_calls_per_response": 8,
    "max_consecutive_nudges": 3,
    "loop_repeat_limit": 3,
    "max_consecutive_tool_errors": 6,
}
_TOOLS: dict[str, Any] = {
    "read_max_lines": 200,
    "read_max_chars": 12000,
    "grep_max_matches": 100,
    "grep_max_chars": 8000,
    "glob_max": 200,
    "run_timeout_s": 120,
    "run_max_timeout_s": 600,
    "run_head_chars": 2000,
    "run_tail_chars": 6000,
    "write_max_chars": 200000,
    "max_result_chars": 12000,
    "syntax_guard": True,
    "protect": [".git", ".git/**"],
}
_ASSISTANT: dict[str, Any] = {
    "history_turns": 10,
    "max_tool_rounds": 5,
    "tools_per_turn": 8,
    "claims_check": True,
    "log_conversation": True,
    "persona": "",
    "user_name": "",
    "timezone": "UTC",
}


def render_agent_json(
    session_id: str,
    *,
    mode: str = "code",
    channel: str = "tui",
    sandbox: str = "docker-runsc",
    host_root: str = "",
    model: str = "hal-code",
    context_window: int = 0,
    permissions: str = "auto",
    profile: Mapping[str, Any] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> bytes:
    """The `agent.json` bytes for one session; `overrides` replaces keys of the nested tables.

    Raises `ValueError` for an unknown mode or a bad context window, so a wrong value stops the
    start instead of reaching a model the person cannot watch.
    """
    if mode not in TOOLSET_FOR_MODE:
        raise ValueError(f"unknown mode {mode!r}; use one of {', '.join(TOOLSET_FOR_MODE)}")
    if context_window < 0:
        raise ValueError(f"context_window must be 0 or more, got {context_window}")
    coding = mode != "assistant"
    data: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "session_id": session_id,
        "mode": mode,
        "toolset": TOOLSET_FOR_MODE[mode],
        "channel": channel,
        "sandbox": sandbox,
        "workspace": "/work" if coding else "",
        "host_root": host_root if coding else "",
        "scratch": "/scratch",
        "endpoint": {"url": MODEL_SOCKET, "api": "openai"},
        "broker": BROKER_SOCKET,
        "broker_call_timeout_s": BROKER_CALL_TIMEOUT_S,
        "model": model,
        "profile": dict(profile) if profile else {"family": "generic", "values": {}},
        "budget": json.loads(json.dumps(_BUDGET)),
        "context": {
            "window": context_window,
            "mask_at": 0.6,
            "mask_to": 0.4,
            "summarize_at": 0.85,
            "keep_recent": 3,
            "safety_tokens": 512,
        },
        "tools": json.loads(json.dumps(_TOOLS)),
        "permissions": permissions,
        "instructions": {"agents_md": coding, "max_chars": 16000, "user_file": "/run/hal/user/AGENTS.md"},
        "skills": {
            "enabled": coding,
            "dirs": ["/work/.agents/skills", "/work/.claude/skills", "/run/hal/skills"],
            "max_listed": 12,
            "max_chars": 3000,
        },
        "mcp": {"servers": mcp_servers or [], "project_servers": "box", "restart_limit": 1},
        "assistant": dict(_ASSISTANT),
        "prompt": {"extra": ""},
    }
    for table, values in (overrides or {}).items():
        if table not in data or not isinstance(data[table], dict):
            raise ValueError(f"cannot override {table!r}: not a table of agent.json")
        unknown = set(values) - set(data[table])
        if unknown:
            raise ValueError(f"agent.json {table}.{sorted(unknown)[0]} is not a known key")
        data[table].update(values)
    return (json.dumps(data, indent=1, sort_keys=False) + "\n").encode("ascii")


def user_files(config_dir: Path | None = None) -> dict[str, bytes]:
    """`user/AGENTS.md` and `skills/...` from the config folder, within the size caps."""
    root = config_dir or paths.config_dir()
    out: dict[str, bytes] = {}
    total = 0
    candidates: list[tuple[str, Path]] = []
    agents = root / "AGENTS.md"
    if agents.is_file() and not agents.is_symlink():
        candidates.append(("user/AGENTS.md", agents))
    skills = root / "skills"
    if skills.is_dir() and not skills.is_symlink():
        for path in sorted(skills.rglob("*"))[:MAX_SKILL_FILES]:
            if path.is_file() and not path.is_symlink():
                candidates.append(("skills/" + path.relative_to(skills).as_posix(), path))
    for name, path in candidates:
        size = path.stat().st_size
        if size > MAX_RUN_FILE_BYTES or total + size > MAX_RUN_FILES_BYTES:
            log.warning("run file %s skipped: %d bytes is over the cap", name, size)
            continue
        out[name] = path.read_bytes()
        total += size
    return out


def run_files(**render_args: Any) -> Callable[[str], dict[str, bytes]]:
    """A callable `(session_id) -> run files` for `Session.start(run_files=...)`."""

    def build(session_id: str) -> dict[str, bytes]:
        files = user_files()
        files["agent.json"] = render_agent_json(session_id, **render_args)
        return files

    return build
