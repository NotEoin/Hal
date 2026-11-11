"""The `[gateway]` configuration: typed tables, strict validation, one `GatewayConfig` value.

The schema is the one in docs/design/05-gateway.md "Configuration". Decisions:

- Sub-tables (`log`, `sessions`, `gpu`) are separate halkit tables, so each gets halkit's
  error messages that name file, table and key. Backends and aliases are user-named entries, so
  each entry is validated on its own against the key set of its `type`.
- Nothing is read at import time; `load_config` reads the file on every call, so a daemon sees edits.
- A bad value stops the gateway (REQ-SYS-06): there is no fallback to defaults.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from halkit import config, modelconf, paths, redact

from .gpu.peers import PeerConfigError, parse_peers

OWNER = "hal-gateway"
LOOPBACK_HOSTS = ("127.0.0.1", "::1")
BACKEND_TYPES = ("llama-router", "managed-openai", "ollama", "openai")
RESERVED_BACKEND_NAMES = ("hal",)  # the slash alias form `hal/main` needs the name free
BACKEND_APIS = ("chat", "responses", "messages", "models", "embeddings", "native")
COORDINATORS = ("auto", "gpu-light-v2", "gpu-light-v1", "none")
ON_BUSY = ("proceed", "fail")
GPU_CLASSES = ("interactive", "service", "batch")
LAUNCHERS = ("process", "docker")
MAX_PORT = 65535
# role alias -> the `[hal]` key that names its model (REQ-GW-26)
ROLE_ALIASES = {
    "hal-chat": "chat_model",
    "hal-classifier": "classifier_model",
    "hal-code": "code_model",
    "hal-writer": "writer_model",
    "hal-embed": "embed_model",
    "hal-vision": "vision_model",
}
EMPTY_MEANS_CHAT = ("hal-code", "hal-writer")
# mode alias -> the `[gateway]` key that may point it at another alias (`hal/main = hal-code`)
MODE_KEYS = {"hal/main": "main", "hal/agent": "agent"}
MODE_ALIASES = tuple(MODE_KEYS)
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# set by the gateway itself for a managed server (docs/design/05-gateway.md "Managed OpenAI server")
MANAGED_ENV = ("PORT", "HOST")


def _positive(value: object) -> str | None:
    return None if isinstance(value, (int, float)) and value > 0 else "must be greater than 0"


def _non_negative(value: object) -> str | None:
    return None if isinstance(value, (int, float)) and value >= 0 else "must be 0 or more"


def _port(value: object) -> str | None:
    return None if isinstance(value, int) and 1 <= value <= MAX_PORT else f"must be 1 to {MAX_PORT}"


def _loopback_host(value: object) -> str | None:
    if value in LOOPBACK_HOSTS:
        return None
    return f"{value!r} is not 127.0.0.1 or ::1; the gateway never binds beyond loopback (REQ-SYS-09)"


def _strings(value: object) -> str | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return None
    return "must be a list of strings"


def _peers(value: object) -> str | None:
    try:
        parse_peers(value if isinstance(value, list) else [None])
    except PeerConfigError as exc:
        return str(exc)
    return None


def _url_or_empty(value: object) -> str | None:
    if value == "":
        return None
    parts = urlsplit(str(value))
    if parts.scheme in ("http", "https") and parts.hostname:
        return None
    if parts.scheme == "unix" and parts.path.startswith("/"):
        return None
    return "must be empty, http://..., https://... or unix:/absolute/path.sock"


def _loopback_http_url(value: object) -> str | None:
    """REQ-GW-68: client mode reaches the gateway through a tunnel end on loopback."""
    if value == "":
        return None
    parts = urlsplit(str(value))
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "::1") and parts.port:
        return None
    return "must be empty or http://127.0.0.1:PORT or http://[::1]:PORT (the tunnel end)"


def _command(value: object) -> str | None:
    if not (isinstance(value, list) and value and all(isinstance(item, str) and item for item in value)):
        return "must be a non-empty list of non-empty strings (the server's argv)"
    if not Path(value[0]).expanduser().is_absolute():
        return f"{value[0]!r} must be an absolute path; the server gets no shell and no PATH search"
    return None


def _absolute_or_empty(value: object) -> str | None:
    return None if value == "" or Path(str(value)).expanduser().is_absolute() else "must be an absolute path"


def _env_name_or_empty(value: object) -> str | None:
    return None if value == "" or ENV_NAME.match(str(value)) else "must be an environment variable name"


def _child_env(value: object) -> str | None:
    """REQ-GW-90: a managed server's extra environment holds settings, never secrets."""
    if not isinstance(value, dict):
        return 'must be a table of NAME = "value"'
    for name, item in value.items():
        if not ENV_NAME.match(name) or not isinstance(item, str):
            return f"{name!r} must be an environment variable name with a string value"
        if name in MANAGED_ENV:
            return f"{name} is set by the gateway (from port); remove it"
        if redact.is_secret_key(name):
            return f"{name} looks like a secret; the config holds no secrets, use key_env for the API key"
    return None


