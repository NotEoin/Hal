"""Parse `hal-module.toml` manifests, v2 strictly and v1 with warnings, into frozen specs.

04-hub.md, "Manifest v2", is the schema; this module is its one implementation (RESOLUTIONS
C1 makes 04 the schema owner). Design decisions:

- v2 is strict: an unknown key, a wrong type, a missing required key or a value outside its
  allowed set raises `ManifestError` naming the file, the dotted table path and the key
  (REQ-HUB-01), because a typo in a security flag must never load as its default.
- v1 loads fail closed: every tool becomes tier `act`, `reviewable = false`, whatever
  `confirm` says (REQ-HUB-02, RESOLUTIONS B6). Unknown v1 keys warn and are ignored.
- There is no per-tool `default` verdict key: a pack can never relax policy for itself
  (RESOLUTIONS C1). `default` inside a tool table is therefore an unknown key.
- Parameters named `_*`, `confirm` or `confirmed` are refused in v1 and v2 alike (REQ-HUB-05
  asks it of v2; none of the 33 live v1 manifests uses them, and such a name could only ever
  be an attempt to look like an approval).
- Placeholders (`{root}`, `{python}`, `{state}`, `{data}`) are substituted at load, so a
  `ModuleSpec` holds final argv lists; `{state}` and `{data}` read `halkit.paths` then.
- Nothing here imports pack code or touches the network.
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from halkit import paths

from .policy.model import ParamSpec as PolicyParamSpec
from .policy.model import Tier as PolicyTier
from .policy.model import ToolMeta

log = logging.getLogger(__name__)

MANIFEST_NAME = "hal-module.toml"
SCHEMA_V2 = 2
BUILTIN_MODULE = "hub"  # the hub's own module; its tools use the reserved prefix below
BUILTIN_PREFIX = "hal_"
RESERVED_MODULE_NAMES = frozenset({"hal"})  # a module "hal" would own the reserved prefix
RESERVED_PACK_PREFIX = "mcp-"  # host MCP servers' pack names (RESOLUTIONS E6)

TIERS = ("read", "write", "act", "send", "system")
PARAM_TYPES = ("string", "integer", "number", "boolean")
PARAM_FORMATS = ("text", "path", "url", "host", "email", "phone", "command", "json")
UNIT_SUFFIXES = (".service", ".timer", ".target", ".path")
RESERVED_PARAM_NAMES = frozenset({"confirm", "confirmed"})

MODULE_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,31}$")
PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
TOOL_SUFFIX_RE = r"[a-z0-9]+(_[a-z0-9]+)*"
PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FUNCTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ANY_TOOL_RE = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9]+(_[a-z0-9]+)*$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
INSTALL_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
REQUIREMENT_RE = re.compile(r"^([a-z][a-z0-9]{0,31})\s*(.*)$")
SPECIFIER_RE = re.compile(r"^(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)$")
HOST_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
PLACEHOLDER_RE = re.compile(r"\{([a-z]+)\}")
PLACEHOLDERS = frozenset({"root", "python", "state", "data"})

MAX_TOOL_NAME = 64  # the MCP tool-name limit most clients enforce
MAX_SUMMARY = 200
MAX_DESCRIPTION = 2000
DEFAULT_MAX_LENGTH = 65_536  # 01-security's default string cap
DEFAULT_TOOL_TIMEOUT = 60.0
DEFAULT_INSTALL_TIMEOUT = 600.0
V1_INSTALL_TIMEOUT = 3600.0  # v1 `install` steps fetched whole models; keep their headroom
MAX_INSTALL_TIMEOUT = 7200.0
MIN_TOOL_TIMEOUT = 1.0
MAX_WORKERS_LIMIT = 8


@dataclass(frozen=True)
class ManifestDefaults:
    """Defaults and caps that come from `[hub.runner]` (04-hub "Configuration")."""

    max_timeout: float = 7200.0  # the largest declared timeout today (`models_pull`)
    max_result_chars: int = 20_000
    startup_timeout: float = 20.0
    max_workers: int = 2


DEFAULTS = ManifestDefaults()

Tier = Literal["read", "write", "act", "send", "system"]
ParamType = Literal["string", "integer", "number", "boolean"]
ParamFormat = Literal["text", "path", "url", "host", "email", "phone", "command", "json"]
Source = Literal["entry_point", "installed", "path", "builtin"]


class ManifestError(ValueError):
    """A manifest breaks the schema; `key` is the dotted path, such as `tools[3].params.when.type`."""

    def __init__(self, path: Path, key: str | None, message: str) -> None:
        self.path = Path(path)
        self.key = key
        self.message = message
        where = f"{self.path}: {key}: " if key else f"{self.path}: "
        super().__init__(where + message)


@dataclass(frozen=True)
class ParamSpec:
    """One declared tool parameter (04-hub "`[tools.params.<name>]`")."""

    name: str
    type: ParamType = "string"
    description: str = ""
    required: bool = False
    enum: tuple[str | int | float, ...] | None = None
    default: str | int | float | bool | None = None
    list: bool = False
    item_type: Literal["string", "integer"] = "string"
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = DEFAULT_MAX_LENGTH
    aliases: tuple[str, ...] = ()
    format: ParamFormat = "text"
    access: Literal["read", "write"] = "read"
    content: bool = False
    v1_object: bool = False  # v1 "object": the worker gets the parsed JSON


@dataclass(frozen=True)
class RoutineSpec:
    """Names the parameters that carry a routine's id and its JSON step list (REQ-HUB-64)."""

    id_param: str
    steps_param: str


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool, as declared, with v1 defaults already applied."""

    name: str
    module: str
    pack: str
    function: str
    description: str
    tier: Tier
    tier_inferred: bool
    title: str | None = None
    network: bool = False
    untrusted_output: bool = False
    reviewable: bool = False
    reversible: bool = False
    runs_code: bool = False
    routine: RoutineSpec | None = None
    timeout: float = DEFAULT_TOOL_TIMEOUT
    always_offer: bool = False
    keywords: tuple[str, ...] = ()
    max_output: int = DEFAULTS.max_result_chars
    params: tuple[ParamSpec, ...] = ()

    def param(self, name: str) -> ParamSpec | None:
        """The parameter called `name`, or None."""
        return next((p for p in self.params if p.name == name), None)

    def input_schema(self) -> dict[str, object]:
        """Returns the JSON Schema object for the tool's arguments (flat, additionalProperties false)."""
        properties: dict[str, object] = {}
        for p in self.params:
            prop: dict[str, object] = {"type": p.type}
            description = p.description
            if p.list:
                kind = "integers" if p.item_type == "integer" else "values"
                description = f"{description} (comma-separated {kind})".strip()
            if description:
                prop["description"] = description
            if p.enum is not None:
                prop["enum"] = list(p.enum)
            if p.default is not None:
                prop["default"] = p.default
            if p.minimum is not None:
                prop["minimum"] = p.minimum
            if p.maximum is not None:
                prop["maximum"] = p.maximum
            if p.type == "string":
                prop["maxLength"] = p.max_length
            properties[p.name] = prop
        return {
            "type": "object",
            "properties": properties,
            "required": [p.name for p in self.params if p.required],
            "additionalProperties": False,
        }

    def meta(self, module: ModuleSpec) -> ToolMeta:
        """Returns 01-security's `ToolMeta` for this tool: pack, pack_version, tier, flags,
        domains and per-parameter type, required, enum, max_length, format, access, content."""
        params = tuple(
            PolicyParamSpec(
                name=p.name,
                type=p.type,
                required=p.required,
                enum=p.enum,
                max_length=p.max_length,
                format=p.format,
                access=p.access,
                content=p.content,
            )
            for p in self.params
        )
        return ToolMeta(
            name=self.name,
            pack=self.pack,
            pack_version=module.version,
            tier=PolicyTier(self.tier),
            params=params,
            network=self.network,
            untrusted_output=self.untrusted_output,
            reviewable=self.reviewable,
            reversible=self.reversible,
            runs_code=self.runs_code,
            domains=module.domains,
        )


