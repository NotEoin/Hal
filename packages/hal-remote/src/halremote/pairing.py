"""Laptop side of pairing: `hal remote setup` in its bootstrap and manual flows (design 07, Pairing flow).

Responsible for key creation, the `authorize` exchange over the user's own SSH access, host key
pinning in a per-profile `known_hosts`, the `hello` call that delivers the token once, and the
files that follow (`profile.toml`, `ssh_config`, `gateway-token`, the `[gateway]` client-mode
lines). Every process runs through an injected `Runner` with a timeout, so the tests use fakes.

Decisions:
- The manual flow fetches the host key with `ssh-keyscan` and asks the user to compare its
  fingerprint, instead of `StrictHostKeyChecking=ask`: Hal runs ssh without a terminal
  (`BatchMode`), where `ask` cannot prompt. The pinned key and the check are the same.
- The bootstrap `authorize` call uses `BatchMode=yes`: there is no terminal for a password
  prompt, so the user's key must already work (agent or key without passphrase). Reason:
  a prompt that cannot be answered would hang until the timeout.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from halkit import files, paths, proc, timeutil

from . import authorize, sshconf
from .keys import PublicKey, generate, parse_public_key
from .model import Forward, Profile, ProfilePaths, TunnelSettings, check_client_name, profile_paths

log = logging.getLogger(__name__)

SCHEMA_PROFILE = 1
SSH_TIMEOUT_S = 60.0  # the desktop may be waking up; authorize itself runs in a few seconds
KEYSCAN_TIMEOUT_S = 15.0
DEFAULT_REMOTE_HAL = "~/.local/bin/hal-remote"  # design 07: not on PATH for a non-interactive bash
DEFAULT_PROFILE = "desktop"
DEFAULT_SSH_PORT = 22
GATEWAY_LOCAL_PORT = authorize.GATEWAY_PORT
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
GATEWAY_TABLE_RE = re.compile(r"^\s*\[gateway\]\s*$", re.MULTILINE)

Runner = Callable[..., subprocess.CompletedProcess[bytes]]
Confirm = Callable[[str], bool]


class PairingError(RuntimeError):
    """Pairing failed; the message says what to do next."""


@dataclass(frozen=True)
class SetupOptions:
    """The arguments of `hal remote setup`."""

    host: str
    profile: str = DEFAULT_PROFILE
    client: str = ""
    user: str = ""
    port: int = DEFAULT_SSH_PORT
    manual: bool = False
    from_pattern: str | None = None
    remote_hal: str = DEFAULT_REMOTE_HAL
    host_key_fingerprint: str = ""
    expires: str | None = None
    rotate: bool = False
    desktop_tools: bool = True


@dataclass(frozen=True)
class SetupResult:
    """What `setup` did, for the next-steps text."""

    profile: Profile
    gateway_written: bool
    gateway_lines: str
    mcp_server_lines: str


def split_target(host: str, user: str) -> tuple[str, str]:
    """Splits `user@host`; an explicit `user` wins, and a missing one is an error."""
    name, _, rest = host.rpartition("@")
    final_user = user or name
    if not final_user:
        raise PairingError("no user: give HOST as user@host or pass --user")
    if not rest or any(c.isspace() or c in "\"'\\" for c in rest + final_user):
        raise PairingError(f"invalid host or user: {host!r}")
    if rest.startswith("-") or final_user.startswith("-"):
        raise PairingError("host and user must not start with '-'")
    return final_user, rest


def render_profile(profile: Profile) -> str:
    """The `profile.toml` text of `profile`."""
    t = profile.tunnel
    lines = [
        f"schema = {SCHEMA_PROFILE}",
        f"profile = {json.dumps(profile.name)}",
        f"client = {json.dumps(profile.client)}",
        f"host = {json.dumps(profile.host)}",
        f"user = {json.dumps(profile.user)}",
        f"port = {profile.port}",
        f"created = {json.dumps(profile.created)}",
    ]
    for f in profile.forwards:
        lines += ["", f"[forward.{f.name}]", f"local = {f.local_port}", f"remote = {f.remote_port}"]
    lines += [
        "",
        "[tunnel]",
        f"server_alive_interval_s = {t.server_alive_interval_s}",
        f"server_alive_count_max = {t.server_alive_count_max}",
        f"connect_timeout_s = {t.connect_timeout_s}",
        f"backoff_initial_s = {t.backoff_initial_s}",
        f"backoff_max_s = {t.backoff_max_s}",
        f"stable_after_s = {t.stable_after_s}",
        f"health_interval_s = {t.health_interval_s}",
    ]
    return "\n".join(lines) + "\n"


def parse_profile(text: str) -> Profile:
    """Parses `profile.toml`; raises `PairingError` for a bad or foreign file."""
    try:
        data = tomllib.loads(text)
        if data.get("schema") != SCHEMA_PROFILE:
            raise PairingError(f"profile.toml schema must be {SCHEMA_PROFILE}")
        tunnel = TunnelSettings(**{k: int(v) for k, v in data.get("tunnel", {}).items()})
        forwards = tuple(
            Forward("gateway", int(v["local"]), int(v["remote"])) for _, v in data.get("forward", {}).items()
        )
        return Profile(
            name=check_client_name(str(data["profile"])),
            client=check_client_name(str(data["client"])),
            host=str(data["host"]),
            user=str(data["user"]),
            port=int(data["port"]),
            forwards=forwards,
            tunnel=tunnel,
            created=str(data.get("created", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PairingError(f"profile.toml is not valid: {exc}") from None


def save_profile(profile: Profile) -> ProfilePaths:
    """Writes `profile.toml` (0600) and `ssh_config` for `profile`; returns its paths."""
    pp = profile_paths(profile.name)
    files.write_atomic(pp.root / "profile.toml", render_profile(profile), mode=0o600)
    files.write_atomic(pp.ssh_config, sshconf.render(profile, pp), mode=0o600)
    return pp


def load_profile(name: str) -> Profile:
    """Reads the profile `name`; raises `PairingError` when it was never set up."""
    path = profile_paths(name).root / "profile.toml"
    try:
        return parse_profile(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PairingError(f"profile {name!r} is not set up: run `hal remote setup`") from None


def resolve_profile(name: str | None, default: str = "") -> str:
    """The profile to use: `name`, else `[remote].default_profile`, else the only one set up."""
    if name:
        return name
    if default:
        return default
    root = paths.config_dir() / "remote"
    found = sorted(p.name for p in root.iterdir() if (p / "profile.toml").is_file()) if root.is_dir() else []
    if len(found) == 1:
        return found[0]
    if not found:
        raise PairingError("no profile is set up: run `hal remote setup HOST`")
    raise PairingError(f"several profiles exist ({', '.join(found)}): name one")


def known_hosts_text(profile_name: str, host_keys: list[str]) -> str:
    """The pinned `known_hosts` lines under the alias `hal-<profile>`; invalid keys raise."""
    if not host_keys:
        raise PairingError("the desktop returned no host keys")
    lines = []
    for raw in host_keys:
        try:
            key = parse_public_key(raw)
        except ValueError as exc:
            raise PairingError(f"bad host key from the desktop: {exc}") from None
        lines.append(f"hal-{profile_name} {key.type} {key.blob}")
    return "\n".join(lines) + "\n"


def remote_command(remote_hal: str, *args: str) -> str:
    """The remote command line; a leading `~/` stays unquoted so the remote shell expands it."""
    if remote_hal.startswith("~/"):
        head = "~/" + shlex.quote(remote_hal[2:])
    else:
        head = shlex.quote(remote_hal)
    return " ".join([head, *(shlex.quote(a) for a in args)])


def make_keys(
    pp: ProfilePaths, client: str, ssh_keygen: Path, runner: Runner, rotate: bool
) -> tuple[PublicKey, PublicKey]:
    """Creates the tunnel and mcp keys; with `rotate` the old pairs are deleted first."""
    if rotate:
        for key in (pp.tunnel_key, pp.mcp_key):
            for f in (key, key.with_name(key.name + ".pub")):
                f.unlink(missing_ok=True)
    try:
        tunnel = generate(
            pp.tunnel_key, comment=f"hal-remote:{client}:tunnel", ssh_keygen=ssh_keygen, runner=runner
        )
        mcp = generate(pp.mcp_key, comment=f"hal-remote:{client}:mcp", ssh_keygen=ssh_keygen, runner=runner)
    except FileExistsError as exc:
        raise PairingError(str(exc)) from None
    return tunnel, mcp


def bootstrap_authorize(
    opts: SetupOptions, user: str, host: str, request: authorize.AuthorizeRequest, ssh: Path, runner: Runner
) -> dict:
    """Runs `authorize --stdin --json` on the desktop over the user's own SSH access."""
    body = {
        "schema": authorize.SCHEMA_REQUEST,
        "client": request.client,
        "tunnel_key": _line(request.tunnel_key),
        "mcp_key": _line(request.mcp_key),
        "from": request.from_pattern,
        "expires": request.expires.isoformat() if request.expires else None,
        "rotate": request.rotate,
    }
    argv = [
        str(ssh), "-T", "-o", "BatchMode=yes", "-p", str(opts.port), "-l", user, "--", host,
        remote_command(opts.remote_hal, "authorize", "--client", request.client, "--stdin", "--json"),
    ]  # fmt: skip
    try:
        done = runner(argv, timeout=SSH_TIMEOUT_S, input=json.dumps(body).encode("utf-8"), check=False)
    except (OSError, proc.ProcTimeout) as exc:
        raise PairingError(f"could not run ssh to {host}: {exc}") from None
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip()[-300:]
        raise PairingError(
            f"authorize on {host} failed (exit {done.returncode}): {tail or 'no output'}. "
            "Check that your own SSH key works, or use --manual."
        )
    try:
        resp = json.loads(done.stdout.decode("utf-8"))
    except ValueError as exc:
        raise PairingError(f"authorize on {host} did not return JSON: {exc}") from None
    if not isinstance(resp, dict) or resp.get("schema") != authorize.SCHEMA_RESPONSE:
        raise PairingError(f"authorize on {host} returned an unexpected reply")
    return resp