def _http_path(value: object) -> str | None:
    return None if str(value).startswith("/") else "must start with /"


def _vram_map(value: object) -> str | None:
    ok = isinstance(value, dict) and all(isinstance(v, int) and v > 0 for v in value.values())
    return None if ok else "must map names to MiB integers above 0"


K = config.Key
GATEWAY_TABLE = config.register(
    config.Table(
        name="gateway",
        owner=OWNER,
        keys=(
            K(
                "url",
                str,
                "",
                check=_loopback_http_url,
                doc="Client mode: URL of a remote gateway; empty = local",
            ),
            K("unix_path", str, "", doc="Unix socket path; empty = runtime_dir()/gateway.sock"),
            K("tcp", bool, False, doc="Listen on loopback TCP (off until `hal connect`)"),
            K("tcp_host", str, "127.0.0.1", check=_loopback_host, doc="TCP bind address, loopback only"),
            K("tcp_port", int, 8741, check=_port, doc="TCP port"),
            K(
                "token_file",
                str,
                "~/.config/hal/gateway-token",
                doc="Owner token (client token in client mode)",
            ),
            K("allow_origins", list, [], check=_strings, doc="Browser origins let in; empty = none"),
            K("allow_hosts", list, [], check=_strings, doc="Extra Host header values"),
            K("default_model", str, "", doc="Model for unknown names; empty = 404"),
            K("main", str, "hal-code", doc="The alias hal/main serves; empty = the router mode's"),
            K("agent", str, "hal-code", doc="The alias hal/agent serves (subagents); empty = the mode's"),
            K("models_file", str, "", doc="Model manifest; empty = config_dir()/models.toml"),
            K("max_request_bytes", int, 32 * 1024 * 1024, check=_positive, doc="Body cap for the owner"),
            K("max_header_bytes", int, 65536, check=_positive, doc="Header size cap"),
            K("max_connections", int, 64, check=_positive, doc="Concurrent connections"),
            K("client_timeout_s", float, 300.0, check=_positive, doc="Idle client socket timeout"),
            K("upstream_connect_timeout_s", float, 10.0, check=_positive, doc="Backend connect timeout"),
            K("upstream_read_timeout_s", float, 3600.0, check=_positive, doc="Backend read timeout"),
            K(
                "drain_s",
                float,
                30.0,
                check=_non_negative,
                doc="Wait for in-flight requests on a mode switch",
            ),
            K("log", dict, {}, doc="The [gateway.log] table"),
            K("sessions", dict, {}, doc="The [gateway.sessions] table"),
            K("gpu", dict, {}, doc="The [gateway.gpu] table"),
            K("backends", dict, {}, doc="User-named backends, see [gateway.backends.NAME]"),
            K("aliases", dict, {}, doc="User-named model aliases, see [gateway.aliases.NAME]"),
        ),
    )
)

LOG_TABLE = config.register(
    config.Table(
        name="gateway.log",
        owner=OWNER,
        keys=(
            K("requests", bool, True, doc="Write the request metadata log"),
            K("content", bool, False, doc="Also store request and reply bodies (opt-in)"),
            K("max_bytes", int, 10 * 1024 * 1024, check=_positive, doc="Rotate the log at this size"),
            K("keep", int, 3, check=_non_negative, doc="Rotated logs kept"),
        ),
    )
)

SESSIONS_TABLE = config.register(
    config.Table(
        name="gateway.sessions",
        owner=OWNER,
        keys=(
            K("max_request_bytes", int, 8 * 1024 * 1024, check=_positive, doc="Body cap for a boxed session"),
            K("max_concurrent", int, 4, check=_positive, doc="Sessions at once"),
            K("ttl_s", int, 86400, check=_positive, doc="Session token lifetime"),
            K("max_requests_per_min", int, 120, check=_positive, doc="Rate cap per session"),
            K("max_output_tokens", int, 16384, check=_positive, doc="Clamp on max_tokens"),
            K("max_tokens_per_hour", int, 100_000_000, check=_positive, doc="Token budget per session"),
        ),
    )
)

