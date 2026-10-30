"""Gateway error tree and the error body each API format expects.

A handler raises one of these; `error_body` renders it in the shape the client's API
understands (REQ-GW-0x/1x), so an SDK shows the message instead of a parse failure. Decision:
the status lives on the exception class, not at the raise site, so one error means one status
everywhere.
"""

from __future__ import annotations

import json

FORMATS = ("openai", "anthropic", "ollama")
ANTHROPIC_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    409: "invalid_request_error",
    413: "request_too_large",
    429: "rate_limit_error",
    503: "overloaded_error",
}


class GatewayError(Exception):
    """Base of every error the gateway turns into an HTTP response."""

    status = 500
    code = "gateway_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BadRequest(GatewayError):
    """The request is malformed or names something the gateway cannot serve."""

    status = 400
    code = "bad_request"


class Unauthorized(GatewayError):
    """No token, or a token that does not match."""

    status = 401
    code = "unauthorized"


class Forbidden(GatewayError):
    """The caller is known but not allowed here (peer uid, Host, Origin, scope)."""

    status = 403
    code = "forbidden"


class NotFound(GatewayError):
    """No such route, model or backend API."""

    status = 404
    code = "not_found"


class PayloadTooLarge(GatewayError):
    """The body is larger than the limit for this principal."""

    status = 413
    code = "payload_too_large"


class TooManyRequests(GatewayError):
    """A rate, concurrency or connection cap was hit."""

    status = 429
    code = "rate_limited"


class BadGatewayError(GatewayError):
    """The backend answered badly or not at all."""

    status = 502
    code = "bad_gateway"


class Unavailable(GatewayError):
    """The backend is down, loading or switched off."""

    status = 503
    code = "unavailable"


class SwapUnavailable(Unavailable):
    """A model swap timed out or failed; the client may retry."""

    code = "swap_unavailable"
    retry_after = 30  # seconds; a swap takes 17 to 60 s on the reference machine

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        if retry_after is not None:
            self.retry_after = retry_after


class ModelUnverified(Unavailable):
    """The model is not pinned in `models.toml`, or its file or digest changed (REQ-GW-77)."""

    code = "model_unverified"


def error_body(err: GatewayError, fmt: str = "openai") -> bytes:
    """Returns the JSON error body of `err` in API format `fmt` (`openai`, `anthropic`, `ollama`)."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown error format {fmt!r}; use one of {list(FORMATS)}")
    if fmt == "anthropic":
        kind = ANTHROPIC_TYPES.get(err.status, "api_error")
        doc: dict[str, object] = {"type": "error", "error": {"type": kind, "message": err.message}}
    elif fmt == "ollama":
        doc = {"error": err.message}
    else:
        doc = {"error": {"message": err.message, "type": err.code, "code": err.code}}
    return json.dumps(doc).encode("ascii")


class UpstreamTimeout(GatewayError):
    """The backend did not connect or answer in time, before any byte of the reply."""

    status = 504
    code = "upstream_timeout"


class Conflict(GatewayError):
    """A request that names more than one possible target."""

    status = 409
    code = "ambiguous_model"


class RouterBusy(Conflict):
    """A mode switch found requests still running on the router and `force` was not set."""

    code = "router_busy"
