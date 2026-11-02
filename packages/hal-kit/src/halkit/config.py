"""Strict TOML config loading: typed tables, one precedence rule, errors that name file and key.

Every component declares a `Table` of typed `Key` entries and reads it with `load`. A broken
file, an unknown key or a bad value raises `ConfigError` instead of silently falling back to
defaults, which is what the old `halcore/config.py` did (REQ-SYS-06). Precedence for every
value, highest first, is the `cli` mapping, the key's environment variable, the file, the
default (REQ-SYS-07).

`load` reads the file on every call and keeps no cache, so a long-running daemon sees edits.
An unknown top-level table is not an error: the pack that owns it may be uninstalled.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import paths
from .files import write_atomic

log = logging.getLogger(__name__)

ENV_TRUE = frozenset({"1", "true", "yes"})
ENV_FALSE = frozenset({"0", "false", "no"})
LOOPBACK_NAMES = frozenset({"localhost"})  # resolves to loopback on every supported OS


class ConfigError(Exception):
    """A config file or value is invalid; names the file, table, key and line when known."""

    def __init__(
        self,
        message: str,
        *,
        path: Path,
        table: str = "",
        key: str | None = None,
        line: int | None = None,
    ) -> None:
        self.path = path
        self.table = table
        self.key = key
        self.line = line
        where = f"{path}:{line}" if line else str(path)
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True)
class Key:
    """One setting of a table."""

    name: str
    type: type | tuple[type, ...]  # str, int, float, bool, list, dict
    default: object
    env: str | None = None  # environment variable that overrides the file value
    choices: tuple[object, ...] | None = None
    check: Callable[[object], str | None] | None = None  # returns an error message or None
    doc: str = ""  # one line; `hal configure --show` prints it
    deprecated_alias: str | None = None  # old key name, accepted with one warning


@dataclass(frozen=True)
class Table:
    """A named group of keys, owned by one distribution."""

    name: str  # dotted path in the file, e.g. "gateway" or "agent.budgets"
    keys: tuple[Key, ...]
    owner: str  # distribution name, for error messages
    open: bool = False  # True only for tables of user-named entries; unknown keys pass through


_registry: dict[str, Table] = {}
_warned_aliases: set[tuple[str, str]] = set()


def register(table: Table) -> Table:
    """Records `table` for `known_tables()` and returns it.

    Registering the same table twice is fine; a different table under a used name raises
    `ValueError`, because two owners for one section is a bug.
    """
    existing = _registry.get(table.name)
    if existing is not None and existing != table:
        raise ValueError(f"config table [{table.name}] is already registered by {existing.owner}")
    _registry[table.name] = table
    return table


def known_tables() -> list[Table]:
    """Every registered table, sorted by name."""
    return [_registry[name] for name in sorted(_registry)]


def _toml_line(exc: tomllib.TOMLDecodeError) -> int | None:
    line = getattr(exc, "lineno", None)  # Python 3.14 and later
    if isinstance(line, int):
        return line
    found = re.search(r"line (\d+)", str(exc))
    return int(found.group(1)) if found else None


def read_toml(path: Path) -> dict[str, object]:
    """Parses `path`; a missing file gives `{}`.

    Raises `ConfigError` with the line number for malformed TOML and for an unreadable file.
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"cannot read: {exc.strerror or exc}", path=Path(path)) from exc
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"not valid UTF-8: {exc}", path=Path(path)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed TOML: {exc}", path=Path(path), line=_toml_line(exc)) from exc


def _section(data: Mapping[str, object], dotted: str, path: Path) -> Mapping[str, object]:
    node: object = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return {}
        node = node[part]
    if not isinstance(node, Mapping):
        raise ConfigError(f"[{dotted}] must be a table", path=path, table=dotted)
    return node


def _types(expected: type | tuple[type, ...]) -> tuple[type, ...]:
    return expected if isinstance(expected, tuple) else (expected,)


def _type_names(expected: type | tuple[type, ...]) -> str:
    return " or ".join(t.__name__ for t in _types(expected))


def _matches(value: object, expected: type | tuple[type, ...]) -> bool:
    types = _types(expected)
    if isinstance(value, bool):  # bool is an int subclass; only accept it where asked for
        return bool in types
    if isinstance(value, int) and float in types:
        return True
    return isinstance(value, types)


def _parse_env(text: str, expected: type | tuple[type, ...]) -> object:
    types = _types(expected)
    lowered = text.strip().lower()
    for kind in types:
        if kind is bool and lowered in ENV_TRUE | ENV_FALSE:
            return lowered in ENV_TRUE
        if kind is int and re.fullmatch(r"[+-]?\d+", lowered):
            return int(lowered)
        if kind is float:
            try:
                return float(lowered)
            except ValueError:
                continue
        if kind in (list, dict):
            try:
                return json.loads(text)
            except ValueError:
                continue
        if kind is str:
            return text
    return text  # left as text so validation reports the type mismatch