GPU_TABLE = config.register(
    config.Table(
        name="gateway.gpu",
        owner=OWNER,
        keys=(
            K("coordinator", str, "auto", choices=COORDINATORS, doc="GPU coordination mode"),
            K("claim_wait_s", float, 10.0, check=_non_negative, doc="Wait for a GPU claim"),
            K("on_busy", str, "proceed", choices=ON_BUSY, doc="What to do when the claim times out"),
            K("idle_release_s", float, 120.0, check=_non_negative, doc="v2: release the claim when idle"),
            K(
                "idle_unload_s",
                float,
                900.0,
                check=_non_negative,
                doc="v1 and none: unload when idle; 0 = hold",
            ),
            K("owner_unix_class", str, "service", choices=GPU_CLASSES, doc="Class for host packs"),
            K("owner_tcp_class", str, "interactive", choices=GPU_CLASSES, doc="Class for host harnesses"),
            K("respect_off_flag", bool, True, doc="Refuse owner requests while `hal off` is set"),
            K("peer_timeout_s", float, 3.0, check=_positive, doc="Peer GPU guard timeout"),
            K("v2_socket", str, "", doc="gpu-light v2 socket; empty = its own resolution order"),
            K("reclaimer_name", str, "hal-gateway", doc="Name registered with gpu-light"),
            K("reclaimer_timeout_s", float, 60.0, check=_positive, doc="Time allowed to free VRAM"),
            K("v1_claim_name", str, "hal-gateway", doc="Claim name for gpu-light protocol 1"),
            K("v1_claim_wait_s", float, 120.0, check=_non_negative, doc="Wait for a v1 claim"),
            K("v1_dir", str, "", doc="gpu-light v1 directory; empty = its own resolution order"),
            K("vram_mib", dict, {}, check=_vram_map, doc="Model or preset -> MiB for admission"),
            K("peers", list, [], check=_peers, doc="Peer GPU servers to check before loading"),
            K(
                "make_room_for",
                list,
                ["hal-chat"],
                check=_strings,
                doc="Aliases whose Ollama requests park a server with `park` when the model is not loaded",
            ),
        ),
    )
)

