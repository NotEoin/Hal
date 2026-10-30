"""Egress URL checks and a guarded HTTP fetch for host-side tools.

01-security owns the rules (REQ-SEC-10, REQ-SEC-11, REQ-SEC-92); `halhub.policy` uses
`check_url` inside its pure decision, and packs that reach the internet use `fetch`.

- `check_url` is pure: no DNS, no socket. It refuses schemes other than `https` (and `http`
  with `allow_http`), URLs with userinfo, IP literals in every form `inet_aton` accepts
  (dotted, decimal `2130706433`, octal, hex, short `127.1`) and IPv6 including IPv4-mapped
  forms unless `allow_ip_literals`, and local or private names unless `allow_private`. It also
  says whether the host is on the `domains` list (`*.example.com` matches subdomains, not the
  apex). No DNS lookup of a model-chosen host may happen before the policy says `allow`, which
  is why this function never resolves.
- `canonical_url` is the comparison form for `url:from-search`: scheme and host lowercased,
  host in IDNA ASCII form, default port and fragment removed, path and query byte for byte.
- `fetch` resolves the host once, refuses the hop when any address is not global (unless
  `allow_private`), connects to the checked address with SNI and `Host` set to the name (so a
  second DNS answer cannot redirect the connection: DNS rebinding), re-checks every redirect
  target and stops after `max_redirects`. With a non-empty `domains`, every hop must be on
  it. Caller headers named `Authorization` or `Cookie` are dropped when a redirect changes
  the host.

Hostnames are converted with Python's `idna` codec (IDNA 2003). It is the stdlib's only IDNA
implementation; the `idna` package (IDNA 2008) is not on the dependency allowlist.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import ipaddress
import logging
import re
import socket
import ssl
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

log = logging.getLogger(__name__)

DEFAULT_PORTS = {"http": 80, "https": 443}
PRIVATE_NAMES = ("localhost",)
PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")  # REQ-SEC-10
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
CROSS_HOST_DROPPED_HEADERS = frozenset({"authorization", "cookie"})
# The forms `inet_aton` reads as an IPv4 address: 1 to 4 parts, each decimal, octal or hex.
_NUMERIC_HOST_RE = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+)){0,3}$")


class BlockedURL(ValueError):
    """A URL or redirect target refused by the egress rules; `rule` is the policy rule id."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


class FetchError(OSError):
    """The server sent something that is not valid HTTP, or DNS resolution failed."""


@dataclass(frozen=True)
class Response:
    """The answer of `fetch` after redirects."""

    url: str  # final URL after redirects
    status: int
    headers: Mapping[str, str]  # lowercased names
    body: bytes
    truncated: bool  # body cut at max_bytes


@dataclass(frozen=True)
class _Host:
    name: str  # lowercase IDNA ASCII, no trailing dot; an IP address in compressed form
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None


def _host(raw: str) -> _Host | None:
    """The normalised host of a URL, or None when it cannot be one."""
    text = raw.strip().rstrip(".").lower()
    if not text:
        return None
    try:
        return _Host(str(ipaddress.ip_address(text)), ipaddress.ip_address(text))
    except ValueError:
        pass
    if _NUMERIC_HOST_RE.match(text):
        try:
            ip = ipaddress.IPv4Address(socket.inet_aton(text))
        except OSError:
            return None
        return _Host(str(ip), ip)
    try:
        name = text.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return _Host(name, None) if re.fullmatch(r"[a-z0-9_.-]+", name) else None


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not ip.is_global


def _listed(name: str, domains: Collection[str]) -> bool:
    for entry in domains:
        entry = entry.strip().rstrip(".").lower()
        if entry.startswith("*."):
            if name.endswith(entry[1:]):
                return True
        elif name == entry:
            return True
    return False