def _validate(table: Table, key: Key, value: object, source: str, path: Path) -> object:
    def fail(message: str) -> ConfigError:
        return ConfigError(
            f"[{table.name}] {key.name} ({source}): {message}", path=path, table=table.name, key=key.name
        )

    if not _matches(value, key.type):
        raise fail(f"expected {_type_names(key.type)}, got {type(value).__name__} {value!r}")
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and float in _types(key.type)
        and int not in _types(key.type)
    ):
        value = float(value)
    if key.choices is not None and value not in key.choices:
        raise fail(f"{value!r} is not one of {list(key.choices)}")
    if key.check is not None:
        problem = key.check(value)
        if problem:
            raise fail(problem)
    return value


def load(
    table: Table,
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    cli: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """Returns the settings of `table`: `cli`, then `environ`, then the file, then defaults.

    Raises `ConfigError` for malformed TOML, an unknown key (unless `table.open`), a value of
    the wrong type, outside `choices`, or rejected by `check`.
    """
    path = Path(path) if path is not None else paths.config_file()
    env = os.environ if environ is None else environ
    section = _section(read_toml(path), table.name, path)
    by_name = {key.name: key for key in table.keys}
    aliases = {key.deprecated_alias: key for key in table.keys if key.deprecated_alias}
    extras = {}

    from_file: dict[str, object] = {}
    for name, value in section.items():
        if name in by_name:
            from_file[name] = value
        elif name in aliases:
            if (table.name, name) not in _warned_aliases:
                _warned_aliases.add((table.name, name))
                log.warning("%s: [%s] %s is deprecated; use %s", path, table.name, name, aliases[name].name)
            from_file.setdefault(aliases[name].name, value)
        elif table.open:
            extras[name] = value
        else:
            raise ConfigError(
                f"[{table.name}] has no key {name!r} (owner {table.owner})",
                path=path,
                table=table.name,
                key=name,
            )

    unknown_cli = set(cli or {}) - set(by_name)
    if unknown_cli:
        raise ConfigError(
            f"[{table.name}] cli has no key {sorted(unknown_cli)[0]!r}",
            path=path,
            table=table.name,
            key=sorted(unknown_cli)[0],
        )

    result: dict[str, object] = dict(extras)
    for key in table.keys:
        if cli is not None and key.name in cli:
            result[key.name] = _validate(table, key, cli[key.name], "command line", path)
        elif key.env and env.get(key.env) not in (None, ""):
            parsed = _parse_env(env[key.env], key.type)
            result[key.name] = _validate(table, key, parsed, f"environment {key.env}", path)
        elif key.name in from_file:
            result[key.name] = _validate(table, key, from_file[key.name], "file", path)
        else:
            result[key.name] = key.default
    return result


def _loopback_url(value: object) -> str | None:
    host = urlsplit(str(value)).hostname
    if host is None:
        return f"{value!r} is not a URL with a host"
    if host in LOOPBACK_NAMES:
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return f"host {host!r} is not loopback; Hal binds and talks to loopback only (REQ-SYS-09)"


HAL_TABLE = register(
    Table(
        name="hal",
        owner="hal-kit",
        keys=(
            Key("vault", str, "~/notes", doc="Obsidian vault the memory tools read"),
            Key("chat_model", str, "gemma4:12b-it-qat", doc="Model for chat and assistant turns"),
            Key("fast_model", str, "qwen3.5:4b", doc="Small model for classifying and titling"),
            Key("code_model", str, "", doc="Model for `hal code`; empty means chat_model"),
            Key("writer_model", str, "", doc="Model for prose; empty means chat_model"),
            Key("embed_model", str, "embeddinggemma", doc="Embedding model for vault search"),
            Key("embed_on_cpu", bool, True, doc="Keep the vault index off the GPU"),
            Key("vision_model", str, "qwen3-vl:8b", doc="Model for screen reading"),
            Key(
                "ollama_url",
                str,
                "http://127.0.0.1:11434",
                check=_loopback_url,
                doc="Ollama upstream for the gateway; loopback only",
            ),
            Key("user_name", str, "", doc="How Hal addresses the user"),
            Key("profile", str, "", doc="Hardware profile last chosen by `hal configure` (informational)"),
        ),
    )
)


def hal() -> Mapping[str, object]:
    """The `[hal]` table with defaults applied, read from the current config file."""
    return load(HAL_TABLE)


def write_private(path: Path, text: str) -> None:
    """Writes `text` to `path` atomically with mode 0600, for files that hold secrets."""
    write_atomic(path, text, mode=0o600)