_COMMON_BACKEND = (
    K("type", str, "", choices=BACKEND_TYPES, doc="Backend kind"),
    K("url", str, "", check=_url_or_empty, doc="Backend URL; empty = derived or default"),
    K("allow_remote", bool, False, doc="Allow a non-loopback https URL"),
    K("api_key_file", str, "", doc="File holding the backend API key"),
    K("api_key_env", str, "", doc="Environment variable holding the backend API key"),
    K("apis", list, [], check=_strings, doc="APIs served; empty = all the type supports"),
    K("gpu", bool, True, doc="The backend uses the GPU"),
    K("expose_models", bool, True, doc="List its models in /v1/models"),
    K("gpu_light_uses", list, [], check=_strings, doc="gpu-light reclaimers that free this backend"),
    K("connect_timeout_s", float, 0.0, check=_non_negative, doc="Override of the gateway value; 0 = inherit"),
    K("read_timeout_s", float, 0.0, check=_non_negative, doc="Override of the gateway value; 0 = inherit"),
)
LLAMA_SERVER = "~/.local/share/hal/llama.cpp/llama-b12121/llama-server"  # where `hal setup` installs the pin
_ROUTER_KEYS = (
    K("managed", bool, True, doc="The gateway starts and stops llama-server"),
    K("server", str, LLAMA_SERVER, doc="llama-server binary"),
    K("models_dir", str, "", doc="Directory of model files; required when managed"),
    K("mode", str, "gpu", doc="Mode preset loaded first"),
    K("modes_dirs", list, ["~/.config/hal/modes"], check=_strings, doc="User mode preset directories"),
    K("listen", str, "tcp", choices=("tcp", "unix"), doc="How the router listens"),
    K("port", int, 8080, check=_port, doc="Router TCP port"),
    K("autostart", str, "on-demand", choices=("on-demand", "with-gateway"), doc="When the router starts"),
    K("load_timeout_s", float, 900.0, check=_positive, doc="Wait for a model load"),
    K("stop_timeout_s", float, 60.0, check=_positive, doc="Wait for the router to stop"),
    K("swap_wait_s", float, 7200.0, check=_positive, doc="Longest wait of a request for a model swap"),
    K("max_restarts", int, 3, check=_non_negative, doc="Crash restarts before giving up"),
    K("unload_ollama", list, ["ollama"], check=_strings, doc="Ollama backends emptied before a load"),
    K("launcher", str, "process", choices=LAUNCHERS, doc="Run llama-server as a process or in docker"),
    K("container_image", str, "", doc="Docker image for the docker launcher"),
    K("container_runtime", str, "nvidia", choices=("nvidia", "runc", "runsc-gpu"), doc="Docker runtime"),
    K("park", bool, False, doc="Free the GPU by parking llama-server in host RAM instead of stopping it"),
)
_MANAGED_KEYS = (
    K("command", list, [], check=_command, doc="argv of the server; the first item is an absolute path"),
    K("cwd", str, "", check=_absolute_or_empty, doc="Working directory; empty = the command's directory"),
    K("env", dict, {}, check=_child_env, doc="Extra environment for the server; no secrets"),
    K("port", int, 8000, check=_port, doc="Loopback TCP port, passed to the server as PORT"),
    K(
        "key_env",
        str,
        "",
        check=_env_name_or_empty,
        doc="Variable for the generated API key; empty = none",
    ),
    K("served_models", list, [], check=_strings, doc="Model ids the server serves; required"),
    K("health_path", str, "/health", check=_http_path, doc="GET path that answers 200 when ready"),
    K("startup_timeout_s", float, 300.0, check=_positive, doc="Wait for the server to become ready"),
    K("stop_timeout_s", float, 60.0, check=_positive, doc="Wait after SIGTERM before SIGKILL"),
    K("autostart", str, "on-demand", choices=("on-demand", "with-gateway"), doc="When the server starts"),
    K("max_restarts", int, 3, check=_non_negative, doc="Crash restarts before giving up"),
    K("exclusive_gpu", bool, True, doc="Stop other managed GPU servers and empty Ollama before a start"),
    K("unload_ollama", list, ["ollama"], check=_strings, doc="Ollama backends emptied before a start"),
    K("idle_stop", bool, True, doc="The idle unload ([gateway.gpu].idle_unload_s) stops the server"),
    K("park", bool, False, doc="Free the GPU by parking the server in host RAM instead of stopping it"),
)
_OLLAMA_KEYS = (K("unload_offloaded_on_claim", bool, True, doc="Unload partly offloaded models on a claim"),)
_ALIAS_KEYS = (
    K("backend", str, "", doc="Backend that serves the alias"),
    K("model", str, "", doc="Upstream model name"),
    K("patterns", list, [], check=_strings, doc="Client model names mapped to this alias"),
    K("context_window", int, 0, check=_non_negative, doc="Context length reported; 0 = unknown"),
    K(
        "slots",
        int,
        0,
        check=_non_negative,
        doc="Parallel decoding slots reported in meta.slots; 0 = unknown",
    ),
    K("effort", bool, False, doc="Apply the effort policy"),
    K("route", dict, {}, doc="Route policy settings"),
    K("delegate", dict, {}, doc="Delegate policy settings"),
    K("shell_tools", list, ["Bash", "run"], check=_strings, doc="Tool names the policies treat as shell"),
    K("agent_tools", list, ["Agent", "Task"], check=_strings, doc="Tool names the policies treat as agents"),
)


@dataclass(frozen=True)
class GatewayConfig:
    """Every `[gateway]` setting with defaults applied; `backends` and `aliases` are by name."""

    gateway: Mapping[str, object]
    log: Mapping[str, object]
    sessions: Mapping[str, object]
    gpu: Mapping[str, object]
    backends: Mapping[str, Mapping[str, object]]
    aliases: Mapping[str, Mapping[str, object]]
    ollama_url: str = ""  # `[hal].ollama_url`, the default of an Ollama backend
    hal: Mapping[str, object] = field(default_factory=dict)  # the `[hal]` table: role alias sources
    roles: Mapping[str, modelconf.Role] = field(default_factory=dict)  # `[models.roles]`: per-role settings


