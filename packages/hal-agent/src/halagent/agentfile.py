"""The `agent.json` run file (03-agent 3.16.3): a strict loader and the mapping to settings.

halcore renders the file with resolved values; the box mounts it read-only, so the agent never
reads `config.toml`. Every key is required and an unknown key fails loudly, because a typo in a
file the model cannot see would otherwise silently weaken a limit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from halagent.assistant import AssistantOptions
from halagent.profiles import KEYS as PROFILE_KEYS
from halagent.profiles import Profile
from halagent.settings import AgentSettings, Tuning

SCHEMA_VERSION = 1
MODES = ("code", "assistant", "headless")
TOOLSETS = ("coding", "assistant")
SESSION_ID = re.compile(r"[0-9a-f]{12}")
MAX_FILE_BYTES = 1_000_000  # a run file is a few KiB; a larger one is not ours

_STR, _INT, _NUM, _BOOL, _LIST = str, int, (int, float), bool, list
BUDGET_KEYS = {"max_tool_calls": _INT, "max_model_requests": _INT, "max_wall_s": _INT}
# key -> type, or a nested object schema
SCHEMA: dict[str, Any] = {
    "v": _INT,
    "session_id": _STR,
    "mode": _STR,
    "toolset": _STR,
    "channel": _STR,
    "sandbox": _STR,
    "workspace": _STR,
    "host_root": _STR,
    "scratch": _STR,
    "endpoint": {"url": _STR, "api": _STR},
    "broker": _STR,
    "broker_call_timeout_s": _INT,
    "model": _STR,
    "profile": {"family": _STR, "values": dict},
    "budget": {
        "interactive": BUDGET_KEYS,
        "task": BUDGET_KEYS,
        "warn_calls": _INT,
        "max_calls_per_response": _INT,
        "max_consecutive_nudges": _INT,
        "loop_repeat_limit": _INT,
        "max_consecutive_tool_errors": _INT,
    },
    "context": {
        "window": _INT,
        "mask_at": _NUM,
        "mask_to": _NUM,
        "summarize_at": _NUM,
        "keep_recent": _INT,
        "safety_tokens": _INT,
    },
    "tools": {
        "read_max_lines": _INT,
        "read_max_chars": _INT,
        "grep_max_matches": _INT,
        "grep_max_chars": _INT,
        "glob_max": _INT,
        "run_timeout_s": _INT,
        "run_max_timeout_s": _INT,
        "run_head_chars": _INT,
        "run_tail_chars": _INT,
        "write_max_chars": _INT,
        "max_result_chars": _INT,
        "syntax_guard": _BOOL,
        "protect": _LIST,
    },
    "permissions": _STR,
    "instructions": {"agents_md": _BOOL, "max_chars": _INT, "user_file": _STR},
    "skills": {"enabled": _BOOL, "dirs": _LIST, "max_listed": _INT, "max_chars": _INT},
    "mcp": {"servers": _LIST, "project_servers": _STR, "restart_limit": _INT},
    "assistant": {
        "history_turns": _INT,
        "max_tool_rounds": _INT,
        "tools_per_turn": _INT,
        "claims_check": _BOOL,
        "log_conversation": _BOOL,
        "persona": _STR,
        "user_name": _STR,
        "timezone": _STR,
    },
    "prompt": {"extra": _STR, "repo_map_auto": bool},
}


class AgentFileError(ValueError):
    """The run file is missing, malformed or does not match schema 1; the message names the key."""


def _check(data: object, schema: Mapping[str, Any], where: str) -> None:
    if not isinstance(data, dict):
        raise AgentFileError(f"{where or 'agent.json'}: must be an object")
    for key in data:
        if key not in schema:
            raise AgentFileError(f"{where}{key}: unknown key")
    for key, kind in schema.items():
        if key not in data:
            raise AgentFileError(f"{where}{key}: missing")
        value = data[key]
        if isinstance(kind, dict):
            _check(value, kind, f"{where}{key}.")
            continue
        wrong = not isinstance(value, kind) or (isinstance(value, bool) and kind not in (bool, _NUM))
        if kind is _INT and isinstance(value, bool):
            wrong = True
        if wrong:
            raise AgentFileError(f"{where}{key}: wrong type")


@dataclass(frozen=True)
class AgentFile:
    """A validated `agent.json`; `data` is the parsed object."""

    data: Mapping[str, Any]

    @property
    def session_id(self) -> str:
        """The host's session id for the first session of the process, or empty."""
        return str(self.data["session_id"])

    @property
    def extra_prompt(self) -> str:
        """Text appended to the system prompt."""
        return str(self.data["prompt"]["extra"])

    @property
    def permissions(self) -> str:
        """`auto` or `review`."""
        return str(self.data["permissions"])

    @property
    def toolset(self) -> str:
        """`coding` or `assistant`."""
        return str(self.data["toolset"])

    def extras_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for `boot.prompt_extras` (instructions and skills)."""
        i, k = self.data["instructions"], self.data["skills"]
        return {"agents_md": bool(i["agents_md"]), "max_chars": int(i["max_chars"]),
                "user_file": Path(i["user_file"]) if i["user_file"] else None,
                "skills_enabled": bool(k["enabled"]), "skill_dirs": tuple(Path(d) for d in k["dirs"]),
                "max_listed": int(k["max_listed"]), "skills_chars": int(k["max_chars"])}  # fmt: skip

    def tuning(self) -> Tuning:
        """The budget, context and tool limits of the file."""
        b, c, t = self.data["budget"], self.data["context"], self.data["tools"]
        return Tuning(
            warn_calls=int(b["warn_calls"]), max_calls_per_response=int(b["max_calls_per_response"]),
            max_consecutive_nudges=int(b["max_consecutive_nudges"]),
            loop_repeat_limit=int(b["loop_repeat_limit"]),
            max_consecutive_tool_errors=int(b["max_consecutive_tool_errors"]),
            mask_at=float(c["mask_at"]), mask_to=float(c["mask_to"]), summarize_at=float(c["summarize_at"]),
            keep_recent=int(c["keep_recent"]), safety_tokens=int(c["safety_tokens"]),
            max_result_chars=int(t["max_result_chars"]), syntax_guard=bool(t["syntax_guard"]),
            protect=tuple(str(g) for g in t["protect"]),
        )  # fmt: skip

    def mcp_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for `boot.start_user_mcp` (the sandbox, servers and restart limit)."""
        m = self.data["mcp"]
        return {"configured": list(m["servers"]), "project_servers": str(m["project_servers"]),
                "restart_limit": int(m["restart_limit"]), "sandbox": str(self.data["sandbox"])}  # fmt: skip

    def assistant_options(self) -> AssistantOptions:
        """The `assistant` table and the channel as options for `build_assistant`."""
        a = self.data["assistant"]
        return AssistantOptions(
            channel=str(self.data["channel"]) or "chat",
            user_name=str(a["user_name"]),
            persona=str(a["persona"]),
            timezone=str(a["timezone"]),
            tools_per_turn=int(a["tools_per_turn"]),
            max_tool_rounds=int(a["max_tool_rounds"]),
            history_turns=int(a["history_turns"]),
            claims_check=bool(a["claims_check"]),
            log_conversation=bool(a["log_conversation"]),
        )

    def profile(self) -> Profile:
        """The profile the host resolved, so the box never reads a profile file."""
        p = self.data["profile"]
        values = dict(p["values"])
        return Profile(str(self.data["model"]), str(p["family"]) or "default", values,
                       {k: "agent.json" for k in values})  # fmt: skip

    def to_settings(self, *, task: bool = False) -> AgentSettings:
        """Maps the file to `AgentSettings`; `task` picks the task budget over the interactive one."""
        d = self.data
        budget = d["budget"]["task" if task else "interactive"]
        window = int(d["context"]["window"])
        return AgentSettings(
            endpoint=str(d["endpoint"]["url"]),
            model=str(d["model"]),
            api=str(d["endpoint"]["api"]),
            broker=str(d["broker"]),
            max_turns=int(budget["max_model_requests"]),
            max_tool_calls=int(budget["max_tool_calls"]),
            max_wall_s=int(budget["max_wall_s"]),
            tuning=self.tuning(),
            repo_map_auto=bool(d["prompt"]["repo_map_auto"]),
            **({"context_window": window} if window > 0 else {}),
        )


