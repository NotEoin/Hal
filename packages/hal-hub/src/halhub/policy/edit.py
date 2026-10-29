"""Durable allow rules for `hal policy allow`: the new `policy.toml` text with one rule added.

01-security "The approvals module" and REQ-SEC-28. Prompt answers never outlive their session;
a durable allow is a rule the user writes in `policy.toml`. `allow_rule` returns the edited
text and writes nothing: 06-core-cli shows the diff, asks for a typed `yes` on a terminal with
`HAL_HARNESS` unset, and writes the file atomically with mode 0600.

Design decisions:

- The edit is textual, so the user's comments and layout survive: a new `[tools.<tool>]`
  table is appended, or one `allow_args` or `domains` line inside the existing table is
  replaced. The stdlib reads TOML but cannot write it, and a re-serialised file would lose
  every comment.
- Anything the line editor cannot change safely (a value spread over several lines, a
  `[tools.<tool>.allow_args]` sub-table, the tool defined with dotted keys or inline) raises
  `PolicyEditError` asking for a hand edit, rather than guessing.
- `allow_args` values are exact: glob characters in a value are escaped for `fnmatch`, so an
  approved `report[1].txt` never matches `report1.txt`.
- `allow_args` is one AND of ORs, so merging two different multi-parameter calls into one
  table would also allow mixed combinations nobody approved. A merge is done only when both
  name the same single parameter; otherwise `PolicyEditError`.
- The result is checked with `load_policy` and, given the tool's `ToolMeta`, `check_rules`,
  so a rule that breaks a REQ-SEC-08 invariant (an allow for a `system` or `runs_code` tool)
  is refused here and not only when the hub next loads the file.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

from .load import POLICY_SCHEMA_VERSION, check_rules, load_policy
from .model import Tier, ToolMeta

POLICY_FILE_MODE = 0o600  # REQ-SEC-15: the loader refuses a policy file others can write
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")  # 04-hub tool names; a bare TOML key
_HEADER_RE = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(#.*)?$")
_GLOB_CHARS = re.compile(r"([*?\[])")
_CONTROL_LIMIT = 0x20  # TOML basic strings may not hold raw control characters
_DELETE = 0x7F
_BMP_LIMIT = 0xFFFF


class PolicyEditError(ValueError):
    """The rule cannot be added: a bad request, or a file the line editor cannot change safely."""


def allow_rule(
    path: Path,
    tool: str,
    *,
    args: Mapping[str, object] | None = None,
    domain: str | None = None,
    meta: ToolMeta | None = None,
) -> str:
    """The text of `path` (or a new file) with an `allow_args` or `domains` entry for `tool`.

    Give exactly one of `args` (the call's canonical arguments, matched exactly) or `domain`
    (a host that turns the tool's `ask` into `allow`). `meta`, when known, checks argument
    names and REQ-SEC-08. Raises `PolicyEditError`, or `halkit.config.ConfigError` when the
    edited file would not load.
    """
    if not _TOOL_NAME_RE.fullmatch(tool):
        raise PolicyEditError(f"{tool!r} is not a tool name")
    if (args is None) == (domain is None):
        raise PolicyEditError("give exactly one of args or domain")
    if meta is not None:
        _check_meta(tool, meta, args)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyEditError(f"{path} is not valid TOML: {exc}") from exc
    if "schema_version" not in data:  # the loader refuses a non-empty file without it
        text = f"schema_version = {POLICY_SCHEMA_VERSION}\n{text}"
    existing = _table(data, tool)
    if args is not None:
        key, value = "allow_args", _merge_args(existing.get("allow_args"), args)
        line = f"allow_args = {_inline_table(value)}"
    else:
        assert domain is not None  # one of the two, checked above
        key, value = "domains", _merge_domains(existing.get("domains"), domain)
        line = f"domains = {_array(value)}"
    edited = _set_key(text, tool, key, line, present=key in existing, known=bool(existing))
    _validate(edited, meta)
    return edited


def _check_meta(tool: str, meta: ToolMeta, args: Mapping[str, object] | None) -> None:
    if meta.name != tool:
        raise PolicyEditError(f"metadata is for {meta.name}, not {tool}")
    if meta.tier is Tier.SYSTEM or meta.runs_code:
        raise PolicyEditError(f"{tool} must always ask; no rule can allow it (REQ-SEC-08)")
    if args is not None:
        known = {p.name for p in meta.params}
        unknown = sorted(set(args) - known)
        if unknown:
            raise PolicyEditError(f"{tool} has no argument {', '.join(unknown)}")


def _table(data: Mapping[str, object], tool: str) -> Mapping[str, object]:
    tools = data.get("tools", {})
    if not isinstance(tools, Mapping):
        raise PolicyEditError("tools is not a table; edit policy.toml by hand")
    table = tools.get(tool, {})
    if not isinstance(table, Mapping):
        raise PolicyEditError(f"tools.{tool} is not a table; edit policy.toml by hand")
    return table


def _exact(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()  # as `decide`'s `args_match` compares it
    if isinstance(value, int | float | str):
        return _GLOB_CHARS.sub(r"[\1]", str(value))
    raise PolicyEditError(f"cannot match a {type(value).__name__} value exactly; edit policy.toml by hand")


def _merge_args(existing: object, args: Mapping[str, object]) -> dict[str, list[str]]:
    if not args:
        raise PolicyEditError("an allow_args rule needs at least one argument")
    new = {str(name): [_exact(value)] for name, value in sorted(args.items())}
    if existing is None:
        return new
    if not isinstance(existing, Mapping) or not all(isinstance(v, list) for v in existing.values()):
        raise PolicyEditError("allow_args is not a table of lists; edit policy.toml by hand")
    old = {str(k): [str(p) for p in v] for k, v in existing.items()}
    if all(set(new[k]) <= set(old.get(k, [])) for k in new) and set(old) == set(new):
        return old  # already covered: no change
    if set(old) != set(new) or len(new) != 1:
        raise PolicyEditError(
            "an allow_args rule already exists for other arguments; merging would also allow "
            "combinations nobody approved, so edit policy.toml by hand"
        )
    (name,) = new
    return {name: [*old[name], *(p for p in new[name] if p not in old[name])]}


def _merge_domains(existing: object, domain: str) -> list[str]:
    name = domain.strip().rstrip(".").lower()
    if not name:
        raise PolicyEditError("the domain is empty")
    if existing is None:
        return [name]
    if not isinstance(existing, list):
        raise PolicyEditError("domains is not a list; edit policy.toml by hand")
    old = [str(d) for d in existing]
    return old if name in old else [*old, name]


def _string(value: str) -> str:
    """`value` as a TOML basic string, ASCII only."""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif code < _CONTROL_LIMIT or code == _DELETE or code > _DELETE:
            out.append(f"\\u{code:04X}" if code <= _BMP_LIMIT else f"\\U{code:08X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _array(values: list[str]) -> str:
    return "[" + ", ".join(_string(v) for v in values) + "]"


def _inline_table(value: Mapping[str, list[str]]) -> str:
    return "{ " + ", ".join(f"{_string(k)} = {_array(v)}" for k, v in value.items()) + " }"


def _header_name(line: str) -> str | None:
    match = _HEADER_RE.match(line)
    if match is None or line.lstrip().startswith("[["):
        return None
    return re.sub(r"\s*\.\s*", ".", match.group(1)).replace('"', "")


def _set_key(text: str, tool: str, key: str, line: str, *, present: bool, known: bool) -> str:
    lines = text.split("\n")
    header = f"tools.{tool}"
    names = [_header_name(raw) for raw in lines]
    if f"{header}.{key}" in names:
        raise PolicyEditError(f"[{header}.{key}] is a sub-table; edit policy.toml by hand")
    if header not in names:
        if known:
            raise PolicyEditError(f"tools.{tool} is written with dotted keys; edit policy.toml by hand")
        body = text if text.endswith("\n") or not text else text + "\n"
        return f"{body}\n[{header}]\n{line}\n"
    start = names.index(header) + 1
    end = next((i for i in range(start, len(lines)) if names[i] is not None), len(lines))
    if not present:
        last = max((i for i in range(start, end) if lines[i].strip()), default=start - 1)
        return "\n".join([*lines[: last + 1], line, *lines[last + 1 :]])
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    found = [i for i in range(start, end) if key_re.match(lines[i])]
    if len(found) != 1 or not _one_line_value(lines[found[0]]):
        raise PolicyEditError(f"tools.{tool}.{key} spans several lines; edit policy.toml by hand")
    lines[found[0]] = line
    return "\n".join(lines)


def _one_line_value(line: str) -> bool:
    try:
        tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return False
    return True


def _validate(text: str, meta: ToolMeta | None) -> None:
    """Loads `text` as a policy file (and checks `meta`'s invariants); raises `ConfigError`."""
    with tempfile.TemporaryDirectory(prefix="hal-policy-") as tmp:
        candidate = Path(tmp) / "policy.toml"
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, POLICY_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        policy = load_policy(candidate)
        if meta is not None:
            check_rules(policy, [meta])
