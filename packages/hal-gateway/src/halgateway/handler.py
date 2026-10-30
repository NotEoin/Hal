"""The HTTP handler: limits, authentication, route match, dispatch to the relay.

Decisions:

- HTTP/1.1 keep-alive; the handler never reads a body before the peer check and the token
  check have passed, so a foreign peer costs no memory.
- A peer with another uid gets a minimal 403 written in `setup()`, then the socket closes
  (`verify_request` could only drop it silently, and REQ-GW-02 wants the 403).
- Every error is a `GatewayError` rendered in the route's format by `errors.error_body`.
- The body of a model route is parsed as JSON, its `model` rewritten to the upstream name and
  every other field kept (REQ-GW-11). Routes without a built handler answer 404 saying so.
"""

from __future__ import annotations

import http.server
import json
import logging
import time

from . import auth, policies, relay, reqlog, routes
from .backends import LlamaRouter
from .errors import (
    BadRequest,
    Forbidden,
    GatewayError,
    NotFound,
    PayloadTooLarge,
    SwapUnavailable,
    Unauthorized,
    error_body,
)
from .estimate import ESTIMATE_HEADER, estimate_input_tokens
from .policies import delegate, effort, overflow, swap
from .policies.text import conversation_key
from .routes import Api, Route

log = logging.getLogger(__name__)

HEALTH_BODY = json.dumps({"status": "ok"}).encode("ascii")
RELAYED = frozenset(
    {Api.CHAT, Api.RESPONSES, Api.RESPONSES_COUNT, Api.MESSAGES, Api.COUNT_TOKENS, Api.EMBEDDINGS}
)
ADMIN_BODY_LIMIT = 65536  # admin bodies are a few keys
STATUS_LENGTH_REQUIRED = 411
EPOCH_UTC = "1970-01-01T00:00:00Z"  # the backends report no creation time


class LengthRequired(GatewayError):
    """A POST without `Content-Length`."""

    status = STATUS_LENGTH_REQUIRED
    code = "length_required"


def _openai_entry(entry) -> dict:  # type: ignore[no-untyped-def,type-arg]
    doc: dict = {"id": entry.id, "object": "model", "created": 0, "owned_by": entry.owned_by}
    if entry.context_length:
        doc["context_length"] = entry.context_length
    if entry.profile:
        doc["meta"] = {"hal_profile": entry.profile}
    return doc


def _anthropic_entry(entry) -> dict:  # type: ignore[no-untyped-def,type-arg]
    return {"type": "model", "id": entry.id, "display_name": entry.id, "created_at": EPOCH_UTC}