@dataclass(frozen=True)
class SecretSpec:
    """A secret a module's tools need (01-security, "Secrets")."""

    name: str
    config_key: str
    default_path: str


@dataclass(frozen=True)
class InstallStep:
    """One declared install step; run only after a typed confirmation (REQ-HUB-59)."""

    id: str
    summary: str
    run: tuple[str, ...]
    timeout: float = DEFAULT_INSTALL_TIMEOUT
    network: bool = False
    cwd: Literal["root", "state"] = "root"


@dataclass(frozen=True)
class UnitSpec:
    """A systemd user unit shipped with a module; `file` is absolute."""

    file: Path
    gpu: bool = False
    enable: bool = True


@dataclass(frozen=True)
class ModuleSpec:
    """Everything one manifest declares, with placeholders substituted and defaults applied."""

    name: str
    pack: str
    summary: str
    version: str
    schema: Literal[1, 2]
    root: Path  # directory holding the manifest
    manifest: Path
    source: Source
    distribution: str | None
    python: str | None
    exec: tuple[str, ...] | None
    protocol: Literal["jsonl", "oneshot"]
    interpreter: tuple[str, ...] | None  # resolved per "Interpreter"; None means the hub's
    inprocess: bool
    requires: tuple[str, ...]
    calls: tuple[str, ...]
    requires_hal: str | None
    env: tuple[str, ...]
    domains: tuple[str, ...]
    secrets: tuple[SecretSpec, ...]
    startup_timeout: float
    max_workers: int
    keywords: tuple[str, ...]
    tools: tuple[ToolSpec, ...]
    install: tuple[InstallStep, ...]
    units: tuple[UnitSpec, ...]
    desktop: tuple[Path, ...]
    warnings: tuple[str, ...]
    declared_pack: str | None = None  # `[module] pack` as written; the registry settles `pack`

    def state_dir(self) -> Path:
        """`state_dir()/modules/<name>`: the v2 worker's working directory (D16)."""
        return module_state_dir(self.name)


def module_state_dir(name: str) -> Path:
    """`state_dir()/modules/<name>`."""
    return paths.state_dir() / "modules" / name


def module_data_dir(name: str) -> Path:
    """`data_dir()/modules/<name>`."""
    return paths.data_dir() / "modules" / name


