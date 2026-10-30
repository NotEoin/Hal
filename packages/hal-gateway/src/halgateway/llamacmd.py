"""The managed llama-server command line and environment (REQ-SEC-76, REQ-GW-32).

One function builds both, so the rules are testable in one place. Design decisions:

- The environment is built from an allowlist, never copied from the caller's: a `LLAMA_ARG_*`,
  `LLAMA_API_KEY` or `HF_TOKEN` in the user's shell must not change how the server runs or give
  it a credential.
- `--host` is a unix socket or `127.0.0.1`. On TCP the key comes from `--api-key-file` (a file,
  never argv, so `ps` does not show it) and an explicit `--port` is required.
- `DENIED_FLAGS` is shared with the mode-file renderer, which must refuse the same keys in a
  user preset: they switch on a built-in agent, tools, MCP, remote offload or downloads.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LOOPBACK = "127.0.0.1"
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "CUDA_VISIBLE_DEVICES")
RECLAIMER_NAME = "hal-gateway"  # the gpu-light reclaimer name the gateway registers under
# REQ-SEC-76; `slot-save-path` is allowed only below the state directory (see `check_slot_path`)
DENIED_FLAGS = frozenset(
    {
        "tools", "agent", "mcp-servers-config", "mcp-servers-json", "webui-mcp-proxy",
        "props", "rpc", "media-path", "hf-repo",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Preset:
    """A rendered mode file: the `--models-preset` argument."""

    path: Path
    sha256: str


class CommandRefused(ValueError):
    """The command would break REQ-SEC-76; the message names the setting."""


def check_slot_path(path: Path, state: Path) -> None:
    """Raises `CommandRefused` unless `path` resolves below `state/gateway/slots`."""
    allowed = (state / "gateway" / "slots").resolve()
    if not path.resolve().is_relative_to(allowed):
        raise CommandRefused(f"slot-save-path {path} is outside {allowed}")


def llama_server_command(
    preset: Preset,
    *,
    listen: str,
    api_key_file: Path | None,
    state: Path,
    llama_dir: Path | None = None,
    models_max: int = 1,
    autoload: bool = True,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The argv and environment for the managed router.

    `listen` is `127.0.0.1:PORT` or an absolute unix socket path below `state/gateway/run/`.
    TCP needs `api_key_file`. `autoload=False` adds `--no-models-autoload`. `llama_dir` is the
    only directory added to `LD_LIBRARY_PATH`.
    Raises `CommandRefused` for any other listen address or a missing key.
    """
    argv = [str(llama_dir / "llama-server") if llama_dir else "llama-server"]
    if listen.startswith("/"):
        run_dir = (state / "gateway" / "run").resolve()
        if not Path(listen).resolve().is_relative_to(run_dir):
            raise CommandRefused(f"socket {listen} is outside {run_dir}")
        argv += ["--host", listen]
    else:
        host, _, port = listen.rpartition(":")
        if host != LOOPBACK or not port.isdigit():
            raise CommandRefused(f"{listen!r} is not {LOOPBACK}:PORT or a unix socket path")
        if api_key_file is None:
            raise CommandRefused("a TCP listener needs --api-key-file")
        argv += ["--host", host, "--port", port]
    if api_key_file is not None:
        argv += ["--api-key-file", str(api_key_file)]
    argv += ["--models-preset", str(preset.path), "--models-max", str(models_max), "--no-webui"]
    if not autoload:
        argv.append("--no-models-autoload")  # swap modes: only the gateway loads a model
    source = os.environ if environ is None else environ
    env = {name: source[name] for name in ENV_ALLOWLIST if name in source}
    env["GPU_LIGHT_SERVICE"] = RECLAIMER_NAME
    if llama_dir is not None:
        env["LD_LIBRARY_PATH"] = str(llama_dir)
    return argv, env