def scan_host_key(host: str, port: int, ssh: Path, runner: Runner) -> PublicKey:
    """Fetches the desktop's ed25519 host key with `ssh-keyscan` (manual flow)."""
    scan = sshconf.find_ssh_keyscan(ssh)
    try:
        done = runner(
            [str(scan), "-q", "-t", "ed25519", "-p", str(port), "--", host],
            timeout=KEYSCAN_TIMEOUT_S,
            check=False,
        )
    except (OSError, proc.ProcTimeout) as exc:
        raise PairingError(f"ssh-keyscan failed: {exc}") from None
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "ssh-ed25519":
            return parse_public_key(f"{parts[1]} {parts[2]}")
    raise PairingError(f"no ed25519 host key from {host}:{port}; is sshd running?")


def confirm_host_key(key: PublicKey, expected: str, confirm: Confirm) -> None:
    """Accepts `key` when it matches `expected`, else when the user confirms the fingerprint."""
    fingerprint = key.fingerprint()
    if expected:
        if not FINGERPRINT_RE.match(expected):
            raise PairingError("--host-key-fingerprint must look like SHA256:<43 characters>")
        if expected != fingerprint:
            raise PairingError(f"host key mismatch: the desktop offers {fingerprint}, expected {expected}")
        return
    if not confirm(f"The desktop's host key is {fingerprint}. Does it match the one printed on the desktop?"):
        raise PairingError("host key not confirmed; nothing was pinned")