def check_url(
    url: str,
    *,
    domains: Collection[str],
    allow_http: bool,
    allow_ip_literals: bool,
    allow_private: bool,
) -> tuple[str | None, bool]:
    """`(deny rule, host listed)` for `url` under REQ-SEC-10; the rule is None when allowed.

    Rules: `url:scheme`, `url:userinfo`, `url:host` (missing, malformed or bad port),
    `url:ip-literal`, `url:private`. `listed` is true when the host is on `domains`.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "url:host", False
    schemes = ("https", "http") if allow_http else ("https",)
    if parts.scheme.lower() not in schemes:
        return "url:scheme", False
    if "@" in parts.netloc:
        return "url:userinfo", False
    host = _host(parts.hostname or "")
    if host is None or (port is not None and not 0 < port < 65536):
        return "url:host", False
    if host.ip is not None:
        if not allow_ip_literals:
            return "url:ip-literal", False
        if _is_private_ip(host.ip) and not allow_private:
            return "url:private", False
        return None, _listed(host.name, domains)
    private = host.name in PRIVATE_NAMES or host.name.endswith(PRIVATE_SUFFIXES) or "." not in host.name
    if private and not allow_private:
        return "url:private", False
    return None, _listed(host.name, domains)


def canonical_url(url: str) -> str:
    """The comparison form of `url` for provenance checks (01-security `url:from-search`).

    Raises `ValueError` for a URL without a usable host.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = _host(parts.hostname or "")
    if host is None:
        raise ValueError(f"{url!r} has no usable host")
    name = f"[{host.name}]" if isinstance(host.ip, ipaddress.IPv6Address) else host.name
    port = parts.port
    if port is not None and port != DEFAULT_PORTS.get(scheme):
        name = f"{name}:{port}"
    netloc = parts.netloc.rpartition("@")[0] + "@" + name if "@" in parts.netloc else name
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def _system_resolver(timeout: float) -> Callable[[str], list[str]]:
    def resolve(host: str) -> list[str]:
        # getaddrinfo has no timeout of its own; a worker thread gives the lookup one
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
            infos = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as err:
            raise FetchError(f"DNS lookup of {host} timed out after {timeout} s") from err
        except socket.gaierror as err:
            raise FetchError(f"DNS lookup of {host} failed: {err}") from err
        finally:
            pool.shutdown(wait=False)
        return list(dict.fromkeys(str(info[4][0]) for info in infos))

    return resolve


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection whose `Host` is the name but whose socket goes to a checked address."""

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """The TLS form of `_PinnedHTTPConnection`: SNI and certificate check use the name."""

    def __init__(self, host: str, port: int, address: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._ctx = context

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(raw, server_hostname=self.host)


def fetch(
    url: str,
    *,
    domains: Collection[str],
    timeout: float,
    max_bytes: int,
    max_redirects: int = 3,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    resolver: Callable[[str], list[str]] | None = None,
    allow_http: bool = False,
    allow_ip_literals: bool = False,
    allow_private: bool = False,
    context: ssl.SSLContext | None = None,
) -> Response:
    """Fetches `url` under the egress rules and returns the final response.

    Raises `BlockedURL` when `check_url` refuses the URL or a redirect target, when a
    non-empty `domains` does not list a hop's host (`url:unlisted`), when an address is not
    global (`url:private`) or after `max_redirects` (`url:redirects`); `FetchError` for DNS
    and protocol failures; `OSError` (including `TimeoutError`) for connection failures.
    `context` replaces the default TLS context (tests pass one that trusts a local CA).
    """
    resolve = resolver or _system_resolver(timeout)
    ctx = context or ssl.create_default_context()
    send_headers = dict(headers or {})
    previous_host: str | None = None
    for hop in range(max_redirects + 1):
        rule, listed = check_url(
            url,
            domains=domains,
            allow_http=allow_http,
            allow_ip_literals=allow_ip_literals,
            allow_private=allow_private,
        )
        if rule is not None:
            raise BlockedURL(rule, f"{url}: refused by egress rule {rule}")
        if domains and not listed:
            raise BlockedURL("url:unlisted", f"{url}: host is not on the allowed domains")
        parts = urlsplit(url)
        host = _host(parts.hostname or "")
        if host is None:  # check_url refuses these; kept so a change there cannot slip through
            raise BlockedURL("url:host", f"{url}: has no usable host")
        if previous_host is not None and host.name != previous_host:
            send_headers = {
                k: v for k, v in send_headers.items() if k.lower() not in CROSS_HOST_DROPPED_HEADERS
            }
        address = _checked_address(host, resolve, allow_private)
        response = _request(parts, host.name, address, method, send_headers, body, timeout, max_bytes, ctx)
        location = response.headers.get("location")
        if response.status not in REDIRECT_STATUSES or not location:
            return response
        if hop == max_redirects:
            break
        log.debug("redirect %s -> %s", url, location)
        if response.status == 303 or (response.status in (301, 302) and method == "POST"):
            method, body = "GET", None
        previous_host = host.name
        url = urljoin(url, location)
    raise BlockedURL("url:redirects", f"{url}: more than {max_redirects} redirects")


def _checked_address(host: _Host, resolve: Callable[[str], list[str]], allow_private: bool) -> str:
    if host.ip is not None:
        addresses = [host.name]
    else:
        addresses = resolve(host.name)
        if not addresses:
            raise FetchError(f"DNS lookup of {host.name} returned no address")
    for text in addresses:
        ip = ipaddress.ip_address(text)
        if _is_private_ip(ip) and not allow_private:
            raise BlockedURL("url:private", f"{host.name} resolves to non-global address {ip}")
    return addresses[0]


def resolve_checked(
    name: str, *, resolve: Callable[[str], list[str]] | None = None, timeout: float = 5.0
) -> str:
    """The address to connect to for host `name`; every address it resolves to must be global.

    Raises `BlockedURL` (rule `url:private`) when any address is private, loopback or
    link-local, and `FetchError` when the lookup fails or `name` is not a host name. The caller
    connects to the returned address, so DNS cannot change between the check and the connect.
    """
    host = _host(name)
    if host is None:
        raise FetchError(f"{name!r} is not a host name")
    return _checked_address(host, resolve or _system_resolver(timeout), allow_private=False)


def _request(
    parts: SplitResult,
    name: str,
    address: str,
    method: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
    max_bytes: int,
    ctx: ssl.SSLContext,
) -> Response:
    scheme = parts.scheme.lower()
    port = parts.port or DEFAULT_PORTS[scheme]
    if scheme == "https":
        conn: http.client.HTTPConnection = _PinnedHTTPSConnection(name, port, address, timeout, ctx)
    else:
        conn = _PinnedHTTPConnection(name, port, address, timeout)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        conn.request(method, target, body=body, headers=dict(headers))
        resp = conn.getresponse()
        data = resp.read(max_bytes + 1)
        return Response(
            url=urlunsplit(parts._replace(fragment="")),
            status=resp.status,
            headers={k.lower(): v for k, v in resp.getheaders()},
            body=data[:max_bytes],
            truncated=len(data) > max_bytes,
        )
    except http.client.HTTPException as err:
        raise FetchError(f"{name}: invalid HTTP response: {err}") from err
    finally:
        conn.close()
