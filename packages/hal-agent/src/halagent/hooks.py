"""User hooks: commands run around tool calls and at session start and stop, inside the box.

Hooks come from `~/.config/hal/hooks.toml`, `<project>/.hal/hooks.toml` and the `[[hook]]` tables of
`<project>/.hal/settings.toml`. The agent runs inside
the box, so a hook is only as powerful as the agent already is: it can reach nothing the model
could not reach with the `execute` tool. The project file lives in the working copy and a model
can edit it, so both files are read once when the session starts and later edits wait for the next
session. A hook that fails or times out never ends the turn; only a `pre_tool` hook that exits
with `BLOCK_EXIT_CODE` stops the call, and its stderr is the reason the model sees.

A `command` is split into words like a shell line, but no shell runs it (a repo style rule), so
pipes and redirects need a script: `sh check.sh`.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("halagent.hooks")

EVENTS = ("pre_tool", "post_tool", "user_prompt_submit", "session_start", "session_stop")
BLOCK_EXIT_CODE = 2  # the same code Claude Code hooks use to block a call
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 120
MAX_HOOKS = 20
MAX_NOTE_CHARS = 2_000
MAX_ARG_CHARS = 20_000  # a hook sees the start of a long argument, not a megabyte of it


@dataclass(frozen=True)
class Hook:
    """One configured command."""

    event: str
    command: str
    tools: tuple[str, ...] = ()
    timeout: int = DEFAULT_TIMEOUT_S


def parse_hooks(text: str, source: str) -> list[Hook]:
    """Parses one hooks file; raises `ValueError` naming `source` for any bad entry."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{source}: not valid TOML: {exc}") from exc
    entries = data.get("hook", [])
    if not isinstance(entries, list) or len(entries) > MAX_HOOKS:
        raise ValueError(f"{source}: `hook` must be a list of at most {MAX_HOOKS} tables")
    hooks = []
    for n, e in enumerate(entries, 1):
        where = f"{source}: hook {n}"
        if not isinstance(e, dict) or e.get("event") not in EVENTS:
            raise ValueError(f"{where}: `event` must be one of {', '.join(EVENTS)}")
        command, tools, timeout = e.get("command"), e.get("tools", []), e.get("timeout", DEFAULT_TIMEOUT_S)
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{where}: `command` must be a non-empty string")
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ValueError(f"{where}: `tools` must be a list of tool names")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT_S:
            raise ValueError(f"{where}: `timeout` must be 1 to {MAX_TIMEOUT_S} seconds")
        hooks.append(Hook(e["event"], command, tuple(tools), timeout))
    return hooks


class Hooks:
    """The hooks of one session."""

    def __init__(self, hooks: list[Hook], cwd: Path) -> None:
        self.hooks = hooks
        self.cwd = cwd

    @classmethod
    def load(cls, cwd: Path, config_dir: Path) -> Hooks:
        """Reads the user and project files; a bad file is logged and skipped, never fatal."""
        found: list[Hook] = []
        for path in (config_dir / "hooks.toml", cwd / ".hal" / "hooks.toml", cwd / ".hal" / "settings.toml"):
            if not path.is_file():
                continue
            try:
                found += parse_hooks(path.read_text("utf-8"), str(path))
            except (ValueError, OSError) as exc:
                log.warning("hooks skipped: %s", exc)
        return cls(found, cwd)

    def __bool__(self) -> bool:
        return bool(self.hooks)

    def _run(self, hook: Hook, payload: dict) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(  # no shell (repo style rule): the command line is split like a shell would
                shlex.split(hook.command), cwd=self.cwd, input=json.dumps(payload), text=True,
                capture_output=True, timeout=hook.timeout, check=False,
            )  # fmt: skip
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            log.warning("hook %r failed: %s", hook.command, exc)
            return None

    def _matching(self, event: str, tool: str = "") -> list[Hook]:
        return [h for h in self.hooks if h.event == event and (not h.tools or tool in h.tools)]

    @staticmethod
    def _args(args: dict) -> dict:
        return {k: (v[:MAX_ARG_CHARS] if isinstance(v, str) else v) for k, v in args.items()}

    def pre_tool(self, tool: str, args: dict) -> str | None:
        """Returns the reason when a hook blocks the call, else None."""
        for hook in self._matching("pre_tool", tool):
            done = self._run(hook, {"event": "pre_tool", "tool": tool, "args": self._args(args)})
            if done and done.returncode == BLOCK_EXIT_CODE:
                return (done.stderr or done.stdout).strip()[:MAX_NOTE_CHARS] or "blocked by a hook"
        return None

    def post_tool(self, tool: str, args: dict, result: str) -> str:
        """Returns notes from failed hooks to append to the result, or an empty string."""
        notes = []
        for hook in self._matching("post_tool", tool):
            payload = {
                "event": "post_tool",
                "tool": tool,
                "args": self._args(args),
                "result": result[:MAX_ARG_CHARS],
            }
            done = self._run(hook, payload)
            if done and done.returncode != 0:
                notes.append(
                    f"[hook {hook.command!r} exited {done.returncode}] {(done.stderr or done.stdout).strip()}"
                )
        return "\n".join(notes)[:MAX_NOTE_CHARS]

    def user_prompt_submit(self, prompt: str) -> str | None:
        """Returns the reason when a hook blocks the prompt, else None."""
        for hook in self._matching("user_prompt_submit"):
            done = self._run(hook, {"event": "user_prompt_submit", "prompt": prompt[:MAX_ARG_CHARS]})
            if done and done.returncode == BLOCK_EXIT_CODE:
                return (done.stderr or done.stdout).strip()[:MAX_NOTE_CHARS] or "blocked by a hook"
        return None

    def lifecycle(self, event: str) -> None:
        """Runs the `session_start` or `session_stop` hooks."""
        for hook in self._matching(event):
            self._run(hook, {"event": event})
