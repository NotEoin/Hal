"""How a box reaches the model: a scoped gateway session, or Ollama when no gateway runs.

`route` decides before the box starts (the model name goes into the agent's run file); `open`
serves the box's model socket once the session has made it. Design decisions:

- With the gateway's socket present, the box asks for the role alias (`hal-code` for coding,
  `hal-chat` for chat), so it gets the model, context and effort `hal tune` set. Its model
  socket is a `halgateway.forwarder.SessionForwarder` holding a session token minted for this
  box with `allow_remote = false`: the gateway refuses a remote backend to that token, so a box
  cannot reach a cloud model whatever it asks for, and the forwarder's budget (05 REQ-GW-69,
  `[gateway.sessions]`) bounds a looping agent.
- A rejected token (a gateway restart ends every session) is replaced by a fresh one.
- Without the gateway, or with an explicit upstream URL (`--upstream`, `HAL_CODE_UPSTREAM`), the
  box talks to that server through `halsandbox.forwarder.ModelForwarder`, as before, under the
  `[hal]` model name; a log line says the role settings do not apply there.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from halgateway import client as gwclient
from halgateway.config import GATEWAY_TABLE, SESSIONS_TABLE
from halgateway.forwarder import ForwarderEvent, ForwarderLimits, SessionForwarder
from halkit import config as kitconfig
from halkit import paths
from halsandbox.errors import SandboxError
from halsandbox.forwarder import ModelForwarder

from . import config

log = logging.getLogger(__name__)

SOCKET_NAME = "gateway.sock"
UPSTREAM_ENV = "HAL_CODE_UPSTREAM"
MODEL_ENV = "HAL_AGENT_MODEL"
# the role a box kind asks the gateway for, and the `[hal]` key used straight against Ollama
ROLE_ALIAS = {"code": "hal-code", "job": "hal-code", "chat": "hal-chat"}
OLLAMA_KEY = {"code": "code_model", "job": "code_model", "chat": "chat_model"}


@dataclass(frozen=True)
class Route:
    """Where a box's model requests go: the gateway (`upstream` empty) or a server URL."""

    model: str
    upstream: str = ""
    gateway_socket: Path | None = None

    @property
    def via_gateway(self) -> bool:
        """True when the box is served through a gateway session."""
        return self.gateway_socket is not None

    def describe(self) -> str:
        """`the gateway (/run/.../gateway.sock)` or the URL, for log lines."""
        return f"the gateway ({self.gateway_socket})" if self.gateway_socket else self.upstream


@dataclass
class Link:
    """A served model socket; `close` stops the forwarder and ends the gateway session."""

    route: Route
    close: Callable[[], None]


def gateway_socket() -> Path | None:
    """The gateway's unix socket when it exists (the gateway is running), else None."""
    configured = str(kitconfig.load(GATEWAY_TABLE)["unix_path"])
    try:
        path = Path(configured).expanduser() if configured else paths.runtime_dir() / SOCKET_NAME
    except paths.RuntimeDirMissing:
        return None
    return path if path.is_socket() else None


def route(kind: str, *, upstream: str | None = None, model: str | None = None) -> Route:
    """The route for a `code`, `job` or `chat` box; an explicit `upstream` or `model` wins."""
    shared = config.shared()
    upstream = upstream or os.environ.get(UPSTREAM_ENV) or ""
    sock = None if upstream else gateway_socket()
    if sock is not None:
        return Route(model or os.environ.get(MODEL_ENV) or ROLE_ALIAS.get(kind, "hal-chat"), "", sock)
    named = str(shared.get(OLLAMA_KEY.get(kind, "chat_model")) or shared["chat_model"])
    found = Route(model or os.environ.get(MODEL_ENV) or named, upstream or str(shared["ollama_url"]))
    if not upstream:
        log.warning(
            "the Hal gateway is not running; the box uses %s at %s directly, without the context "
            "and settings hal tune set (start it with hal-gateway serve)", found.model, found.upstream,
        )  # fmt: skip
    return found


def _limits() -> ForwarderLimits:
    table = kitconfig.load(SESSIONS_TABLE)
    return ForwarderLimits(
        max_request_bytes=int(table["max_request_bytes"]),  # type: ignore[call-overload]
        max_requests_per_min=int(table["max_requests_per_min"]),  # type: ignore[call-overload]
        max_output_tokens=int(table["max_output_tokens"]),  # type: ignore[call-overload]
        max_tokens_per_hour=int(table["max_tokens_per_hour"]),  # type: ignore[call-overload]
    )


def open(found: Route, model_socket: Path, label: str) -> Link:  # noqa: A001 - called as `modellink.open`
    """Serves `model_socket` along `found`; raises `SandboxError` when the gateway refuses a session."""
    if not found.via_gateway:
        plain = ModelForwarder(model_socket, found.upstream)
        plain.start()
        return Link(found, plain.stop)
    assert found.gateway_socket is not None
    owner = gwclient.GatewayClient(found.gateway_socket, "")  # the socket's peer uid proves the owner
    try:
        grant = owner.create_session(label, allow_remote=False)
    except gwclient.ClientError as exc:
        raise SandboxError(
            f"the Hal gateway refused a session for the box ({exc}); restart it with "
            "hal-gateway serve, or pass --upstream URL to use a model server directly"
        ) from exc
    held = [grant.id]

    def renew(event: ForwarderEvent) -> None:
        if event.kind != "token_rejected":
            return
        fresh = owner.create_session(label, allow_remote=False)
        held[0] = fresh.id
        forwarder.set_token(fresh.token)
        log.info("gateway session for %s renewed after its token was rejected", label)

    forwarder = SessionForwarder(model_socket, found.gateway_socket, grant.token, limits=_limits(), on_event=renew)
    forwarder.start()

    def close() -> None:
        forwarder.close()
        try:
            owner.revoke_session(held[0])
        except gwclient.ClientError as exc:
            # the gateway may have stopped first; its sessions end with it
            log.debug("could not revoke gateway session %s: %s", held[0], exc)

    return Link(found, close)
