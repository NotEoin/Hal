"""Import of the legacy `hal.conf` settings that the gateway now owns (design 05, 8.4).

`import_hal_conf` reads `KEY=value` lines and never sources the file, so a shell expansion,
command substitution or any other code in it is never run. Only the five keys the old
`hal-serve` read are used; every other line is left to halcore. The result has the shape of
`config.toml` (`[gateway.backends.local]` and `[hal]`) so `hal configure` can show it and
merge it. Only a leading `~` or `$HOME` is expanded, because those are the only expansions the
old file used for paths.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_LOCAL_KEYS = {
    "HAL_LLAMA_SERVER": "server",
    "HAL_MODELS_DIR": "models_dir",
    "HAL_MODE": "mode",
}


class LegacyImportError(ValueError):
    """A `hal.conf` value cannot be mapped to a valid gateway setting."""


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    # an unquoted value ends at the first ` #`, as in the shell
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _expand_home(value: str) -> str:
    home = str(Path.home())
    if value.startswith("~/") or value == "~":
        return home + value[1:]
    for prefix in ("$HOME", "${HOME}"):
        if value.startswith(prefix):
            return home + value[len(prefix) :]
    return value


def parse_hal_conf(text: str) -> dict[str, str]:
    """The `KEY=value` pairs in `text`; later lines win, comments and other lines are skipped."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match and not line.lstrip().startswith("#"):
            pairs[match.group(1)] = _unquote(match.group(2))
    return pairs


def import_hal_conf(path: Path) -> dict:
    """Map the legacy keys in `path` to a `config.toml`-shaped dict; empty values are skipped.

    Raises `FileNotFoundError` when `path` is missing and `LegacyImportError` for a
    `HAL_LLAMA_URL` that is not a loopback `http` URL with a port.
    """
    pairs = parse_hal_conf(path.read_text(encoding="utf-8", errors="replace"))
    local: dict[str, object] = {
        name: _expand_home(pairs[key]) for key, name in _LOCAL_KEYS.items() if pairs.get(key)
    }
    if pairs.get("HAL_LLAMA_URL"):
        local["port"] = _port_of(pairs["HAL_LLAMA_URL"])
    out: dict = {}
    if local:
        out["gateway"] = {"backends": {"local": local}}
    if pairs.get("HAL_OLLAMA_URL"):
        out["hal"] = {"ollama_url": pairs["HAL_OLLAMA_URL"]}
    log.info("imported %d legacy setting(s) from %s", len(local) + ("hal" in out), path)
    return out


def _port_of(url: str) -> int:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise LegacyImportError(f"HAL_LLAMA_URL has a bad port: {url}") from exc
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS or port is None:
        raise LegacyImportError(f"HAL_LLAMA_URL must be a loopback http URL with a port: {url}")
    return port