def hello(profile: Profile, pp: ProfilePaths, ssh: Path, runner: Runner) -> dict[str, str]:
    """Calls `hello` over the mcp key and returns the delivered tokens (once only)."""
    argv = [*sshconf.mcp_argv(profile, pp, ssh), "hello"]
    try:
        done = runner(argv, timeout=SSH_TIMEOUT_S, check=False)
    except (OSError, proc.ProcTimeout) as exc:
        raise PairingError(f"hello failed: {exc}") from None
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip()[-300:]
        raise PairingError(f"hello failed (exit {done.returncode}): {tail or 'no output'}")
    try:
        data = json.loads(done.stdout.decode("utf-8"))
    except ValueError as exc:
        raise PairingError(f"hello did not return JSON: {exc}") from None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not tokens or not isinstance(tokens, dict) or not tokens.get("gateway"):
        raise PairingError(
            "no token was delivered: it is delivered once. Run `hal remote authorize --client "
            f"{profile.client} --rotate-token` on the desktop, then setup again with --rotate"
        )
    return {str(k): str(v) for k, v in tokens.items()}


def gateway_lines(token_file: Path, port: int = GATEWAY_LOCAL_PORT) -> str:
    """The `[gateway]` client-mode lines for the laptop's `config.toml`."""
    return f'[gateway]\nurl = "http://127.0.0.1:{port}"\ntoken_file = "{token_file.as_posix()}"\n'


