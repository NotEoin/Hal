"""Desktop side of pairing: authorize, revoke and list clients (REQ-RMT-06 to 13, 46; design 07).

`authorize` runs the server checks, writes the two restricted `authorized_keys` lines, turns on
the gateway's loopback TCP listener, obtains a named gateway token and holds it for one-time
delivery by the `hello` verb. Every step that changes something is undone when a later step
fails, so a failed `authorize` leaves the desktop as it was (design 07, failure table).

Constraints. The `hal` program is the only way to reach the gateway: this package imports only
`halkit`. The registry `clients.toml` is rewritten whole under a lock; it holds fingerprints and
names, never a key or a token. The gateway-TCP check SC17 cannot block the first `authorize`
because this module is what turns the listener on; it is reported as a warning instead.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from halkit import config as kitconfig
from halkit import files, paths, timeutil

from . import authkeys, netrange, serve, servercheck
from . import config as cfg
from .keys import PublicKey, Runner, parse_public_key
from .model import ClientRecord, Forward, check_client_name

log = logging.getLogger(__name__)

SCHEMA_REQUEST = "hal.remote.authorize-request/1"
SCHEMA_RESPONSE = "hal.remote.authorize/1"
SCHEMA_REGISTRY = 1
GATEWAY_PORT = 8741  # 05-gateway `[gateway].tcp_port` default; REQ-RMT-47 forwards the same number
HAL_TIMEOUT_S = 15  # design 07: `hal gateway token` and the TCP switch
TOKEN_PREFIX = "remote-"
FORBIDDEN_PATH_CHARS = ('"', "\\", "\n", "\r", "\0")
TCP_SWITCH_ARGV = ("config", "set", "gateway.tcp", "true")  # I2: lockdown is set with `hal config set`
NOT_BLOCKING = frozenset({"SC17"})  # authorize is what turns the listener on


class AuthorizeError(RuntimeError):
    """`authorize` refused or failed; the message says what to do. State is rolled back."""


@dataclass(frozen=True)
class AuthorizeRequest:
    """The validated request of a laptop (design 07, Authorize request)."""

    client: str
    tunnel_key: PublicKey
    mcp_key: PublicKey
    from_pattern: str | None = None  # None: derive from the client address; "any": omit
    expires: date | None = None
    rotate: bool = False

    @classmethod
    def from_json(cls, text: str) -> AuthorizeRequest:
        """Parses and validates the JSON of a request; raises `AuthorizeError` on any problem."""
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise AuthorizeError(f"request is not JSON: {exc}") from None
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_REQUEST:
            raise AuthorizeError(f"request schema must be {SCHEMA_REQUEST}")
        return cls.build(
            client=str(data.get("client", "")),
            tunnel_key=str(data.get("tunnel_key", "")),
            mcp_key=str(data.get("mcp_key", "")),
            from_pattern=data.get("from"),
            expires=data.get("expires"),
            rotate=data.get("rotate", False),
        )

    @classmethod
    def build(
        cls,
        *,
        client: str,
        tunnel_key: str,
        mcp_key: str,
        from_pattern: object = None,
        expires: object = None,
        rotate: object = False,
    ) -> AuthorizeRequest:
        """Validates raw field values; raises `AuthorizeError`."""
        try:
            check_client_name(client)
            tunnel = parse_public_key(tunnel_key)
            mcp = parse_public_key(mcp_key)
        except ValueError as exc:
            raise AuthorizeError(str(exc)) from None
        if tunnel.blob == mcp.blob:
            raise AuthorizeError("the tunnel key and the mcp key must be different keys")
        if from_pattern is not None and not isinstance(from_pattern, str):
            raise AuthorizeError("from must be a string or null")
        if not isinstance(rotate, bool):
            raise AuthorizeError("rotate must be true or false")
        day = None
        if expires:
            try:
                day = date.fromisoformat(str(expires))
            except ValueError:
                raise AuthorizeError("expires must be YYYY-MM-DD") from None
        return cls(client, tunnel, mcp, from_pattern, day, rotate)


@dataclass
class Resolved:
    """Absolute executables that are written into `command=` or run."""

    serve_path: Path
    hub_path: Path
    hal_path: Path


@dataclass
class Undo:
    """What a partly finished `authorize` has changed, to put back on failure."""

    steps: list[Any] = field(default_factory=list)

    def add(self, step: Any) -> None:
        """Registers a rollback step."""
        self.steps.append(step)

    def run(self) -> list[str]:
        """Runs the steps in reverse order and returns the problems they raised."""
        problems: list[str] = []
        for step in reversed(self.steps):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - rollback must try every step and report, not stop
                problems.append(str(exc))
        return problems


def registry_path() -> Path:
    """Where the client registry lives (`state_dir()/remote/clients.toml`)."""
    return paths.state_dir() / "remote" / "clients.toml"


def _quote(text: str) -> str:
    return json.dumps(text)  # a JSON string is a valid TOML basic string, and it escapes non-ASCII


def _dump(records: Collection[ClientRecord]) -> str:
    out = [f"schema = {SCHEMA_REGISTRY}\n"]
    for r in sorted(records, key=lambda rec: rec.name):
        out += [
            f"\n[clients.{r.name}]\n",
            f"created = {_quote(r.created)}\n",
            f"updated = {_quote(r.updated)}\n",
            f"from = {_quote(r.from_pattern)}\n",
            f"expires = {_quote(r.expires)}\n",
            f"tunnel_fingerprint = {_quote(r.tunnel_fingerprint)}\n",
            f"mcp_fingerprint = {_quote(r.mcp_fingerprint)}\n",
            f"forwards = [{', '.join(str(p) for p in r.forwards)}]\n",
            f"gateway_token_name = {_quote(r.gateway_token_name)}\n",
            f"accepted_risks = [{', '.join(_quote(x) for x in r.accepted_risks)}]\n",
        ]
    return "".join(out)


def clients(path: Path | None = None) -> list[ClientRecord]:
    """The registered clients, sorted by name; raises `AuthorizeError` for an unknown schema."""
    path = path or registry_path()
    data = kitconfig.read_toml(path)
    if data and data.get("schema") != SCHEMA_REGISTRY:
        raise AuthorizeError(f"{path}: unsupported registry schema {data.get('schema')!r}")
    table = data.get("clients", {})
    out = []
    for name, row in sorted(table.items()):  # type: ignore[union-attr]  # read_toml returns plain dicts
        out.append(
            ClientRecord(
                name=name,
                created=str(row.get("created", "")),
                updated=str(row.get("updated", "")),
                from_pattern=str(row.get("from", "")),
                expires=str(row.get("expires", "")),
                tunnel_fingerprint=str(row.get("tunnel_fingerprint", "")),
                mcp_fingerprint=str(row.get("mcp_fingerprint", "")),
                forwards=tuple(int(p) for p in row.get("forwards", [])),
                gateway_token_name=str(row.get("gateway_token_name", "")),
                accepted_risks=tuple(str(x) for x in row.get("accepted_risks", [])),
            )
        )
    return out


def _save_registry(records: Collection[ClientRecord], path: Path | None = None) -> None:
    path = path or registry_path()
    files.write_atomic(path, _dump(records), mode=0o600)


def _resolve_exe(configured: str, name: str, fallback: Path | None = None) -> Path:
    """An absolute path of an existing executable, never containing a quote or backslash."""
    candidate = configured or (str(fallback) if fallback else "") or shutil.which(name) or ""
    if not candidate or any(c in candidate for c in FORBIDDEN_PATH_CHARS):
        raise AuthorizeError(
            f"cannot resolve {name} to a safe path; set [remote.server].{name.replace('-', '_')}_path"
        )
    path = Path(candidate).expanduser()
    if not (path.is_absolute() and path.is_file() and os.access(path, os.X_OK)):
        raise AuthorizeError(f"{name} is not an absolute executable path: {candidate}")
    return path


def resolve_paths(settings: Mapping[str, object] | None = None) -> Resolved:
    """Resolves `hal-remote`, `hal-hub` and `hal` from `[remote.server]` or the install."""
    s = settings if settings is not None else kitconfig.load(cfg.SERVER_TABLE)
    here = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    return Resolved(
        serve_path=_resolve_exe(
            str(s.get("serve_path", "")), "hal-remote", here if here and here.name == "hal-remote" else None
        ),
        hub_path=_resolve_exe(str(s.get("hub_path", "")), "hal-hub"),
        hal_path=_resolve_exe(str(s.get("hal_path", "")), "hal"),
    )


def host_keys(etc_ssh: Path = Path("/etc/ssh")) -> list[str]:
    """The public host keys of sshd, ed25519 first (world-readable files)."""
    found = sorted(etc_ssh.glob("ssh_host_*_key.pub"), key=lambda p: (("ed25519" not in p.name), p.name))
    return [p.read_text(encoding="utf-8").strip() for p in found]


def _hal(runner: Runner, hal: Path, *args: str) -> str:
    """Runs `hal ARGS`; returns stdout, raises `AuthorizeError` with the first stderr line on failure."""
    try:
        proc = runner([str(hal), *args], timeout=HAL_TIMEOUT_S, check=False)
    except OSError as exc:
        raise AuthorizeError(f"cannot run {hal}: {exc}") from None
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise AuthorizeError(
            f"hal {' '.join(args[:3])} failed (exit {proc.returncode}): {err[0] if err else ''}"
        )
    return proc.stdout.decode("utf-8", "replace")


def _gateway_tcp_on() -> bool:
    data = kitconfig.read_toml(paths.config_file())
    gateway = data.get("gateway", {})
    return bool(isinstance(gateway, dict) and gateway.get("tcp"))


def issue_token(runner: Runner, hal: Path, client: str, *, rotate: bool = False) -> str:
    """Issues (or with `rotate` replaces) the named gateway token of `client` and returns it."""
    name = TOKEN_PREFIX + client
    args = ["gateway", "token", "issue", "--name", name, "--json"] + (["--rotate"] if rotate else [])
    try:
        reply = json.loads(_hal(runner, hal, *args))
        token = reply["token"]
    except (ValueError, KeyError, TypeError):
        raise AuthorizeError("hal gateway token issue returned no token") from None
    if not isinstance(token, str) or not token:
        raise AuthorizeError("hal gateway token issue returned an empty token")
    return token


def write_delivery(client: str, token: str) -> Path:
    """Holds `token` for one-time delivery by the `hello` verb (0600, atomic)."""
    directory = serve.delivery_dir()
    paths.ensure_private_dir(directory.parent)
    paths.ensure_private_dir(directory)
    target = directory / f"{client}.json"
    body = {"schema": serve.SCHEMA_DELIVERY, "created": timeutil.iso_z(), "tokens": {"gateway": token}}
    files.write_atomic(target, json.dumps(body), mode=0o600)
    return target


def _tagged_lines(text: str, client: str) -> list[str]:
    return [ln for ln in text.splitlines() if authkeys._is_tagged(ln, client)]  # noqa: SLF001 - same package


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8") if path.exists() else ""


def authorize(
    req: AuthorizeRequest,
    *,
    client_addr: str | None,
    runner: Runner,
    accept_risks: frozenset[str],
    authorized_keys: Path,
    resolved: Resolved | None = None,
    etc_ssh: Path = Path("/etc/ssh"),
    home: Path | None = None,
    settings: Mapping[str, object] | None = None,
    check_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Authorizes `req.client` and returns the response dict; raises `AuthorizeError`, state restored."""
    settings = settings if settings is not None else kitconfig.load(cfg.SERVER_TABLE)
    exe = resolved or resolve_paths(settings)
    known = {c.name: c for c in clients()}
    existing = known.get(req.client)
    if existing is not None and not req.rotate:
        raise AuthorizeError(f"client {req.client!r} exists; pass --rotate to replace its keys")
    pattern = _from_pattern(req.from_pattern, client_addr, settings)

    results = servercheck.run_checks(
        home=home or Path.home(),
        etc_ssh=etc_ssh,
        runner=runner,
        registry_clients=set(known),
        registry_from_any={n for n, c in known.items() if c.from_pattern == "any"},
        delivery_dir=serve.delivery_dir(),
        delivery_ttl_s=int(settings["delivery_ttl_s"]),  # type: ignore[call-overload]
        **(check_options or {}),
    )
    blocked = sorted(
        r.id
        for r in results
        if r.severity == "critical" and r.status == "fail" and r.id not in accept_risks | NOT_BLOCKING
    )
    if blocked:
        fixes = "; ".join(f"{r.id}: {r.fix}" for r in results if r.id in blocked)
        raise AuthorizeError(
            f"critical checks failed: {', '.join(blocked)}. {fixes} (or pass --accept-risk ID)"
        )

    forwards = [Forward("gateway", GATEWAY_PORT, GATEWAY_PORT)]
    undo = Undo()
    try:
        before = _read(authorized_keys)
        previous = _tagged_lines(before, req.client)
        lines = [
            authkeys.render_line(
                role,
                client=req.client,
                key=key,
                from_pattern=pattern,
                forwards=forwards,
                serve_path=exe.serve_path,
                hub_path=exe.hub_path,
                expires=req.expires,
            )
            for role, key in (("tunnel", req.tunnel_key), ("mcp", req.mcp_key))
        ]
        authkeys.update(authorized_keys, req.client, lines)
        undo.add(lambda: authkeys.update(authorized_keys, req.client, previous))

        tcp_enabled = False
        if not _gateway_tcp_on():
            _hal(runner, exe.hal_path, *TCP_SWITCH_ARGV)
            tcp_enabled = True
            undo.add(lambda: _hal(runner, exe.hal_path, "config", "set", "gateway.tcp", "false"))

        token = issue_token(runner, exe.hal_path, req.client, rotate=existing is not None)
        name = TOKEN_PREFIX + req.client
        if existing is None:
            undo.add(lambda: _hal(runner, exe.hal_path, "gateway", "token", "revoke", "--name", name))
        write_delivery(req.client, token)
        undo.add(lambda: (serve.delivery_dir() / f"{req.client}.json").unlink(missing_ok=True))

        now = timeutil.iso_z()
        record = ClientRecord(
            name=req.client,
            created=existing.created if existing else now,
            updated=now,
            from_pattern=pattern if pattern is not None else "any",
            expires=req.expires.isoformat() if req.expires else "",
            tunnel_fingerprint=req.tunnel_key.fingerprint(),
            mcp_fingerprint=req.mcp_key.fingerprint(),
            forwards=tuple(f.remote_port for f in forwards),
            gateway_token_name=name,
            accepted_risks=tuple(sorted(accept_risks)),
        )
        _save_registry([c for n, c in known.items() if n != req.client] + [record])
    except Exception as exc:
        problems = undo.run()
        if problems:
            log.error("rollback problems: %s", "; ".join(problems))
        if isinstance(exc, AuthorizeError):
            if problems:
                raise AuthorizeError(f"{exc}; could not undo: {'; '.join(problems)}") from exc
            raise
        raise AuthorizeError(f"authorize failed and was rolled back: {exc}") from exc

    warnings = sorted(
        r.id
        for r in results
        if r.status == "fail" and r.severity != "critical" or r.id in NOT_BLOCKING and r.status == "fail"
    )
    return {
        "schema": SCHEMA_RESPONSE,
        "client": req.client,
        "time": timeutil.iso_z(),
        "server": serve._server_info(),  # noqa: SLF001 - same package
        "host_keys": host_keys(etc_ssh),
        "from": record.from_pattern,
        "forwards": [serve.GATEWAY_FORWARD],
        "checks": {"ok": True, "accepted_risks": sorted(accept_risks), "warnings": warnings},
        "gateway_tcp_enabled": tcp_enabled,
        "token_pending": True,
    }


