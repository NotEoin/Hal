"""Tool and argument name aliases, and coercion of argument values to the schema types.

Small models call `read_file` for `read` or send `file_path` for `path`. The agent is the only
place that repairs near-miss names (the broker applies only the aliases a manifest declares), and
an alias applies only when its target tool is offered. Repair never widens what a model can do.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from collections.abc import Mapping

PREFIXES = ("functions.", "default_api.", "tools.", "tool.", "mcp.")
CONFIRM_KEYS = ("_confirmed", "confirmed", "confirm")
CHECKBOX = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*\[[ xX~>]\]")


def _table(rows: dict[str, str]) -> dict[str, str]:
    return {alias: target for target, names in rows.items() for alias in names.split()}


TOOL_ALIASES = _table(
    {
        "run": "bash shell sh run_command run_shell_command execute execute_command exec "
        "exec_shell_command terminal cmd command shell_exec container.exec",
        "read": "read_file view view_file open open_file cat file_read readfile get_file",
        "edit": "str_replace str_replace_editor str_replace_based_edit_tool edit_file replace "
        "replace_in_file search_replace apply_edit file_edit multiedit",
        "write": "write_file create_file create save_file file_write",
        "grep": "search search_code grep_search rg ripgrep find_in_files code_search git_grep",
        "glob": "find_files list_files ls list_dir list_directory file_glob_search find",
        "finish": "submit submit_patch done complete task_complete attempt_completion final_answer",
        "todo": "update_plan plan todo_write todowrite set_plan",
        "hal_find_tools": "find_tools search_tools tool_search hal_search",
    }
)
EDITOR_COMMANDS = {"view": "read", "create": "write", "str_replace": "edit"}

ARG_ALIASES: dict[str, dict[str, str]] = {
    "read": _table(
        {
            "path": "file_path filepath filename file",
            "start_line": "offset start line_start from_line line",
            "end_line": "end line_end to_line",
        }
    ),  # fmt: skip
    "edit": _table(
        {
            "path": "file_path file",
            "old_string": "old_str old search find old_text target",
            "new_string": "new_str new replace replacement new_text",
        }
    ),  # fmt: skip
    "write": _table({"path": "file_path file", "content": "file_text text contents data file_content code"}),
    "run": _table(
        {
            "command": "cmd script code bash_command shell_command commands",
            "timeout": "timeout_s timeout_seconds",
        }
    ),  # fmt: skip
    "grep": _table(
        {
            "pattern": "query regex search text pattern_text",
            "glob": "include file_pattern files glob_pattern",
            "path": "dir directory root cwd",
        }
    ),  # fmt: skip
    "glob": _table({"pattern": "glob query name file_pattern", "path": "dir directory root"}),
    "todo": _table({"plan": "todos items steps list"}),
    "finish": _table({"summary": "message result answer text"}),
}
LIMIT_KEYS = ("limit", "lines", "count")


def resolve_name(name: str, offered: Mapping[str, object], args: dict | None = None) -> str | None:
    """Returns the offered tool `name` stands for, or None."""
    if name in offered:
        return name
    lower = name.strip().lower()
    candidates = [lower]
    for prefix in PREFIXES:
        if lower.startswith(prefix):
            candidates.append(lower[len(prefix) :])
            break
    by_lower = {k.lower(): k for k in offered}
    for cand in candidates:
        if cand in by_lower:
            return by_lower[cand]
    for cand in candidates:
        if cand in ("str_replace_editor", "str_replace_based_edit_tool") and args:
            target = EDITOR_COMMANDS.get(str(args.get("command")), "edit")
            if target in offered:
                return target
        target = TOOL_ALIASES.get(cand)
        if target in offered:
            return target
    return None


def parse_args(raw: object) -> dict | None:
    """Parses an argument string (or passes a dict through); None when nothing parses to a dict."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return {}
    attempts = [text, re.sub(r",\s*([}\]])", r"\1", text)]
    for attempt in attempts:
        try:
            value = json.loads(attempt)
        except ValueError:
            continue
        if isinstance(value, str):
            return parse_args(value)
        if isinstance(value, dict):
            return value
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _to_str(value: object, tool: str, key: str) -> object:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, list):
        items = [str(v) for v in value]
        if tool == "run" and key == "command":
            whole = all(re.search(r"\s", i) or i in ("ls", "pwd") for i in items)
            return " && ".join(items) if whole else shlex.join(items)
        if tool == "todo" and key == "plan":
            return "\n".join(i if CHECKBOX.match(i) else f"- [ ] {i}" for i in items)
        return "\n".join(items)
    return value


def _to_int(value: object) -> object:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value)
    return value


def repair_args(
    tool: str, args: dict, properties: Mapping[str, Mapping], required: tuple[str, ...]
) -> tuple[dict, list[str]]:
    """Applies argument aliases and coercion; returns (arguments, notes for the model)."""
    notes: list[str] = []
    out = {k: v for k, v in args.items() if k not in CONFIRM_KEYS and v is not None}
    for alias, target in ARG_ALIASES.get(tool, {}).items():
        if alias in out and alias not in properties and target not in out:
            out[target] = out.pop(alias)
    if tool == "read" and "end_line" not in out:
        for key in LIMIT_KEYS:
            if key in out:
                start = _to_int(out.get("start_line", 1))
                count = _to_int(out.pop(key))
                if isinstance(start, int) and isinstance(count, int):
                    out["end_line"] = start + count - 1
    for key in list(out):
        if not properties:
            break  # a schema that declares nothing accepts anything
        if key not in properties:
            notes.append(f"[hal] ignored unknown argument {key!r}")
            del out[key]
            continue
        kind = properties[key].get("type")
        if kind == "string":
            out[key] = _to_str(out[key], tool, key)
        elif kind == "integer":
            out[key] = _to_int(out[key])
    return out, notes