# ---- version specifiers ----------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, ...]:
    """`"0.1.2"` -> `(0, 1, 2)`; a dev or pre-release suffix is ignored. Raises `ValueError`."""
    match = re.match(r"^(\d+(?:\.\d+)*)", text.strip())
    if not match:
        raise ValueError(f"not a dotted version: {text!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def parse_specifier(text: str) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Parses `">=0.1,<0.2"` into `((">=", (0, 1)), ("<", (0, 2)))`; raises `ValueError`."""
    clauses = []
    for part in text.split(","):
        match = SPECIFIER_RE.match(part.strip())
        if not match:
            raise ValueError(f"bad version clause {part.strip()!r}; use >=, >, <=, <, == or != and a number")
        clauses.append((match.group(1), parse_version(match.group(2))))
    return tuple(clauses)


def _padded(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def version_satisfies(version: str, specifier: str) -> bool:
    """True when `version` meets every clause of `specifier` (an empty specifier always does)."""
    if not specifier.strip():
        return True
    have = parse_version(version)
    for op, want in parse_specifier(specifier):
        left, right = _padded(have, want)
        ok = {
            ">=": left >= right,
            ">": left > right,
            "<=": left <= right,
            "<": left < right,
            "==": left == right,
            "!=": left != right,
        }[op]
        if not ok:
            return False
    return True


def parse_requirement(text: str) -> tuple[str, str]:
    """`"memory>=0.1"` -> `("memory", ">=0.1")`; raises `ValueError` on bad syntax."""
    match = REQUIREMENT_RE.match(text.strip())
    if not match:
        raise ValueError(f"bad requirement {text!r}; use a module name and optional version clauses")
    name, rest = match.group(1), match.group(2).strip()
    if rest:
        parse_specifier(rest)
    return name, rest


# ---- the parser ------------------------------------------------------------------------------


class _Reader:
    """Typed access to one manifest's tables, raising `ManifestError` with dotted keys."""

    def __init__(self, path: Path, schema: int) -> None:
        self.path = path
        self.schema = schema
        self.warnings: list[str] = []

    @property
    def strict(self) -> bool:
        return self.schema == SCHEMA_V2

    def fail(self, key: str | None, message: str) -> ManifestError:
        return ManifestError(self.path, key, message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        log.warning("%s: %s", self.path, message)

    def check_keys(self, table: Mapping[str, object], where: str, allowed: Sequence[str]) -> None:
        for key in table:
            if key in allowed:
                continue
            dotted = f"{where}.{key}" if where else key
            if self.strict:
                raise self.fail(dotted, f"unknown key; allowed: {', '.join(sorted(allowed))}")
            self.warn(f"{dotted}: unknown key ignored")

    def table(self, data: Mapping[str, object], key: str, where: str, *, required: bool = False) -> dict:
        dotted = f"{where}.{key}" if where else key
        if key not in data:
            if required:
                raise self.fail(dotted, "required table is missing")
            return {}
        value = data[key]
        if not isinstance(value, dict):
            raise self.fail(dotted, f"must be a table, got {type(value).__name__}")
        return value

    def tables(self, data: Mapping[str, object], key: str, where: str) -> list[dict]:
        dotted = f"{where}.{key}" if where else key
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise self.fail(dotted, "must be an array of tables")
        return value

    def get(
        self,
        data: Mapping[str, object],
        key: str,
        where: str,
        kind: type | tuple[type, ...],
        default: object = None,
        *,
        required: bool = False,
    ) -> object:
        dotted = f"{where}.{key}" if where else key
        if key not in data:
            if required:
                raise self.fail(dotted, "required key is missing")
            return default
        value = data[key]
        kinds = kind if isinstance(kind, tuple) else (kind,)
        if isinstance(value, bool) and bool not in kinds:
            raise self.fail(dotted, f"expected {_names(kinds)}, got boolean")
        if isinstance(value, int) and not isinstance(value, bool) and float in kinds and int not in kinds:
            value = float(value)
        if not isinstance(value, kinds):
            raise self.fail(dotted, f"expected {_names(kinds)}, got {type(value).__name__}")
        return value

    def strings(self, data: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
        dotted = f"{where}.{key}" if where else key
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise self.fail(dotted, "expected an array of strings")
        return tuple(value)

    def choice(
        self, data: Mapping[str, object], key: str, where: str, choices: Sequence[str], default: str
    ) -> str:
        value = self.get(data, key, where, str, default)
        if value not in choices:
            dotted = f"{where}.{key}" if where else key
            raise self.fail(dotted, f"{value!r} is not one of {', '.join(choices)}")
        return str(value)

    def keywords(self, data: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
        value = data.get(key, "")
        if isinstance(value, str):
            return tuple(value.split())
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return tuple(word for item in value for word in item.split())
        dotted = f"{where}.{key}" if where else key
        raise self.fail(dotted, "expected a string or an array of strings")


def _names(kinds: tuple[type, ...]) -> str:
    labels = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "table"}
    return " or ".join(labels.get(k, k.__name__) for k in kinds)


MODULE_KEYS_V2 = (
    "name", "pack", "summary", "version", "python", "exec", "protocol", "interpreter", "inprocess",
    "requires", "calls", "requires_hal", "env", "domains", "secrets", "startup_timeout",
    "max_workers", "keywords",
)  # fmt: skip
MODULE_KEYS_V1 = ("name", "summary", "python", "exec", "install", "keywords")
TOOL_KEYS_V2 = (
    "name", "function", "title", "description", "tier", "network", "untrusted_output",
    "reviewable", "reversible", "runs_code", "routine", "timeout", "always_offer", "keywords",
    "max_output", "params",
)  # fmt: skip
TOOL_KEYS_V1 = ("name", "function", "description", "confirm", "timeout", "keywords", "params")
PARAM_KEYS_V2 = (
    "type", "description", "required", "enum", "default", "list", "item_type", "minimum",
    "maximum", "max_length", "aliases", "format", "access", "content",
)  # fmt: skip
PARAM_KEYS_V1 = ("type", "description", "required", "enum", "default")
TOP_KEYS_V2 = ("schema", "module", "tools", "install", "units", "desktop")
TOP_KEYS_V1 = ("module", "tools")


def load_manifest(
    path: Path,
    *,
    source: Source,
    distribution: str | None = None,
    interpreter: tuple[str, ...] | None = None,
    defaults: ManifestDefaults = DEFAULTS,
) -> ModuleSpec:
    """Parses the manifest at `path`; raises `ManifestError` for any schema violation.

    `interpreter` is the argv from the installed-pack record (`packs.json`), which wins over a
    manifest `interpreter`; that one counts only for path-scanned modules ("Interpreter").
    """
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError(path, None, "file not found") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(path, None, f"cannot read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(path, None, f"malformed TOML: {exc}") from exc

    if "schema" in data:
        schema = data["schema"]
        if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA_V2:
            raise ManifestError(path, "schema", f"must be the integer 2 (or absent for v1), got {schema!r}")
    else:
        schema = 1
    reader = _Reader(path, schema)
    reader.check_keys(data, "", TOP_KEYS_V2 if reader.strict else TOP_KEYS_V1)
    return _Parser(reader, path, source, distribution, interpreter, defaults).module(data)


class _Parser:
    def __init__(
        self,
        r: _Reader,
        path: Path,
        source: Source,
        distribution: str | None,
        record_interpreter: tuple[str, ...] | None,
        defaults: ManifestDefaults,
    ) -> None:
        self.r = r
        self.path = path
        self.root = path.parent
        self.source = source
        self.distribution = distribution
        self.record_interpreter = record_interpreter
        self.defaults = defaults

    # -- placeholders --

    def substitute(self, argv: Sequence[str], where: str, values: Mapping[str, str]) -> tuple[str, ...]:
        out = []
        for index, item in enumerate(argv):
            for name in PLACEHOLDER_RE.findall(item):
                if name not in values:
                    allowed = ", ".join("{" + n + "}" for n in sorted(values))
                    raise self.r.fail(
                        f"{where}[{index}]", f"unknown placeholder {{{name}}}; allowed: {allowed}"
                    )
            out.append(PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], item))
        return tuple(out)

    # -- module --

    def module(self, data: Mapping[str, object]) -> ModuleSpec:
        r = self.r
        m = r.table(data, "module", "", required=True)
        r.check_keys(m, "module", MODULE_KEYS_V2 if r.strict else MODULE_KEYS_V1)
        name = r.get(m, "name", "module", str, required=True)
        if not MODULE_NAME_RE.match(name):
            raise r.fail("module.name", f"{name!r} must match {MODULE_NAME_RE.pattern}")
        if name in RESERVED_MODULE_NAMES:
            raise r.fail("module.name", f"{name!r} is reserved")
        summary = r.get(m, "summary", "module", str, required=True)
        if len(summary) > MAX_SUMMARY:
            raise r.fail("module.summary", f"at most {MAX_SUMMARY} characters, got {len(summary)}")

        declared_pack = None
        if r.strict and "pack" in m:
            declared_pack = r.get(m, "pack", "module", str)
            if not PACK_NAME_RE.match(declared_pack):
                raise r.fail("module.pack", f"{declared_pack!r} must match {PACK_NAME_RE.pattern}")
            if declared_pack.startswith(RESERVED_PACK_PREFIX):
                raise r.fail(
                    "module.pack", f"the prefix {RESERVED_PACK_PREFIX!r} is reserved for host MCP servers"
                )
        pack = declared_pack or name

        version = self.version(m)
        python = r.get(m, "python", "module", str)
        if python is not None and not re.match(
            r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$", python
        ):
            raise r.fail("module.python", f"{python!r} is not a dotted import path")
        exec_raw = r.strings(m, "exec", "module") if "exec" in m else None
        if exec_raw is not None and not exec_raw:
            raise r.fail("module.exec", "must not be empty")

        if r.strict:
            protocol_default = "jsonl"
        else:
            protocol_default = "oneshot"
        if "protocol" in m and exec_raw is None:
            raise r.fail("module.protocol", "only an exec module has a protocol")
        protocol = r.choice(m, "protocol", "module", ("jsonl", "oneshot"), protocol_default)

        interpreter = self.interpreter(m, python)
        state = str(module_state_dir(name))
        data_value = str(module_data_dir(name))
        values = {"root": str(self.root), "state": state, "data": data_value}
        values["python"] = interpreter[0] if interpreter else sys.executable
        exec_argv = self.substitute(exec_raw, "module.exec", values) if exec_raw is not None else None

        inprocess = bool(r.get(m, "inprocess", "module", bool, False))
        requires = r.strings(m, "requires", "module")
        for index, item in enumerate(requires):
            try:
                parse_requirement(item)
            except ValueError as exc:
                raise r.fail(f"module.requires[{index}]", str(exc)) from None
        calls = r.strings(m, "calls", "module")
        for index, item in enumerate(calls):
            if not ANY_TOOL_RE.match(item) or "__" in item:
                raise r.fail(f"module.calls[{index}]", f"{item!r} is not a tool name")
        requires_hal = r.get(m, "requires_hal", "module", str)
        if requires_hal is not None:
            try:
                parse_specifier(requires_hal)
            except ValueError as exc:
                raise r.fail("module.requires_hal", str(exc)) from None
        env = r.strings(m, "env", "module")
        for index, item in enumerate(env):
            if not ENV_NAME_RE.match(item):
                raise r.fail(f"module.env[{index}]", f"{item!r} is not an environment variable name")
        domains = r.strings(m, "domains", "module")
        for index, item in enumerate(domains):
            problem = _domain_problem(item)
            if problem:
                raise r.fail(f"module.domains[{index}]", problem)
        secrets = self.secrets(m)
        if secrets and exec_argv is not None and protocol == "oneshot":
            raise r.fail(
                "module.secrets", 'a one-shot exec module cannot receive secrets; use protocol = "jsonl"'
            )
        startup_timeout = r.get(m, "startup_timeout", "module", float, self.defaults.startup_timeout)
        if not 0 < startup_timeout <= self.defaults.max_timeout:
            raise r.fail(
                "module.startup_timeout", f"must be above 0 and at most {self.defaults.max_timeout:g}"
            )
        max_workers = r.get(m, "max_workers", "module", int, self.defaults.max_workers)
        if not 1 <= max_workers <= MAX_WORKERS_LIMIT:
            raise r.fail("module.max_workers", f"must be 1 to {MAX_WORKERS_LIMIT}, got {max_workers}")
        keywords = r.keywords(m, "keywords", "module")

        tools = tuple(self.tool(t, index, name, pack) for index, t in enumerate(r.tables(data, "tools", "")))
        seen: set[str] = set()
        for index, tool in enumerate(tools):
            if tool.name in seen:
                raise r.fail(f"tools[{index}].name", f"duplicate tool name {tool.name!r}")
            seen.add(tool.name)
        if tools and (python is None) == (exec_argv is None):
            raise r.fail("module", "a module with tools needs exactly one of python and exec")

        install = self.install(data, m, values)
        units, desktop = self.units(data)
        if not r.strict:
            r.warn(f"v1 manifest (no schema = 2): its {len(tools)} tools load as tier act")
        return ModuleSpec(
            name=name,
            pack=pack,
            summary=summary,
            version=version,
            schema=SCHEMA_V2 if r.strict else 1,
            root=self.root,
            manifest=self.path,
            source=self.source,
            distribution=self.distribution,
            python=python,
            exec=exec_argv,
            protocol=protocol,
            interpreter=interpreter,
            inprocess=inprocess,
            requires=requires,
            calls=calls,
            requires_hal=requires_hal,
            env=env,
            domains=domains,
            secrets=secrets,
            startup_timeout=float(startup_timeout),
            max_workers=max_workers,
            keywords=keywords,
            tools=tools,
            install=install,
            units=units,
            desktop=desktop,
            warnings=tuple(r.warnings),
            declared_pack=declared_pack,
        )

    def version(self, m: Mapping[str, object]) -> str:
        r = self.r
        if r.strict and "version" in m:
            version = r.get(m, "version", "module", str)
            if not VERSION_RE.match(version):
                raise r.fail("module.version", f"{version!r} must be dotted numbers, such as 0.1.0")
            return version
        if self.distribution:
            try:
                return importlib.metadata.version(self.distribution)
            except importlib.metadata.PackageNotFoundError:
                # not installed as a distribution; the caller falls back to the manifest version
                pass
        return "0"

    def interpreter(self, m: Mapping[str, object], python: str | None) -> tuple[str, ...] | None:
        r = self.r
        declared = r.strings(m, "interpreter", "module") if r.strict and "interpreter" in m else None
        if declared is not None:
            if python is None:
                raise r.fail("module.interpreter", "only a python module has an interpreter")
            if not declared:
                raise r.fail("module.interpreter", "must not be empty")
        if self.record_interpreter:
            return tuple(self.record_interpreter)
        if self.source == "entry_point" or self.source == "builtin":
            return None  # the hub's environment is where its metadata came from
        if declared is not None:
            if self.source != "path":
                r.warn("module.interpreter is honoured only for path-scanned modules; ignored")
                return None
            values = {"root": str(self.root), "state": str(module_state_dir(m["name"]))}
            values["data"] = str(module_data_dir(m["name"]))
            return self.substitute(declared, "module.interpreter", values)
        return None

    def secrets(self, m: Mapping[str, object]) -> tuple[SecretSpec, ...]:
        r = self.r
        if not r.strict or "secrets" not in m:
            return ()
        found = []
        names: set[str] = set()
        for index, table in enumerate(r.tables(m, "secrets", "module")):
            where = f"module.secrets[{index}]"
            r.check_keys(table, where, ("name", "config_key", "default_path"))
            name = r.get(table, "name", where, str, required=True)
            if not SECRET_NAME_RE.match(name):
                raise r.fail(f"{where}.name", f"{name!r} must match {SECRET_NAME_RE.pattern}")
            if name in names:
                raise r.fail(f"{where}.name", f"duplicate secret name {name!r}")
            names.add(name)
            config_key = r.get(table, "config_key", where, str, required=True)
            if not re.match(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$", config_key):
                raise r.fail(
                    f"{where}.config_key",
                    f"{config_key!r} must be a dotted config key, such as mail.password_file",
                )
            default_path = r.get(table, "default_path", where, str, required=True)
            if not default_path:
                raise r.fail(f"{where}.default_path", "must not be empty")
            found.append(SecretSpec(name, config_key, default_path))
        return tuple(found)

    # -- tools --

    def tool(self, t: Mapping[str, object], index: int, module: str, pack: str) -> ToolSpec:
        r = self.r
        where = f"tools[{index}]"
        r.check_keys(t, where, TOOL_KEYS_V2 if r.strict else TOOL_KEYS_V1)
        name = r.get(t, "name", where, str, required=True)
        prefix = BUILTIN_PREFIX if module == BUILTIN_MODULE else f"{module}_"
        if not re.fullmatch(re.escape(prefix) + TOOL_SUFFIX_RE, name) or len(name) > MAX_TOOL_NAME:
            raise r.fail(
                f"{where}.name",
                f"{name!r} must be {prefix}<words> in lowercase letters, digits and single underscores, "
                f"at most {MAX_TOOL_NAME} characters",
            )
        if module != BUILTIN_MODULE and name.startswith(BUILTIN_PREFIX):
            raise r.fail(f"{where}.name", f"the prefix {BUILTIN_PREFIX!r} is reserved for the hub")
        function = r.get(t, "function", where, str, name)
        if not FUNCTION_RE.match(function):
            raise r.fail(f"{where}.function", f"{function!r} is not a function name")
        description = r.get(t, "description", where, str, required=True).strip()
        if not description:
            raise r.fail(f"{where}.description", "must not be empty")
        if len(description) > MAX_DESCRIPTION:
            raise r.fail(
                f"{where}.description", f"at most {MAX_DESCRIPTION} characters, got {len(description)}"
            )
        params = self.params(t, where)
        keywords = r.keywords(t, "keywords", where)
        timeout = self.timeout(t, where)

        if not r.strict:
            return ToolSpec(
                name=name,
                module=module,
                pack=pack,
                function=function,
                description=description,
                tier="act",
                tier_inferred=True,
                timeout=timeout,
                keywords=keywords,
                max_output=self.defaults.max_result_chars,
                params=params,
            )

        tier = r.get(t, "tier", where, str, required=True)
        if tier not in TIERS:
            raise r.fail(f"{where}.tier", f"{tier!r} is not one of {', '.join(TIERS)}")
        flags = {
            flag: bool(r.get(t, flag, where, bool, False))
            for flag in (
                "network",
                "untrusted_output",
                "reviewable",
                "reversible",
                "runs_code",
                "always_offer",
            )
        }
        routine = self.routine(t, where, tier, flags["reviewable"], params)
        max_output = r.get(t, "max_output", where, int, self.defaults.max_result_chars)
        if max_output < 1:
            raise r.fail(f"{where}.max_output", "must be at least 1")
        title = r.get(t, "title", where, str)
        return ToolSpec(
            name=name,
            module=module,
            pack=pack,
            function=function,
            description=description,
            tier=tier,
            tier_inferred=False,
            title=title,
            network=flags["network"],
            untrusted_output=flags["untrusted_output"],
            reviewable=flags["reviewable"],
            reversible=flags["reversible"],
            runs_code=flags["runs_code"],
            routine=routine,
            timeout=timeout,
            always_offer=flags["always_offer"],
            keywords=keywords,
            max_output=max_output,
            params=params,
        )

    def timeout(self, t: Mapping[str, object], where: str) -> float:
        r = self.r
        value = float(r.get(t, "timeout", where, float, DEFAULT_TOOL_TIMEOUT))
        cap = self.defaults.max_timeout
        if MIN_TOOL_TIMEOUT <= value <= cap:
            return value
        if r.strict:
            raise r.fail(
                f"{where}.timeout", f"must be {MIN_TOOL_TIMEOUT:g} to {cap:g} seconds, got {value:g}"
            )
        clamped = min(max(value, MIN_TOOL_TIMEOUT), cap)
        r.warn(f"{where}.timeout {value:g} clamped to {clamped:g}")
        return clamped

    def routine(
        self, t: Mapping[str, object], where: str, tier: str, reviewable: bool, params: tuple[ParamSpec, ...]
    ) -> RoutineSpec | None:
        r = self.r
        if "routine" not in t:
            return None
        table = r.table(t, "routine", where)
        r.check_keys(table, f"{where}.routine", ("id", "steps"))
        id_param = r.get(table, "id", f"{where}.routine", str, required=True)
        steps_param = r.get(table, "steps", f"{where}.routine", str, required=True)
        if tier != "act":
            raise r.fail(f"{where}.routine", "a routine-defining tool must have tier act (REQ-HUB-64)")
        if reviewable:
            raise r.fail(f"{where}.routine", "a routine-defining tool must not be reviewable (REQ-HUB-64)")
        by_name = {p.name: p for p in params}
        if id_param not in by_name:
            raise r.fail(f"{where}.routine.id", f"{id_param!r} is not a declared parameter")
        steps = by_name.get(steps_param)
        if steps is None or steps.format != "json":
            raise r.fail(
                f"{where}.routine.steps", f'{steps_param!r} must name a declared format = "json" parameter'
            )
        return RoutineSpec(id_param, steps_param)

    def params(self, t: Mapping[str, object], where: str) -> tuple[ParamSpec, ...]:
        r = self.r
        table = r.table(t, "params", where)
        found = [self.param(name, spec, f"{where}.params.{name}") for name, spec in table.items()]
        names = {p.name for p in found}
        claimed: dict[str, str] = {}
        for p in found:
            for alias in p.aliases:
                if alias in names or alias in claimed:
                    raise r.fail(
                        f"{where}.params.{p.name}.aliases", f"alias {alias!r} is already a name or alias"
                    )
                claimed[alias] = p.name
        return tuple(found)

    def param(self, name: str, spec: object, where: str) -> ParamSpec:
        r = self.r
        if not isinstance(spec, dict):
            raise r.fail(where, "must be a table")
        if name.startswith("_") or name in RESERVED_PARAM_NAMES:
            raise r.fail(where, f"parameter name {name!r} is reserved: approvals never come from arguments")
        if not PARAM_NAME_RE.match(name):
            raise r.fail(where, f"parameter name {name!r} must match {PARAM_NAME_RE.pattern}")
        r.check_keys(spec, where, PARAM_KEYS_V2 if r.strict else PARAM_KEYS_V1)
        raw_type = r.get(spec, "type", where, str, "string")
        is_list = bool(r.get(spec, "list", where, bool, False)) if r.strict else False
        fmt = r.choice(spec, "format", where, PARAM_FORMATS, "text") if r.strict else "text"
        v1_object = False
        if not r.strict and raw_type == "array":
            r.warn(f"{where}.type: v1 array loads as a comma-separated string")
            raw_type, is_list = "string", True
        elif not r.strict and raw_type == "object":
            r.warn(f"{where}.type: v1 object loads as a JSON string")
            raw_type, fmt, v1_object = "string", "json", True
        if raw_type not in PARAM_TYPES:
            raise r.fail(f"{where}.type", f"{raw_type!r} is not one of {', '.join(PARAM_TYPES)}")
        ptype: ParamType = raw_type

        if is_list and ptype != "string":
            raise r.fail(f"{where}.list", "only a string parameter can be a list")
        if r.strict and "item_type" in spec and not is_list:
            raise r.fail(f"{where}.item_type", "only a list parameter has an item_type")
        item_type = (
            r.choice(spec, "item_type", where, ("string", "integer"), "string") if r.strict else "string"
        )

        enum = self.enum(spec, where, ptype)
        default = self.default(spec, where, ptype)
        minimum = maximum = None
        if r.strict:
            for bound in ("minimum", "maximum"):
                if bound in spec and ptype not in ("integer", "number"):
                    raise r.fail(f"{where}.{bound}", "only an integer or number parameter has bounds")
            minimum = r.get(spec, "minimum", where, float)
            maximum = r.get(spec, "maximum", where, float)
            if minimum is not None and maximum is not None and minimum > maximum:
                raise r.fail(f"{where}.minimum", "is above maximum")
        max_length = (
            r.get(spec, "max_length", where, int, DEFAULT_MAX_LENGTH) if r.strict else DEFAULT_MAX_LENGTH
        )
        if max_length < 1:
            raise r.fail(f"{where}.max_length", "must be at least 1")
        aliases = r.strings(spec, "aliases", where) if r.strict else ()
        for index, alias in enumerate(aliases):
            if not PARAM_NAME_RE.match(alias) or alias in RESERVED_PARAM_NAMES:
                raise r.fail(f"{where}.aliases[{index}]", f"{alias!r} is not a usable parameter name")
        if r.strict and "access" in spec and fmt != "path":
            raise r.fail(f"{where}.access", 'only a format = "path" parameter has access')
        access = r.choice(spec, "access", where, ("read", "write"), "read") if r.strict else "read"
        if fmt != "text" and ptype != "string":
            raise r.fail(f"{where}.format", "only a string parameter has a format")
        content = bool(r.get(spec, "content", where, bool, False)) if r.strict else False
        return ParamSpec(
            name=name,
            type=ptype,
            description=str(r.get(spec, "description", where, str, "")),
            required=bool(r.get(spec, "required", where, bool, False)),
            enum=enum,
            default=default,
            list=is_list,
            item_type=item_type,
            minimum=minimum,
            maximum=maximum,
            max_length=max_length,
            aliases=aliases,
            format=fmt,
            access=access,
            content=content,
            v1_object=v1_object,
        )

    def enum(
        self, spec: Mapping[str, object], where: str, ptype: str
    ) -> tuple[str | int | float, ...] | None:
        if "enum" not in spec:
            return None
        values = spec["enum"]
        if not isinstance(values, list) or not values:
            raise self.r.fail(f"{where}.enum", "must be a non-empty array")
        for index, value in enumerate(values):
            if not _of_type(value, ptype) or isinstance(value, bool):
                raise self.r.fail(f"{where}.enum[{index}]", f"{value!r} is not a {ptype}")
        return tuple(values)

    def default(self, spec: Mapping[str, object], where: str, ptype: str) -> str | int | float | bool | None:
        if "default" not in spec:
            return None
        value = spec["default"]
        if not _of_type(value, ptype):
            if self.r.strict:
                raise self.r.fail(f"{where}.default", f"{value!r} is not a {ptype}")
            self.r.warn(f"{where}.default {value!r} is not a {ptype}; ignored")
            return None
        return value

    # -- install, units, desktop --

    def install(
        self, data: Mapping[str, object], m: Mapping[str, object], values: Mapping[str, str]
    ) -> tuple[InstallStep, ...]:
        r = self.r
        if not r.strict:
            if "install" not in m:
                return ()
            argv = r.strings(m, "install", "module")
            if not argv:
                raise r.fail("module.install", "must not be empty")
            run = self.substitute(argv, "module.install", values)
            summary = f"run {' '.join(argv)}"
            return (InstallStep("setup", summary, run, timeout=V1_INSTALL_TIMEOUT, network=True),)
        steps = []
        ids: set[str] = set()
        for index, table in enumerate(r.tables(data, "install", "")):
            where = f"install[{index}]"
            r.check_keys(table, where, ("id", "summary", "run", "timeout", "network", "cwd"))
            step_id = r.get(table, "id", where, str, required=True)
            if not INSTALL_ID_RE.match(step_id):
                raise r.fail(f"{where}.id", f"{step_id!r} must match {INSTALL_ID_RE.pattern}")
            if step_id in ids:
                raise r.fail(f"{where}.id", f"duplicate install step id {step_id!r}")
            ids.add(step_id)
            summary = r.get(table, "summary", where, str, required=True)
            if "run" not in table:
                raise r.fail(f"{where}.run", "required key is missing")
            argv = r.strings(table, "run", where)
            if not argv:
                raise r.fail(f"{where}.run", "must not be empty")
            timeout = float(r.get(table, "timeout", where, float, DEFAULT_INSTALL_TIMEOUT))
            if not 0 < timeout <= MAX_INSTALL_TIMEOUT:
                raise r.fail(f"{where}.timeout", f"must be above 0 and at most {MAX_INSTALL_TIMEOUT:g}")
            steps.append(
                InstallStep(
                    id=step_id,
                    summary=summary,
                    run=self.substitute(argv, f"{where}.run", values),
                    timeout=timeout,
                    network=bool(r.get(table, "network", where, bool, False)),
                    cwd=r.choice(table, "cwd", where, ("root", "state"), "root"),
                )
            )
        return tuple(steps)

    def relative_file(self, table: Mapping[str, object], where: str, suffixes: Sequence[str]) -> Path:
        r = self.r
        text = r.get(table, "file", where, str, required=True)
        relative = Path(text)
        if relative.is_absolute() or ".." in relative.parts:
            raise r.fail(f"{where}.file", f"{text!r} must be a path inside the module directory")
        if not text.endswith(tuple(suffixes)):
            raise r.fail(f"{where}.file", f"{text!r} must end in {', '.join(suffixes)}")
        return self.root / relative

    def units(self, data: Mapping[str, object]) -> tuple[tuple[UnitSpec, ...], tuple[Path, ...]]:
        r = self.r
        if not r.strict:
            found = sorted(
                p for p in (self.root / "systemd").glob("*") if p.suffix in UNIT_SUFFIXES and p.is_file()
            )
            launchers = sorted(p for p in (self.root / "desktop").glob("*.desktop") if p.is_file())
            return tuple(UnitSpec(p) for p in found), tuple(launchers)
        units = []
        for index, table in enumerate(r.tables(data, "units", "")):
            where = f"units[{index}]"
            r.check_keys(table, where, ("file", "gpu", "enable"))
            units.append(
                UnitSpec(
                    file=self.relative_file(table, where, UNIT_SUFFIXES),
                    gpu=bool(r.get(table, "gpu", where, bool, False)),
                    enable=bool(r.get(table, "enable", where, bool, True)),
                )
            )
        desktop = []
        for index, table in enumerate(r.tables(data, "desktop", "")):
            where = f"desktop[{index}]"
            r.check_keys(table, where, ("file",))
            desktop.append(self.relative_file(table, where, (".desktop",)))
        return tuple(units), tuple(desktop)


def _of_type(value: object, ptype: str) -> bool:
    if ptype == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if ptype == "integer":
        return isinstance(value, int)
    if ptype == "number":
        return isinstance(value, int | float)
    return isinstance(value, str)


def _domain_problem(host: str) -> str | None:
    """Why `host` is not a lowercase IDNA host name or `*.` wildcard, or None when it is."""
    name = host[2:] if host.startswith("*.") else host
    if host != host.lower():
        return f"{host!r} must be lowercase"
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        return f"{host!r} must be in IDNA (xn--) form"
    labels = name.split(".")
    if len(name) > 253 or len(labels) < 2 or not all(HOST_LABEL_RE.match(label) for label in labels):
        return f"{host!r} is not a host name such as example.org or *.example.org"
    return None