class Handler(http.server.BaseHTTPRequestHandler):
    """Serves one connection; `self.server.gateway` supplies the gateway state."""

    protocol_version = "HTTP/1.1"
    server_version = "hal-gateway"

    def address_string(self) -> str:
        """Client address for logs; the unix socket has none."""
        return "unix" if not self.client_address else str(self.client_address[0])

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base class signature
        """Routes http.server's access lines to the module logger at debug level."""
        log.debug("%s %s", self.address_string(), format % args)

    def setup(self) -> None:
        """Sets the client timeout and records the unix peer's uid (REQ-GW-02)."""
        super().setup()
        gateway = self.server.gateway  # type: ignore[attr-defined]  # set by the server classes
        self.connection.settimeout(gateway.config["client_timeout_s"])
        self.peer: int | None = None
        if gateway.listener_kind(self.server) == "unix":
            self.peer = gateway.peer_uid(self.connection)
        self.foreign_peer = self.peer is not None and self.peer != gateway.own_uid

    def do_GET(self) -> None:  # noqa: N802 - http.server dispatch name
        """Handles a GET request."""
        self._handle()

    def do_POST(self) -> None:  # noqa: N802 - http.server dispatch name
        """Handles a POST request."""
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802 - http.server dispatch name
        """Handles a DELETE request."""
        self._handle()

    def _handle(self) -> None:
        gateway = self.server.gateway  # type: ignore[attr-defined]
        route = routes.match(self.command, self.path)
        fmt = route.fmt if route else "openai"
        self._begin(gateway)
        try:
            if self.foreign_peer:
                self.close_connection = True
                raise Forbidden("this socket serves its owner only")
            kind = gateway.listener_kind(self.server)
            if kind == "tcp":
                auth.check_host_and_origin(
                    self.headers,
                    self.server.server_address[1],
                    gateway.config["allow_hosts"],
                    gateway.config["allow_origins"],
                )
            if route is not None and route.api is Api.HEALTH:
                self._send(200, HEALTH_BODY)
                return
            principal = auth.principal_for(kind, self.peer, self.headers, gateway.owner_token)
            self.principal = principal
            self.rec["principal"] = (
                f"{principal.kind}:{kind}" if principal.kind == "owner" else principal.kind
            )
            if principal.kind == "anonymous":
                raise Unauthorized("missing API key")
            if route is None:
                raise NotFound(f"no route for {self.command} {self.path.split('?', 1)[0]}")
            self._dispatch(gateway, route)
        except GatewayError as exc:
            self.rec["error"] = exc.code
            retry = getattr(exc, "retry_after", None)
            extra = [("Retry-After", str(retry))] if retry else None
            self._send(exc.status, error_body(exc, fmt), extra=extra)
        finally:
            self._finish(gateway)

    def _begin(self, gateway) -> None:  # type: ignore[no-untyped-def]
        """Starts the record of one request (REQ-GW-70)."""
        self.started = time.monotonic()
        self.request_id = reqlog.request_id(self.headers.get("X-Request-Id"))
        self.tail = b""
        self.reply = bytearray()
        self.raw_in = b""
        self.rec: dict = {
            "ts": reqlog.utc_now(),
            "id": self.request_id,
            "listener": gateway.listener_kind(self.server),
            "principal": "anonymous",
            "method": self.command,
            "path": self.path.split("?", 1)[0],
            "alias": None,
            "backend": None,
            "upstream_model": None,
            "remote": False,
            "stream": False,
            "status": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "ttfb_ms": None,
            "duration_ms": 0.0,
            "usage": None,
            "error": None,
            "policy": None,
        }

    def _finish(self, gateway) -> None:  # type: ignore[no-untyped-def]
        """Completes and writes the record; never raises."""
        rec = self.rec
        rec["duration_ms"] = round((time.monotonic() - self.started) * 1000, 1)
        rec["usage"] = reqlog.scan_usage(self.tail)
        gateway.reqlog.write(rec)
        gateway.reqlog.write_content(self.request_id, self.raw_in, bytes(self.reply))

    def _dispatch(self, gateway, route: Route) -> None:  # type: ignore[no-untyped-def]
        if route.api is Api.MODELS:
            self._models(gateway, route)
            return
        if route.api is Api.OLLAMA_NATIVE:
            self._native(gateway, route)
            return
        if route.api is Api.ADMIN:
            self._admin(gateway, route)
            return
        if route.api not in RELAYED:
            raise NotFound(f"{route.api.value} routes are not available yet")
        body = self._read_json(int(gateway.config["max_request_bytes"]))
        model = body.get("model")
        if model is not None and not isinstance(model, str):
            raise BadRequest("model must be a string")
        target = gateway.resolver.resolve(model)
        backend, upstream_model = target.backend, target.model
        self._note_target(model, target, body)
        gateway.check_model(backend, upstream_model)
        if not backend.supports(route.api):
            raise NotFound(
                f"backend {backend.name} does not serve {route.api.value}; it serves: "
                + ", ".join(backend.api_names())
            )
        if route.api is Api.COUNT_TOKENS and getattr(backend, "estimates_count_tokens", False):
            doc = {"input_tokens": estimate_input_tokens(body)}
            self._send(200, json.dumps(doc).encode("ascii"), extra=[ESTIMATE_HEADER])
            return
        with gateway.gpu.request(target, self.principal):
            self._forward(gateway, route, target, body)

    def _forward(self, gateway, route: Route, target, body: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
        """Applies policies, waits for the swap scheduler, then relays; the GPU gate is held."""
        backend = target.backend
        model = self._apply_policies(gateway, route, target, body) or target.model
        scheduler = self._scheduler(backend, route)
        ticket = self._admit(scheduler, backend, model, body) if scheduler else None
        body["model"] = ticket.model if ticket else model
        if route.api is Api.CHAT and "options" in body and self._options(gateway, backend, body):
            return
        try:
            self._relay(gateway, backend, json.dumps(body).encode("utf-8"))
        finally:
            if scheduler and ticket:
                scheduler.finish(ticket, swap.last_stop_reason(self.tail))

    def _options(self, gateway, backend, body: dict) -> bool:  # type: ignore[no-untyped-def,type-arg]
        """Handles the Ollama `options` field of a chat request; True when the reply was sent.

        Ollama gets a non-streaming request through its native route (it has no OpenAI-route
        equivalent of `num_gpu`); every other backend gets the field stripped, since a strict
        OpenAI server may refuse an unknown one.
        """
        options = body.pop("options")
        if backend.type != "ollama" or body.get("stream"):
            return False
        if not isinstance(options, dict):
            raise BadRequest("options must be an object")
        timeout = float(gateway.config["upstream_read_timeout_s"])
        reply = backend.chat_with_options(body, options, timeout)
        self.rec["upstream_model"] = body["model"]
        self._send(200, json.dumps(reply).encode("utf-8"))
        return True

    @staticmethod
    def _scheduler(backend, route: Route):  # type: ignore[no-untyped-def]
        if route.api in (Api.MESSAGES, Api.CHAT) and isinstance(backend, LlamaRouter):
            return backend.scheduler()
        return None

    def _admit(self, scheduler, backend, model: str, body: dict):  # type: ignore[no-untyped-def,type-arg]
        """Waits for the swap scheduler (REQ-GW-46, 47); a timeout or failed swap is 503."""
        backend.ensure()
        try:
            ticket = scheduler.admit(model, body, conversation_key(body.get("messages") or []))
        except swap.SwapTimeout as exc:
            raise SwapUnavailable(str(exc)) from exc
        except swap.SwapError as exc:
            raise SwapUnavailable(str(exc), retry_after=1) from exc
        if ticket.model != model:
            self.rec["upstream_model"] = ticket.model
        if ticket.swap:
            self.rec["policy"] = {**(self.rec.get("policy") or {}), "swap": ticket.swap}
        return ticket

    def _apply_policies(self, gateway, route: Route, target, body: dict) -> str | None:  # type: ignore[no-untyped-def,type-arg]
        """Runs the policies of a router backend (messages and chat only, decision 6); returns the model."""
        backend = target.backend
        if route.api not in (Api.MESSAGES, Api.CHAT) or not isinstance(backend, LlamaRouter):
            return None
        services = policies.Services(
            effort.load_note, delegate.load_delegate_note, backend.classify, backend.turns
        )
        chosen = policies.select(
            target.alias, gateway.cfg.aliases.get(target.alias), backend.hal, target.model, services
        )
        ctx = policies.RequestContext(route.fmt, body, target.alias, target.model)
        policies.run_rewrites(chosen, ctx)
        if ctx.model != target.model:
            self.rec["upstream_model"] = ctx.model
        if ctx.decisions:
            self.rec["policy"] = ctx.decisions
        return ctx.model

    def _admin(self, gateway, route: Route) -> None:  # type: ignore[no-untyped-def]
        """Serves the router admin routes; only the owner on the unix socket may change anything."""
        principal = str(self.rec.get("principal", ""))
        if not principal.startswith("owner") or (route.owner_unix_only and principal != "owner:unix"):
            raise Forbidden("admin routes serve the owner on the unix socket only")
        router = gateway.router()
        name = route.path.removeprefix("/hal/v1/")
        if name == "gpu/release":
            body = self._read_json(ADMIN_BODY_LIMIT)
            flags = {k: body.get(k, False) for k in ("force", "stop_router", "reclaim")}
            if not all(isinstance(v, bool) for v in flags.values()):
                raise BadRequest("force, stop_router and reclaim must be booleans")
            result = gateway.gpu.release(**flags)
            self._send(200, json.dumps(result.as_json()).encode("ascii"))
            return
        if name == "status":
            backends = {
                n: (b.status() if hasattr(b, "status") else {"type": b.type})
                for n, b in gateway.backends.items()
            }
            doc = {
                "backends": backends,
                "in_flight": router.in_flight() if router else 0,
                "gpu": gateway.gpu.status(),
            }
            self._send(200, json.dumps(doc).encode("ascii"))
            return
        if router is None or name not in ("mode", "router/stop", "models/load", "models/unload"):
            raise NotFound(
                f"{route.path} is not available" + ("" if router else ": no managed llama-router backend")
            )
        body = {} if name == "router/stop" else self._read_json(ADMIN_BODY_LIMIT)
        if name in ("mode", "models/load"):
            gateway.gpu.check_peers()
        if name == "router/stop":
            router.stop()
        elif name == "mode":
            mode = body.get("mode")
            if not isinstance(mode, str) or not mode:
                raise BadRequest("mode must be a non-empty string")
            router.switch(mode, drain_s=float(gateway.config["drain_s"]), force=bool(body.get("force")))
        else:
            preset = body.get("model")
            if not isinstance(preset, str) or not preset:
                raise BadRequest("model must be a non-empty string")
            (router.load if name == "models/load" else router.unload)(preset)
        self._send(200, json.dumps({"backends": {router.name: router.status()}}).encode("ascii"))

    def _native(self, gateway, route: Route) -> None:  # type: ignore[no-untyped-def]
        """Serves the limited Ollama native routes (REQ-GW-24)."""
        path = self.path.split("?", 1)[0]
        if self.command == "GET":
            ollama = [b for b in gateway.backends.values() if b.type == "ollama"]
            if not ollama:
                raise NotFound("no Ollama backend is configured")
            if path == "/api/tags":
                self._send(200, json.dumps({"models": self._tags(gateway, ollama)}).encode("ascii"))
            else:
                self._relay(gateway, ollama[0], None)
            return
        body = self._read_json(int(gateway.config["max_request_bytes"]))
        requested = body.get("model")
        if requested is not None and not isinstance(requested, str):
            raise BadRequest("model must be a string")
        target = gateway.resolver.resolve(requested)
        if not target.backend.supports(Api.OLLAMA_NATIVE):
            raise NotFound(
                f"backend {target.backend.name} does not serve ollama_native; it serves: "
                + ", ".join(target.backend.api_names())
            )
        self._note_target(requested, target, body)
        gateway.check_model(target.backend, target.model)
        body["model"] = target.model
        self._relay(gateway, target.backend, json.dumps(body).encode("utf-8"))

    def _note_target(self, alias, target, body: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
        self.rec.update(
            alias=alias,
            backend=target.backend.name,
            upstream_model=target.model,
            remote=bool(target.backend.remote),
            stream=body.get("stream") is True,
        )

    @staticmethod
    def _tags(gateway, ollama) -> list[dict]:  # type: ignore[no-untyped-def,type-arg]
        """Exposed Ollama models plus the aliases that point at Ollama (design: `/api/tags`)."""
        entries = [m for backend in ollama if backend.expose_models for m in backend.tags()]
        by_name = {m.get("name"): m for m in entries}
        for name, alias in gateway.cfg.aliases.items():
            upstream = by_name.get(alias["model"])
            if gateway.backends[alias["backend"]].type == "ollama" and upstream is not None:
                entries.append({**upstream, "name": name, "model": name})
        return entries

    def _models(self, gateway, route: Route) -> None:  # type: ignore[no-untyped-def]
        """Answers `GET /v1/models` and `/v1/models/{id}` in the OpenAI or Anthropic shape."""
        anthropic = "anthropic-version" in self.headers
        entries = gateway.resolver.listing()
        if route.path.endswith("{id}"):
            wanted = self.path.split("?", 1)[0][len("/v1/models/") :]
            entries = [e for e in entries if e.id == wanted]
            if not entries:
                raise NotFound(f"model {wanted!r} not found")
        data = [_anthropic_entry(e) if anthropic else _openai_entry(e) for e in entries]
        if route.path.endswith("{id}"):
            doc: dict = data[0]
        elif anthropic:
            doc = {
                "data": data,
                "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            }
        else:
            doc = {"object": "list", "data": data}
        self._send(200, json.dumps(doc).encode("ascii"))

    def _read_json(self, limit: int) -> dict:  # type: ignore[type-arg]
        if "Transfer-Encoding" in self.headers:
            raise BadRequest("chunked request bodies are not supported")
        length = self.headers.get("Content-Length")
        if length is None:
            raise LengthRequired("POST needs a Content-Length")
        if not length.isdigit():
            raise BadRequest("Content-Length is not a number")
        if int(length) > limit:
            self.close_connection = True  # the body is never read, so the stream is out of sync
            raise PayloadTooLarge(f"request body is larger than {limit} bytes")
        raw = self.rfile.read(int(length))
        self.raw_in = raw
        self.rec["bytes_in"] = len(raw)
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise BadRequest(f"request body is not JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise BadRequest("request body must be a JSON object")
        return doc

    def _relay(self, gateway, backend, body: bytes | None) -> None:  # type: ignore[no-untyped-def]
        if isinstance(backend, LlamaRouter):
            backend.ensure()
            with backend.request():
                self._relay_once(gateway, backend, body)
        else:
            self._relay_once(gateway, backend, body)

    def _relay_once(self, gateway, backend, body: bytes | None) -> None:  # type: ignore[no-untyped-def]
        cfg = gateway.config
        conn = relay.connect(
            backend.address(), cfg["upstream_connect_timeout_s"], cfg["upstream_read_timeout_s"]
        )
        headers = relay.request_headers(self.headers, backend.auth_headers())
        if body is not None:
            headers["Content-Length"] = str(len(body))
        try:
            resp = relay.open_response(conn, self.command, self.path, body, headers)
            if resp.status == 400 and isinstance(backend, LlamaRouter) and self._send_overflow(resp):
                return
            self.send_response_only(resp.status)
            self.rec["status"] = resp.status
            self.send_header("X-Request-Id", self.request_id)
            if backend.remote:
                self.send_header("X-Hal-Remote", backend.address().host)
            length_known = False
            for name, value in relay.response_headers(resp.getheaders()):
                if name.lower() in ("date", "server"):
                    continue
                length_known = length_known or name.lower() == "content-length"
                self.send_header(name, value)
            if not length_known:
                self.close_connection = True  # a close-delimited body
                self.send_header("Connection", "close")
            self.end_headers()
            if not relay.copy_body(resp, self._write):
                self.close_connection = True
        finally:
            conn.close()

    def _send_overflow(self, resp) -> bool:  # type: ignore[no-untyped-def]
        """Maps a context overflow 400 (REQ-GW-40); False when the route or body is not one."""
        route = routes.match(self.command, self.path)
        if route is None or route.api not in (Api.MESSAGES, Api.CHAT):
            return False
        raw = resp.read(overflow.MAX_BODY_BYTES + 1)
        mapped = overflow.map_overflow(route.fmt, 400, raw)
        payload = raw if mapped is None else mapped
        self.rec["status"] = 400
        self.send_response_only(400)
        self.send_header("X-Request-Id", self.request_id)
        for name, value in relay.response_headers(resp.getheaders()):
            if name.lower() not in ("date", "server", "content-length", "connection"):
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write(payload)
        return True

    def _write(self, chunk: bytes) -> None:
        if self.rec["ttfb_ms"] is None:
            self.rec["ttfb_ms"] = round((time.monotonic() - self.started) * 1000, 1)
        self.rec["bytes_out"] += len(chunk)
        self.tail = (self.tail + chunk)[-reqlog.USAGE_TAIL_BYTES :]
        if self.server.gateway.reqlog.content and len(self.reply) < reqlog.MAX_CONTENT_BYTES:  # type: ignore[attr-defined]
            self.reply += chunk
        self.wfile.write(chunk)
        self.wfile.flush()

    def _send(self, status: int, body: bytes, extra: list[tuple[str, str]] | None = None) -> None:
        self.rec["status"] = status
        self.send_response(status)
        self.send_header("X-Request-Id", self.request_id)
        for name, value in extra or []:
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self._write(body)