def mcp_server_lines(profile: Profile, pp: ProfilePaths, ssh: Path) -> str:
    """The `[mcp.servers.hal-<profile>]` entry that exposes the desktop's tools (design 07)."""
    args = json.dumps(sshconf.mcp_argv(profile, pp, ssh)[1:])
    return f'[mcp.servers.hal-{profile.name}]\nhost = true\ncommand = "{ssh.as_posix()}"\nargs = {args}\n'


def append_if_absent(config_file: Path, table_re: re.Pattern[str], text: str) -> bool:
    """Appends `text` to `config_file` unless the table exists; True when it was appended."""
    old = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    if table_re.search(old):
        return False
    sep = "" if not old or old.endswith("\n\n") else ("\n" if old.endswith("\n") else "\n\n")
    files.write_atomic(config_file, old + sep + text, mode=0o600)
    return True


def setup(
    opts: SetupOptions,
    *,
    runner: Runner = proc.run,
    confirm: Confirm | None = None,
    wait_for_desktop: Callable[[str], None] | None = None,
    ssh_override: str = "",
) -> SetupResult:
    """Pairs this machine with the desktop and writes the profile; raises `PairingError`."""
    user, host = split_target(opts.host, opts.user)
    client = check_client_name(opts.client or authorize.hostname().split(".")[0].lower())
    check_client_name(opts.profile)
    try:
        ssh = sshconf.find_ssh(ssh_override)
        keygen = sshconf.find_ssh_keygen(ssh)
    except sshconf.SshNotFound as exc:
        raise PairingError(str(exc)) from None
    pp = profile_paths(opts.profile)
    if pp.root.joinpath("profile.toml").exists() and not opts.rotate:
        raise PairingError(f"profile {opts.profile!r} exists: use --rotate to pair again")
    tunnel, mcp = make_keys(pp, client, keygen, runner, opts.rotate)
    try:
        request = authorize.AuthorizeRequest.build(
            client=client,
            tunnel_key=_line(tunnel),
            mcp_key=_line(mcp),
            from_pattern=opts.from_pattern,
            expires=opts.expires,
            rotate=opts.rotate,
        )
    except authorize.AuthorizeError as exc:
        raise PairingError(str(exc)) from None
    forwards = (Forward("gateway", GATEWAY_LOCAL_PORT, authorize.GATEWAY_PORT),)
    profile = Profile(opts.profile, client, host, user, opts.port, forwards, created=timeutil.iso_z())
    if opts.manual:
        if wait_for_desktop is None or confirm is None:
            raise PairingError("the manual flow needs an interactive terminal")
        wait_for_desktop(manual_command(request, opts.from_pattern))
        key = scan_host_key(host, opts.port, ssh, runner)
        confirm_host_key(key, opts.host_key_fingerprint, confirm)
        host_keys = [_line(key)]
    else:
        resp = bootstrap_authorize(opts, user, host, request, ssh, runner)
        host_keys = list(resp.get("host_keys", []))
    files.write_atomic(pp.known_hosts, known_hosts_text(opts.profile, host_keys), mode=0o600)
    save_profile(profile)
    tokens = hello(profile, pp, ssh, runner)
    files.write_atomic(pp.token, tokens["gateway"] + "\n", mode=0o600)
    written = append_if_absent(paths.config_file(), GATEWAY_TABLE_RE, gateway_lines(pp.token))
    server = ""
    if opts.desktop_tools:
        server = mcp_server_lines(profile, pp, ssh)
        server_re = re.compile(rf"^\s*\[mcp\.servers\.hal-{re.escape(opts.profile)}\]\s*$", re.MULTILINE)
        if not append_if_absent(paths.config_file(), server_re, server):
            server = ""
    return SetupResult(profile, written, "" if written else gateway_lines(pp.token), server)


def _line(key: PublicKey) -> str:
    return f"{key.type} {key.blob} {key.comment}".strip()


def manual_command(request: authorize.AuthorizeRequest, from_pattern: str | None) -> str:
    """The command the user runs on the desktop in the manual flow."""
    parts = [
        "hal remote authorize",
        f"--client {request.client}",
        f"--tunnel-key {shlex.quote(_line(request.tunnel_key))}",
        f"--mcp-key {shlex.quote(_line(request.mcp_key))}",
    ]
    if from_pattern:
        parts.append(f"--from {shlex.quote(from_pattern)}")
    return " ".join(parts)