def _entry(
    kind: str,
    name: str,
    raw: object,
    keys: tuple[config.Key, ...],
    path: Path,
    cli: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise config.ConfigError(
            f"[gateway.{kind}.{name}] must be a table", path=path, table=f"gateway.{kind}"
        )
    table = config.Table(name=f"gateway.{kind}.{name}", owner=OWNER, keys=keys)
    return config.load(table, path=path, environ={}, cli=cli)


def _backend(name: str, raw: object, path: Path, *, default_type: str = "") -> Mapping[str, object]:
    table = f"gateway.backends.{name}"
    if name in RESERVED_BACKEND_NAMES:
        raise config.ConfigError(f"backend name {name!r} is reserved", path=path, table=table)
    kind = raw.get("type", default_type) if isinstance(raw, dict) else None
    extra = {"llama-router": _ROUTER_KEYS, "managed-openai": _MANAGED_KEYS, "ollama": _OLLAMA_KEYS}.get(
        str(kind), ()
    )
    cli = {"type": default_type} if default_type else None
    entry = dict(_entry("backends", name, raw, _COMMON_BACKEND + extra, path, cli))
    if not entry["type"]:
        raise config.ConfigError(f"[{table}] needs a type", path=path, table=table, key="type")
    url = str(entry["url"])
    host = urlsplit(url).hostname if url else None
    if host and urlsplit(url).scheme != "unix" and not _is_loopback(host) and not entry["allow_remote"]:
        raise config.ConfigError(
            f"[{table}] url host {host!r} is not loopback; set allow_remote = true to use it",
            path=path,
            table=table,
            key="url",
        )
    if host and not _is_loopback(host) and urlsplit(url).scheme != "https":
        raise config.ConfigError(
            f"[{table}] a remote backend must use https", path=path, table=table, key="url"
        )
    if kind == "llama-router" and entry["managed"] and not entry["models_dir"]:
        raise config.ConfigError(
            f"[{table}] models_dir is required when managed = true", path=path, table=table, key="models_dir"
        )
    if kind == "managed-openai":
        _check_managed(table, entry, path)
    return entry


def _check_managed(table: str, entry: Mapping[str, object], path: Path) -> None:
    """A managed server needs a command and its model list, and listens where the gateway says."""
    if not entry["command"]:
        raise config.ConfigError(f"[{table}] command is required", path=path, table=table, key="command")
    if not entry["served_models"]:
        raise config.ConfigError(
            f"[{table}] served_models must name at least one model",
            path=path,
            table=table,
            key="served_models",
        )
    if entry["url"]:
        raise config.ConfigError(
            f"[{table}] url is derived from port for a managed server; remove it",
            path=path,
            table=table,
            key="url",
        )
    if entry["key_env"] and entry["key_env"] in dict(entry["env"]):  # type: ignore[call-overload]
        raise config.ConfigError(
            f"[{table}] {entry['key_env']} is key_env, set by the gateway", path=path, table=table, key="env"
        )


def _is_loopback(host: str) -> bool:
    if host in config.LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_config(path: Path | None = None) -> GatewayConfig:
    """Reads and validates `[gateway]` from `path` (default: the config file); raises `ConfigError`."""
    path = Path(path) if path is not None else paths.config_file()
    gateway = config.load(GATEWAY_TABLE, path=path)
    backends_raw = gateway["backends"]
    aliases_raw = gateway["aliases"]
    backends = {name: _backend(name, raw, path) for name, raw in dict(backends_raw).items()}
    if not backends:
        backends = {"ollama": _backend("ollama", {}, path, default_type="ollama")}
    aliases = {
        name: _entry("aliases", name, raw, _ALIAS_KEYS, path) for name, raw in dict(aliases_raw).items()
    }
    hal = config.load(config.HAL_TABLE, path=path)
    for hal_key in ROLE_ALIASES.values():
        if hal[hal_key] in ROLE_ALIASES:
            raise config.ConfigError(
                f"[hal] {hal_key} names the role alias {hal[hal_key]!r}; use a model name",
                path=path,
                table="hal",
                key=hal_key,
            )
    for key in MODE_KEYS.values():
        if gateway[key] in MODE_ALIASES:
            raise config.ConfigError(
                f"[gateway] {key} = {gateway[key]!r} points a mode alias at itself; name one like hal-code",
                path=path,
                table="gateway",
                key=key,
            )
    for name, alias in aliases.items():
        if name in ROLE_ALIASES:
            raise config.ConfigError(
                f"[gateway.aliases.{name}] is a role alias; change [hal].{ROLE_ALIASES[name]} instead",
                path=path,
                table="gateway.aliases",
                key=name,
            )
        if alias["backend"] not in backends:
            raise config.ConfigError(
                f"[gateway.aliases.{name}] backend {alias['backend']!r} is not a configured backend",
                path=path,
                table=f"gateway.aliases.{name}",
                key="backend",
            )
    return GatewayConfig(
        gateway=gateway,
        log=config.load(LOG_TABLE, path=path),
        sessions=config.load(SESSIONS_TABLE, path=path),
        gpu=config.load(GPU_TABLE, path=path),
        backends=backends,
        aliases=aliases,
        ollama_url=str(hal["ollama_url"]),
        hal=hal,
        roles=modelconf.load_roles(path),
    )
