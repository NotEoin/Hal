"""Built-in coding tools confined to a workspace root.

Every path is resolved with `realpath` and refused if it leaves the root, so symlink escapes
fail. Schemas are flat (string and integer properties only). Tools raise `ToolError`; the loop
turns that into a result the model can read. `run_command` has a timeout and an output cap.
"""

from __future__ import annotations

import fnmatch
import os
import re
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_READ_LINES = 200
MAX_READ_CHARS = 12_000
MAX_WRITE_CHARS = 200_000
MAX_LIST = 200
MAX_GREP = 100
MAX_GREP_LINE = 300
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
OUTPUT_HEAD = 2_000
OUTPUT_TAIL = 6_000
KILL_GRACE_S = 5
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}


class ToolError(Exception):
    """A tool call failed in a way the model should see and can fix."""


@dataclass(frozen=True)
class Tool:
    """A callable tool: `schema` is the JSON schema of its flat arguments."""

    name: str
    description: str
    properties: dict[str, dict]
    required: tuple[str, ...]
    fn: Callable[[dict], str]

    def spec(self) -> dict:
        """The OpenAI function-tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                },
            },
        }


def _s(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _i(desc: str) -> dict:
    return {"type": "integer", "description": desc}


class Workspace:
    """The coding toolset rooted at one directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"workspace root is not a directory: {root}")
        self.tools: dict[str, Tool] = {t.name: t for t in self._build()}

    def resolve(self, rel: object) -> Path:
        """Returns the real path of `rel` inside the root; raises `ToolError` if it escapes."""
        if not isinstance(rel, str) or not rel:
            raise ToolError("path must be a non-empty string")
        real = Path(os.path.realpath(self.root / rel))
        if real != self.root and self.root not in real.parents:
            raise ToolError(f"outside_workspace: {rel}")
        return real

    def _build(self) -> list[Tool]:
        return [
            Tool("read_file", "Read a text file with numbered lines.",
                 {"path": _s("File path"), "offset": _i("First line, 1-based")}, ("path",), self.read_file),
            Tool("write_file", "Create or overwrite a file.",
                 {"path": _s("File path"), "content": _s("Full file content")}, ("path", "content"),
                 self.write_file),
            Tool("edit_file", "Replace one exact, unique occurrence of old_text with new_text.",
                 {"path": _s("File path"), "old_text": _s("Exact text to find"),
                  "new_text": _s("Replacement text")}, ("path", "old_text", "new_text"), self.edit_file),
            Tool("list_files", "List files under a directory, optionally filtered by a glob pattern.",
                 {"path": _s("Directory, default '.'"), "pattern": _s("Glob such as *.py")}, (),
                 self.list_files),
            Tool("grep", "Search file contents with a regular expression.",
                 {"pattern": _s("Regex"), "path": _s("Directory or file, default '.'")}, ("pattern",),
                 self.grep),
            Tool("run_command", "Run a shell command in the workspace.",
                 {"command": _s("Shell command"), "timeout": _i("Seconds, default 120, max 600")},
                 ("command",), self.run_command),
        ]  # fmt: skip

    def read_file(self, args: dict) -> str:
        """Numbered lines of a file, at most 200 lines and 12000 characters."""
        path = self.resolve(args.get("path"))
        if not path.is_file():
            raise ToolError(f"not a file: {args['path']}")
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            raise ToolError("binary file")
        lines = data.decode("utf-8", "replace").splitlines()
        start = max(int(args.get("offset") or 1), 1)
        out, size = [], 0
        for n, line in enumerate(lines[start - 1 : start - 1 + MAX_READ_LINES], start):
            row = f"{n}\t{line}"
            if size + len(row) > MAX_READ_CHARS:
                break
            out.append(row)
            size += len(row) + 1
        last = start + len(out) - 1
        if last < len(lines):
            out.append(f"[more lines: continue with offset={last + 1}, file has {len(lines)}]")
        return "\n".join(out) or "[empty file]"

    def write_file(self, args: dict) -> str:
        """Writes a file, creating parent directories."""
        path = self.resolve(args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        if len(content) > MAX_WRITE_CHARS:
            raise ToolError(f"content over {MAX_WRITE_CHARS} characters")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {len(content.splitlines())} lines to {args['path']}"

    def edit_file(self, args: dict) -> str:
        """Replaces the single exact occurrence of `old_text`."""
        path = self.resolve(args.get("path"))
        old, new = args.get("old_text"), args.get("new_text")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ToolError("old_text must be non-empty text and new_text a string")
        if not path.is_file():
            raise ToolError(f"not a file: {args['path']}")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ToolError("not_found: old_text does not occur in the file")
        if count > 1:
            raise ToolError(f"ambiguous: old_text occurs {count} times, add context")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"edited {args['path']}"

    def _walk(self, base: Path):
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                yield Path(dirpath) / name

    def list_files(self, args: dict) -> str:
        """Sorted relative paths, at most 200."""
        base = self.resolve(args.get("path") or ".")
        pattern = args.get("pattern") or "*"
        found = [
            str(p.relative_to(self.root))
            for p in self._walk(base)
            if fnmatch.fnmatch(p.name, pattern) or fnmatch.fnmatch(str(p.relative_to(base)), pattern)
        ]
        found.sort()
        extra = f"\n[{len(found) - MAX_LIST} more not shown]" if len(found) > MAX_LIST else ""
        return "\n".join(found[:MAX_LIST]) + extra if found else "[no files]"

    def grep(self, args: dict) -> str:
        """`path:line:text` matches, at most 100."""
        try:
            rx = re.compile(str(args.get("pattern")))
        except re.error as exc:
            raise ToolError(f"invalid regex: {exc}") from exc
        base = self.resolve(args.get("path") or ".")
        files = [base] if base.is_file() else self._walk(base)
        hits: list[str] = []
        for p in files:
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(lines, 1):
                if rx.search(line):
                    hits.append(f"{p.relative_to(self.root)}:{n}:{line[:MAX_GREP_LINE]}")
                    if len(hits) >= MAX_GREP:
                        return "\n".join(hits) + f"\n[stopped at {MAX_GREP} matches]"
        return "\n".join(hits) or "[no matches]"

    def run_command(self, args: dict) -> str:
        """Runs `bash -c` in the root; output is capped, the process group is killed on timeout."""
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ToolError("command must be non-empty text")
        timeout = min(max(int(args.get("timeout") or DEFAULT_TIMEOUT_S), 1), MAX_TIMEOUT_S)
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
        )  # fmt: skip
        timed_out = False
        try:
            raw, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                raw, _ = proc.communicate(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                raw, _ = proc.communicate()
        out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw.decode("utf-8", "replace"))
        if len(out) > OUTPUT_HEAD + OUTPUT_TAIL:
            out = out[:OUTPUT_HEAD] + "\n[... output truncated ...]\n" + out[-OUTPUT_TAIL:]
        status = f"timed out after {timeout}s" if timed_out else f"exit code {proc.returncode}"
        return f"{out}\n[{status}]"
