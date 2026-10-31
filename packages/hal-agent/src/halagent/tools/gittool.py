"""The `git` tool: status, diff, log and commit inside the working copy.

Four fixed actions replace free-form shell for the commands a coding loop needs, with short
output and no pager. Hooks are switched off (`core.hooksPath`) because a model-planted hook would
run outside the policy; a missing identity is filled with a fixed local one so a commit never
fails on a fresh copy. Only the copy under `/work` is touched, and the host applies changes
through `hal code accept`.
"""

from __future__ import annotations

import os

from halagent.tools.paths import ToolError, Workspace
from halkit import proc

GIT_TIMEOUT_S = 30
MAX_GIT_CHARS = 8_000
LOG_COUNT = 10
ACTIONS = ("status", "diff", "log", "commit")
ENV_OVERRIDES = {
    "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "NO_COLOR": "1",
}  # fmt: skip
SAFE_CONFIG = (
    "-c", "core.hooksPath=/dev/null", "-c", "user.name=Hal agent", "-c", "user.email=agent@hal.invalid",
    "-c", "commit.gpgsign=false",
)  # fmt: skip


def _git(ws: Workspace, *argv: str) -> str:
    env = {**os.environ, **ENV_OVERRIDES}
    try:
        done = proc.run(
            ["git", *SAFE_CONFIG, *argv], timeout=GIT_TIMEOUT_S, cwd=ws.root, env=env, check=False
        )
    except proc.ProcTimeout as exc:
        raise ToolError("timeout", f"git took more than {GIT_TIMEOUT_S} s") from exc
    except OSError as exc:
        raise ToolError("unavailable", f"git cannot run: {exc}") from exc
    out = (done.stdout + done.stderr).decode("utf-8", "replace").strip()
    if done.returncode != 0:
        raise ToolError("git_failed", out[:MAX_GIT_CHARS] or f"git exited with {done.returncode}")
    if len(out) > MAX_GIT_CHARS:
        out = out[:MAX_GIT_CHARS] + "\n[... output cut; pass a path to narrow it ...]"
    return out or "(no output)"


def git(ws: Workspace, args: dict) -> str:
    """Runs one of `status`, `diff`, `log`, `commit`; `path` narrows diff and log, `message` commits."""
    action = str(args.get("action") or "")
    if action not in ACTIONS:
        raise ToolError("invalid_args", f"action must be one of {', '.join(ACTIONS)}")
    path = args.get("path")
    narrow = ["--", ws.display(ws.resolve(path))] if path else []
    if action == "status":
        return _git(ws, "status", "--short", "--branch")
    if action == "diff":
        return (
            _git(ws, "diff", "--stat", "--patch", "HEAD", *narrow)
            if _has_head(ws)
            else _git(ws, "diff", *narrow)
        )
    if action == "log":
        return _git(ws, "log", f"-{LOG_COUNT}", "--oneline", "--decorate", *narrow)
    message = str(args.get("message") or "").strip()
    if not message or "\n" in message:
        raise ToolError("invalid_args", "message must be a one-line commit message")
    _git(ws, "add", "-A", *narrow)
    return _git(ws, "commit", "-m", message)


def _has_head(ws: Workspace) -> bool:
    try:
        _git(ws, "rev-parse", "--verify", "HEAD")
    except ToolError:
        return False
    return True