def _from_pattern(
    requested: str | None, client_addr: str | None, settings: Mapping[str, object]
) -> str | None:
    """The `from=` pattern to write; `None` means omit (`any`)."""
    if requested == "any":
        log.warning("from=any: the key works from every address")
        return None
    if requested:
        try:
            return netrange.validate(requested)
        except ValueError as exc:
            raise AuthorizeError(str(exc)) from None
    if not client_addr:
        raise AuthorizeError("cannot tell where the client connects from: pass --from PATTERN")
    try:
        return netrange.derive(
            client_addr,
            v4_prefix=int(settings["from_prefix_v4"]),  # type: ignore[call-overload]
            v6_prefix=int(settings["from_prefix_v6"]),  # type: ignore[call-overload]
        )
    except ValueError as exc:
        raise AuthorizeError(str(exc)) from None


def revoke(
    client: str, *, runner: Runner, authorized_keys: Path, resolved: Resolved | None = None
) -> list[str]:
    """Removes everything of `client`; returns what could not be removed (empty on success)."""
    check_client_name(client)
    problems: list[str] = []
    try:
        authkeys.update(authorized_keys, client, [])
    except (OSError, TimeoutError, ValueError) as exc:
        problems.append(f"authorized_keys: {exc}")
    try:
        (serve.delivery_dir() / f"{client}.json").unlink(missing_ok=True)
    except OSError as exc:
        problems.append(f"delivery: {exc}")
    try:
        records = clients()
        _save_registry([c for c in records if c.name != client])
    except (OSError, AuthorizeError) as exc:
        problems.append(f"registry: {exc}")
    try:
        hal = (resolved or resolve_paths()).hal_path
        _hal(runner, hal, "gateway", "token", "revoke", "--name", TOKEN_PREFIX + client)
    except AuthorizeError as exc:
        problems.append(f"gateway token: {exc}")
    return problems


