"""Model profiles: per-family sampling, context and tool-call repair settings.

The shipped catalogue and the user's `model-profiles.toml` share one strict schema. An unknown
key, a wrong type or an unknown parser fails loudly with the file and the key, because a typo
would otherwise silently fall back to defaults. The gateway's `meta.hal_profile` picks an entry
by family, so role aliases whose backend model is a local choice still get the right profile.
"""

from __future__ import annotations

import fnmatch
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from halagent.repair.leaks import PARSERS

SHIPPED = Path(__file__).with_name("model-profiles.toml")
SCHEMA = 1
PASS_THROUGH_KEYS = ("cache_prompt", "id_slot")
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty")
# key -> (allowed types, enum or None)
KEYS: dict[str, tuple[tuple[type, ...], tuple[str, ...] | None]] = {
    "context": ((int,), None),
    "max_output": ((int,), None),
    "temperature": ((int, float), None),
    "top_p": ((int, float), None),
    "top_k": ((int,), None),
    "min_p": ((int, float), None),
    "presence_penalty": ((int, float), None),
    "repeat_penalty": ((int, float), None),
    "thinking": ((str,), None),
    "thinking_budget": ((int,), None),
    "reasoning_replay": ((str,), ("none", "tool_turns", "all")),
    "tool_format": ((str,), ("native", "prompt", "auto")),
    "leak_parsers": ((list,), None),
    "parallel_tool_calls": ((bool,), None),
    "first_token_timeout_s": ((int,), None),
    "system_role": ((str,), ("system", "developer", "user")),
    "strict_alternation": ((bool,), None),
    "max_tools_hint": ((int,), None),
    "stop": ((list,), None),
    "tool_set": ((str,), ("full", "compact")),
    "tool_constraint": ((str,), ("none", "grammar", "json_schema")),
    "cache_prompt": ((bool,), None),
    "vision": ((bool,), None),
    "id_slot": ((int,), None),
}
ENTRY_KEYS = ("match", "family", "notes", "evidence")


class ProfileError(ValueError):
    """A profile file is invalid; the message names the file and the key."""


@dataclass(frozen=True)
class Profile:
    """The resolved profile of one model; `sources` maps each key to where its value came from."""

    model: str
    family: str
    values: Mapping[str, Any]
    sources: Mapping[str, str]

    def request_extras(self) -> dict[str, Any]:
        """Sampling keys (and stop strings) for the request body."""
        out = {k: self.values[k] for k in SAMPLING_KEYS if k in self.values}
        if self.values.get("stop"):
            out["stop"] = list(self.values["stop"])
        # llama.cpp server options; Ollama and the gateway ignore keys they do not know
        for key in PASS_THROUGH_KEYS:
            if key in self.values:
                out[key] = self.values[key]
        return out


def _check_values(table: Mapping[str, Any], where: str, extra: tuple[str, ...] = ()) -> None:
    for key, value in table.items():
        if key in extra:
            continue
        if key not in KEYS:
            raise ProfileError(f"{where}: unknown key {key!r}")
        types, enum = KEYS[key]
        if isinstance(value, bool) and bool not in types:
            raise ProfileError(f"{where}: {key} must be {types[0].__name__}")
        if not isinstance(value, types):
            raise ProfileError(f"{where}: {key} must be {types[0].__name__}")
        if enum and value not in enum:
            raise ProfileError(f"{where}: {key} must be one of {', '.join(enum)}")
    for key in ("leak_parsers", "stop"):
        if key in table and not all(isinstance(x, str) for x in table[key]):
            raise ProfileError(f"{where}: {key} must be a list of strings")
    for name in table.get("leak_parsers", ()):
        if name not in PARSERS:
            raise ProfileError(f"{where}: leak_parsers names unknown parser {name!r}")


def load_catalogue(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Reads and validates one catalogue file; returns its `[defaults]` and `[[model]]` entries."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f"{path}: {exc}") from exc
    if data.get("schema") != SCHEMA:
        raise ProfileError(f"{path}: schema must be {SCHEMA}")
    unknown = set(data) - {"schema", "defaults", "model"}
    if unknown:
        raise ProfileError(f"{path}: unknown table {sorted(unknown)[0]!r}")
    defaults = dict(data.get("defaults", {}))
    _check_values(defaults, f"{path} [defaults]")
    entries = list(data.get("model", []))
    for i, entry in enumerate(entries):
        where = f"{path} [[model]] #{i + 1}"
        match = entry.get("match")
        if not match or not isinstance(match, list) or not all(isinstance(m, str) for m in match):
            raise ProfileError(f"{where}: match must be a non-empty list of globs")
        if not isinstance(entry.get("family"), str) or not entry["family"]:
            raise ProfileError(f"{where}: family is required")
        _check_values(entry, where, ENTRY_KEYS)
    return defaults, entries


def _pick(entries: list[dict[str, Any]], model: str, family: str) -> dict[str, Any] | None:
    if family:
        by_family = next((e for e in entries if e["family"] == family), None)
        if by_family:
            return by_family
    low = model.lower()
    return next((e for e in entries if any(fnmatch.fnmatchcase(low, g.lower()) for g in e["match"])), None)


def resolve_profile(
    model: str,
    *,
    user_file: Path | None = None,
    hal_profile: str = "",
    overrides: Mapping[str, Any] | None = None,
    cli: Mapping[str, Any] | None = None,
) -> Profile:
    """Resolves `model`: defaults < shipped entry < user entry < `overrides` < `cli`.

    `hal_profile` is the gateway's `meta.hal_profile`; it selects an entry by family before globs.
    """
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}

    def apply(table: Mapping[str, Any], source: str) -> None:
        for k, v in table.items():
            if k in KEYS:
                values[k], sources[k] = v, source

    shipped_defaults, shipped = load_catalogue(SHIPPED)
    apply(shipped_defaults, "defaults")
    user_defaults: dict[str, Any] = {}
    user: list[dict[str, Any]] = []
    if user_file is not None and user_file.is_file():
        user_defaults, user = load_catalogue(user_file)
    apply(user_defaults, "user:defaults")
    family = ""
    for entries, tag in ((shipped, "shipped"), (user, "user")):
        entry = _pick(entries, model, hal_profile)
        if entry:
            family = entry["family"]
            apply(entry, f"{tag}:{family}")
    for table, tag in ((overrides or {}, "config"), (cli or {}, "cli")):
        _check_values(table, tag)
        apply(table, tag)
    return Profile(model, family or "default", values, sources)
