"""`hal configure --migrate`: reads an old `hal.conf` and adds the mapped keys to `config.toml`.

Design notes:
- `hal.conf` is parsed as `KEY="value"` lines only and never executed, because the file is a
  bash script's settings and may hold shell syntax.
- An existing key in `config.toml` is never changed; the change is a pure addition, shown as a
  unified diff before anything is written (REQ-CLI-84).
- `[jobs].jobhunt_root` is never written: it has no default, so the person sets it themselves.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from pathlib import Path

from halkit import files, timeutil

LINE = re.compile(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(?:"([^"]*)"|\'([^\']*)\'|(\S*))\s*(?:#.*)?$')

# old key -> (table path, new key)
MAPPING: dict[str, tuple[str, str]] = {
    "HAL_PROFILE": ("hal", "profile"),
    "HAL_MODEL": ("compat", "hal_model"),
    "HAL_WRITER_MODEL": ("compat", "writer_model"),
    "HAL_FAST_MODEL": ("compat", "fast_model"),
    "HAL_MODE": ("gateway", "mode"),
    "HAL_LLAMA_SERVER": ("gateway", "llama_server"),
    "HAL_MODELS_DIR": ("gateway", "models_dir"),
    "HAL_LLAMA_URL": ("gateway", "llama_url"),
    "HAL_MAX_OUTPUT_TOKENS": ("claude", "max_output_tokens"),
    "HAL_PERMISSION_MODE": ("claude", "permission_mode"),
    "HAL_ALLOW_NETWORK": ("claude", "allow_network"),
    "HAL_UNIVERSITY": ("claude.env", "HAL_UNIVERSITY"),
    "HAL_ANSWERER": ("claude.env", "HAL_ANSWERER"),
}
DROPPED = ("HAL_NUM_CTX", "HAL_BACKEND", "HAL_OLLAMA_URL")  # gateway modes replace them
BOOLS = {"HAL_ALLOW_NETWORK"}
INTS = {"HAL_MAX_OUTPUT_TOKENS"}


def parse_conf(text: str) -> dict[str, str]:
    """The `KEY="value"` lines of a `hal.conf`; every other line is ignored."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        m = LINE.match(raw)
        if m:
            out[m.group(1)] = next(g for g in m.groups()[1:] if g is not None)
    return out


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lookup(data: dict, table: str) -> dict | None:
    node: object = data
    for part in table.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def _add(text: str, data: dict, table: str, key: str, value: object) -> tuple[str, bool]:
    """Adds `key` to `table` unless it exists; returns the new text and whether it changed."""
    node = _lookup(data, table)
    if node is not None and key in node:
        return text, False
    line = f"{key} = {_toml(value)}"
    header = re.compile(rf"^\[{re.escape(table)}\]\s*$", re.M)
    if header.search(text):
        return header.sub(lambda m: m.group(0) + "\n" + line, text, count=1), True
    return text.rstrip("\n") + f"\n\n[{table}]\n{line}\n", True


def migrate_text(text: str, conf: dict[str, str], conf_path: str) -> tuple[str, list[str]]:
    """The new `config.toml` text and a note for each key that was skipped or dropped."""
    data = tomllib.loads(text) if text.strip() else {}
    notes: list[str] = []
    for old, (table, key) in MAPPING.items():
        if old not in conf:
            continue
        raw = conf[old]
        value: object = raw
        if old in BOOLS:
            value = raw.strip().lower() in ("1", "true", "yes", "on")
        elif old in INTS:
            if not raw.isdigit():
                notes.append(f"{old}={raw!r} is not a number; skipped")
                continue
            value = int(raw)
        text, changed = _add(text, data, table, key, value)
        if not changed:
            notes.append(f"[{table}].{key} already set; {old} left out")
    notes += [f"{old} dropped (the gateway modes replace it)" for old in DROPPED if old in conf]
    compat = _lookup(data, "compat") or {}
    if "hal_conf" not in compat:
        text, _ = _add(text, data, "compat", "hal_conf", [conf_path])
    else:
        notes.append("[compat].hal_conf already set; the path was not appended")
    return text, notes


def diff(old: str, new: str, name: str) -> str:
    """A unified diff of `old` to `new`."""
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), name, name + " (new)"))


def apply(config_path: Path, new_text: str) -> Path:
    """Writes `new_text` atomically with mode 0600 after saving `config.toml.bak.<UTC stamp>`."""
    stamp = timeutil.utc_now().strftime("%Y%m%dT%H%M%SZ")
    backup = config_path.with_name(f"{config_path.name}.bak.{stamp}")
    if config_path.exists():
        files.write_atomic(backup, config_path.read_text(encoding="utf-8"))
    files.write_atomic(config_path, new_text)
    return backup
