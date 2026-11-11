"""The `[models]` configuration: model sources, roles and tuning limits.

    [models]                       # tuning limits
    [models.sources.NAME]          # where models come from (kind: ollama, openai, gguf, ...)
    [models.roles.ROLE]            # which model plays chat, fast, code, writer, vision or embed,
                                   # or a role the user named (a setup such as `code-128k`)

`MODELS_TABLE` is a registered halkit table. Sources and roles are user-named entries, so each
is validated on its own, as `halgateway.config` does for backends: errors name file, table and
key (REQ-SYS-06). Design decisions:

- An API key is never stored inline. A source names a key file (`api_key_file`); a key placed in
  the config under another name (`api_key`, `key`, `token`) is refused with a message that says so.
- A non-loopback `ollama` or `openai` URL needs `allow_remote = true` and https, the same rule as
  a remote gateway backend.
- `anthropic` and `openai-cloud` sources talk to fixed hosts and need only a key file.
- A role's `context`, `kv_type`, `gpu_layers` and `cpu_moe` are filled in by `hal tune --write`
  while `auto = true`; `auto = false` keeps the user's values. `context_cap` limits only what
  auto-tune picks: a context set by hand may go up to the model's own maximum.
- Besides the built-in `ROLES`, a role may have any name `CUSTOM_ROLE` matches: a named setup
  (one model with its own context and settings) that the gateway serves as `hal-NAME`. The name
  is lowercase and short because it becomes part of that alias and of a llama.cpp preset name.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import config, paths

OWNER = "hal-kit"
SOURCE_KINDS = ("ollama", "openai", "gguf", "anthropic", "openai-cloud")
URL_KINDS = ("ollama", "openai")
KEY_FILE_KINDS = ("anthropic", "openai-cloud")
ROLES = ("chat", "code", "writer", "vision", "classifier", "embed")
RENAMED_ROLES = {"fast": "classifier"}  # 0.2.0: an old table under the old name still loads
CUSTOM_ROLE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")  # becomes the alias `hal-NAME`
RESERVED_ROLE_NAMES = ("guard",)  # the tune screens list the guardian under this name
KV_CHOICES = ("auto", "f16", "q8_0", "q4_0")
EFFORT_CHOICES = ("off", "low", "medium", "high", "max")
INLINE_KEY_NAMES = ("api_key", "key", "token", "secret", "password")
UNSET_TEMPERATURE = -1.0
MAX_TEMPERATURE = 2.0  # the OpenAI-compatible APIs reject more
AUTO_LAYERS = -1  # `gpu_layers` value that means "decided by tuning"
DEFAULT_CONTEXT_CAP = 131072  # the common native maximum of current models; see docs/dev/tuning.md
DEFAULT_URLS = {"ollama": "http://127.0.0.1:11434", "openai": "http://127.0.0.1:1234/v1"}


def _positive(value: object) -> str | None:
    return None if isinstance(value, int) and value > 0 else "must be greater than 0"


def _non_negative(value: object) -> str | None:
    return None if isinstance(value, int) and value >= 0 else "must be 0 or more"


def _gpu_layers(value: object) -> str | None:
    return None if isinstance(value, int) and value >= AUTO_LAYERS else "must be -1 (auto) or more"


def _temperature(value: object) -> str | None:
    ok = isinstance(value, (int, float)) and (value == UNSET_TEMPERATURE or 0 <= value <= MAX_TEMPERATURE)
    return None if ok else "must be between 0 and 2, or -1 for the model's default"


K = config.Key
MODELS_TABLE = config.register(
    config.Table(
        name="models",
        owner=OWNER,
        keys=(
            K(
                "context_cap",
                int,
                DEFAULT_CONTEXT_CAP,
                check=_positive,
                doc="Largest context auto-tune picks; a context set by hand may be larger",
            ),
            K(
                "headroom_mib",
                int,
                0,
                check=_non_negative,
                doc="VRAM kept free per GPU; 0 = max(1 GiB, 8 percent)",
            ),
            K("kv_type", str, "auto", choices=KV_CHOICES, doc="Default KV cache type of a role"),
            K("sources", dict, {}, doc="User-named model sources, see [models.sources.NAME]"),
            K("roles", dict, {}, doc="Model per role, see [models.roles.ROLE]"),
        ),
    )
)

_SOURCE_KEYS = (
    K("kind", str, "", choices=("", *SOURCE_KINDS), doc="ollama, openai, gguf, anthropic or openai-cloud"),
    K("url", str, "", doc="Server URL (ollama, openai)"),
    K("path", str, "", doc="Folder of GGUF files (gguf)"),
    K("api_key_file", str, "", doc="File that holds the API key; the key is never put in the config"),
    K("allow_remote", bool, False, doc="Allow a non-loopback https URL"),
)
_ROLE_KEYS = (
    K("source", str, "", doc="Name of the source that holds the model"),
    K("model", str, "", doc="Model name in that source"),
    K("context", int, 0, check=_non_negative, doc="Context tokens; 0 = decided by tuning"),
    K("kv_type", str, "", choices=("", *KV_CHOICES), doc="KV cache type; empty = [models] kv_type"),
    K("gpu_layers", int, AUTO_LAYERS, check=_gpu_layers, doc="Layers on the GPU; -1 = decided by tuning"),
    K(
        "cpu_moe",
        int,
        0,
        check=_non_negative,
        doc="Layers whose routed experts stay in RAM (mixture-of-experts GGUF models)",
    ),
    K("effort", str, "", choices=("", *EFFORT_CHOICES), doc="Default effort level; empty = none"),
    K(
        "temperature",
        float,
        UNSET_TEMPERATURE,
        check=_temperature,
        doc="Sampling temperature; -1 = the model's default",
    ),
    K("family", str, "", doc="Model family for the effort table; empty = detected"),
    K("auto", bool, True, doc="Let `hal tune --write` replace context, kv_type and gpu_layers"),
)


@dataclass(frozen=True)
class Source:
    """A validated `[models.sources.NAME]` entry."""

    name: str
    kind: str
    url: str = ""
    path: str = ""
    api_key_file: str = ""
    allow_remote: bool = False


@dataclass(frozen=True)
class Role:
    """A validated `[models.roles.ROLE]` entry."""

    role: str
    source: str
    model: str
    context: int = 0
    kv_type: str = ""
    gpu_layers: int = AUTO_LAYERS
    effort: str = ""
    temperature: float = UNSET_TEMPERATURE
    family: str = ""
    auto: bool = True
    cpu_moe: int = 0


def is_custom(name: str) -> bool:
    """True when `name` is a role the user named, not one of the built-in `ROLES`."""
    return name not in ROLES


def check_role_name(name: str) -> str | None:
    """Why `name` cannot be a new role, or None when it can."""
    if name in ROLES or name in RESERVED_ROLE_NAMES:
        return f"{name!r} is already a role name"
    if not CUSTOM_ROLE.match(name):
        return (
            f"role name {name!r} must be lowercase letters, digits and '-', "
            "start with a letter, at most 32 characters"
        )
    return None


def role_names(roles: Mapping[str, object]) -> list[str]:
    """The built-in roles in their order, then the named ones in `roles`, sorted."""
    return [*ROLES, *sorted(n for n in roles if is_custom(n))]


def _is_loopback(host: str) -> bool:
    if host in config.LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _fail(msg: str, path: Path, table: str, key: str | None = None) -> config.ConfigError:
    return config.ConfigError(msg, path=path, table=table, key=key)


def _entry(
    kind: str, name: str, raw: object, keys: tuple[config.Key, ...], path: Path
) -> Mapping[str, object]:
    table = f"models.{kind}.{name}"
    if not isinstance(raw, dict):
        raise _fail(f"[{table}] must be a table", path, f"models.{kind}")
    for bad in INLINE_KEY_NAMES:
        if bad in raw:
            raise _fail(
                f"[{table}] {bad}: keys are never stored in the config; put the key in a file "
                "(mode 0600) and set api_key_file",
                path,
                table,
                bad,
            )
    return config.load(config.Table(name=table, owner=OWNER, keys=keys), path=path, environ={})


def _check_source(name: str, e: Mapping[str, object], path: Path) -> Source:
    table = f"models.sources.{name}"
    kind = str(e["kind"])
    if not kind:
        raise _fail(f"[{table}] needs a kind: {', '.join(SOURCE_KINDS)}", path, table, "kind")
    unused = {
        "ollama": ("path", "api_key_file"),
        "openai": ("path",),
        "gguf": ("url", "api_key_file", "allow_remote"),
        "anthropic": ("url", "path", "allow_remote"),
        "openai-cloud": ("url", "path", "allow_remote"),
    }[kind]
    for key in unused:
        if e[key] not in ("", False):
            raise _fail(f"[{table}] {key} does not apply to kind {kind!r}", path, table, key)
    if kind in URL_KINDS:
        url = str(e["url"]) or DEFAULT_URLS[kind]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise _fail(f"[{table}] url {url!r} must be http://... or https://...", path, table, "url")
        if not _is_loopback(parts.hostname) and not (e["allow_remote"] and parts.scheme == "https"):
            raise _fail(
                f"[{table}] url host {parts.hostname!r} is not loopback; set allow_remote = true "
                "and use https",
                path,
                table,
                "url",
            )
    if kind == "gguf" and not e["path"]:
        raise _fail(f"[{table}] a gguf source needs path (a folder of .gguf files)", path, table, "path")
    if kind in KEY_FILE_KINDS and not e["api_key_file"]:
        raise _fail(f"[{table}] a {kind} source needs api_key_file", path, table, "api_key_file")
    url = str(e["url"]) or DEFAULT_URLS.get(kind, "")
    return Source(name, kind, url, str(e["path"]), str(e["api_key_file"]), bool(e["allow_remote"]))


def load_sources(path: Path | None = None) -> dict[str, Source]:
    """The validated sources by name; raises `ConfigError`."""
    path = Path(path) if path is not None else paths.config_file()
    raw = dict(config.load(MODELS_TABLE, path=path)["sources"])  # type: ignore[call-overload]
    return {n: _check_source(n, _entry("sources", n, r, _SOURCE_KEYS, path), path) for n, r in raw.items()}


def load_roles(path: Path | None = None) -> dict[str, Role]:
    """The validated roles by name; raises `ConfigError` for an unknown role or source."""
    path = Path(path) if path is not None else paths.config_file()
    sources = load_sources(path)
    raw = dict(config.load(MODELS_TABLE, path=path)["roles"])  # type: ignore[call-overload]
    roles = {}
    stored = {name: name for name in raw}  # role -> its table name in the file
    for old, new in RENAMED_ROLES.items():
        if old in raw and new not in raw:
            raw[new] = raw.pop(old)
            stored[new] = stored.pop(old)
    for name, body in raw.items():
        table = f"models.roles.{stored[name]}"
        if name not in ROLES and check_role_name(name):
            raise _fail(f"[{table}] {check_role_name(name)}", path, "models.roles", name)
        e = _entry("roles", stored[name], body, _ROLE_KEYS, path)
        if e["source"] not in sources:
            raise _fail(f"[{table}] source {e['source']!r} is not in [models.sources]", path, table, "source")
        if not e["model"]:
            raise _fail(f"[{table}] needs a model name", path, table, "model")
        roles[name] = Role(
            name,
            str(e["source"]),
            str(e["model"]),
            int(e["context"]),
            str(e["kv_type"]),
            int(e["gpu_layers"]),
            str(e["effort"]),
            float(e["temperature"]),
            str(e["family"]),
            bool(e["auto"]),
            int(e["cpu_moe"]),
        )  # type: ignore[call-overload]  # validated above
    return roles


def limits(path: Path | None = None) -> Mapping[str, object]:
    """The `[models]` limits (`context_cap`, `headroom_mib`, `kv_type`) with defaults applied."""
    return config.load(MODELS_TABLE, path=path)