def update_from(client: str, pattern: str, *, authorized_keys: Path, resolved: Resolved) -> None:
    """Changes only the `from=` option of an existing client's lines and its registry entry."""
    known = {c.name: c for c in clients()}
    record = known.get(client)
    if record is None:
        raise AuthorizeError(f"no client named {client!r}")
    value = None if pattern == "any" else netrange.validate(pattern)
    parsed, _ = authkeys.parse(_read(authorized_keys))
    mine = {ln.role: ln for ln in parsed if ln.client == client}
    if set(mine) != {"tunnel", "mcp"}:
        raise AuthorizeError(f"{client}: the tunnel and mcp lines are not both present; authorize it again")
    expires = date.fromisoformat(record.expires) if record.expires else None
    forwards = [Forward("gateway", p, p) for p in record.forwards]
    lines = [
        authkeys.render_line(
            role, client=client, key=mine[role].key, from_pattern=value, forwards=forwards,
            serve_path=resolved.serve_path, hub_path=resolved.hub_path, expires=expires,
        )
        for role in ("tunnel", "mcp")
    ]  # fmt: skip
    authkeys.update(authorized_keys, client, lines)
    changed = ClientRecord(
        **{**record.__dict__, "from_pattern": pattern if value else "any", "updated": timeutil.iso_z()}
    )
    _save_registry([c for n, c in known.items() if n != client] + [changed])


def rotate_token(client: str, *, runner: Runner, resolved: Resolved) -> str:
    """Issues a new token for a registered `client`, holds it for delivery and returns it."""
    if client not in {c.name for c in clients()}:
        raise AuthorizeError(f"no client named {client!r}")
    token = issue_token(runner, resolved.hal_path, client, rotate=True)
    write_delivery(client, token)
    return token


def hostname() -> str:
    """The desktop's host name, for messages."""
    return socket.gethostname()