def load_agent_file(path: Path) -> AgentFile:
    """Reads and validates `path`; raises `AgentFileError` naming the first bad key."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise AgentFileError(f"{path}: larger than {MAX_FILE_BYTES} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentFileError(f"{path}: {exc}") from exc
    try:
        _check(data, SCHEMA, "")
        if data["v"] != SCHEMA_VERSION:
            raise AgentFileError(f"v: must be {SCHEMA_VERSION}")
        if data["mode"] not in MODES or data["toolset"] not in TOOLSETS:
            raise AgentFileError("mode or toolset: unknown value")
        if data["session_id"] and not SESSION_ID.fullmatch(data["session_id"]):
            raise AgentFileError("session_id: must be 12 lowercase hex characters")
        if data["permissions"] not in ("auto", "review"):
            raise AgentFileError("permissions: must be auto or review")
        c = data["context"]
        if not 0 < c["mask_to"] < c["mask_at"] < c["summarize_at"] <= 1:
            raise AgentFileError("context: need 0 < mask_to < mask_at < summarize_at <= 1")
        unknown = set(data["profile"]["values"]) - set(PROFILE_KEYS)
        if unknown:
            raise AgentFileError(f"profile.values.{sorted(unknown)[0]}: unknown key")
    except AgentFileError as exc:
        raise AgentFileError(f"{path}: {exc}") from exc
    return AgentFile(data)
