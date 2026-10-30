"""No telemetry (RESOLUTIONS I9): no network client outside the broker and gateway paths, no analytics SDK.

Three scans over every `src/` file of every package, with `ast` so imports inside functions count:
1. a module that imports an HTTP or TLS client library must be listed in `NETWORK_MODULES` with the
   reason it talks to the network, so a new outbound path needs a reviewed line here;
2. no module may import a telemetry, analytics or crash-report SDK;
3. an `http(s)://` literal must name a local address or one of `ALLOWED_HOSTS`.
The lockfile may not hold an SDK or exporter either (the `opentelemetry-api` that `mcp` pulls in is a
no-op interface without an SDK, so it is the single exception).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

HTTP_LIBS = frozenset({"requests", "httpx", "urllib3", "aiohttp", "websockets", "websocket", "ssl", "ftplib"})
HTTP_SUBMODULES = frozenset({"urllib.request", "http.client", "xmlrpc.client"})

# path under packages/*/src -> why this module may open a network connection
NETWORK_MODULES: dict[str, str] = {
    "halagent/provider.py": "model API over the box's model socket (gateway forwarder)",
    "halcore/brain.py": "host brain talking to the configured model server",
    "halcore/power.py": "status probe of the user's own gateway on localhost",
    "halgateway/client.py": "client of the user's own gateway",
    "halgateway/gpu/peers.py": "probe of gpu-light peers the user configured",
    "halgateway/models/download.py": "model download the user asked for, from the pinned model host",
    "halgateway/relay.py": "gateway relay to the model server the user configured",
    "halhub/_worker/hal_worker.py": "pack worker; reaches the network only through halkit.netguard",
    "halkit/llm.py": "client of the configured model server",
    "halkit/netguard.py": "the single checked fetch used by packs and notify",
    "halremote/servercheck.py": "localhost health probe of the gateway",
    "halremote/supervisor.py": "localhost health probe of the tunnel",
    "halsandbox/forwarder.py": "forwards the box's model socket to the configured model server",
}  # fmt: skip

# host -> why Hal may name it. Local addresses are always fine.
ALLOWED_HOSTS: dict[str, str] = {
    "huggingface.co": "model download the user asked for (halgateway.models)",
    "ntfy.sh": "default of the optional, off-by-default [notify].ntfy_server key",
}
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "host", "...", "example.org", "example.com")  # noqa: S104 - names, not binds

TELEMETRY_SDKS = (
    "sentry_sdk", "posthog", "segment", "analytics", "mixpanel", "amplitude", "datadog", "ddtrace",
    "bugsnag", "rollbar", "statsig", "newrelic", "honeycomb", "logfire", "opentelemetry.sdk",
    "opentelemetry.exporter", "appinsights", "applicationinsights", "raven", "plausible",
)  # fmt: skip
LOCK_BANNED = re.compile(
    r'^name = "(sentry-sdk|posthog|segment-analytics-python|mixpanel|amplitude-analytics|datadog|ddtrace|'
    r'bugsnag|rollbar|statsig|newrelic|logfire|opentelemetry-sdk|opentelemetry-exporter[\w-]*)"',
    re.MULTILINE,
)  # fmt: skip
URL = re.compile(r"https?://([A-Za-z0-9._\[\]:-]+)")


def source_files() -> list[Path]:
    return sorted(REPO.glob("packages/*/src/**/*.py"))


def imported(path: Path) -> set[str]:
    """Dotted module names imported anywhere in `path` (absolute imports only)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def rel(path: Path) -> str:
    return path.relative_to(path.parents[len(path.relative_to(REPO).parts) - 4]).as_posix()


def test_only_listed_modules_import_a_network_client():
    found = {}
    for path in source_files():
        names = imported(path)
        hits = {n for n in names if n.split(".")[0] in HTTP_LIBS or n in HTTP_SUBMODULES}
        if hits:
            found[rel(path)] = sorted(hits)
    unlisted = {p: h for p, h in found.items() if p not in NETWORK_MODULES}
    assert not unlisted, f"network client imported outside the allowed paths: {unlisted}"
    stale = sorted(set(NETWORK_MODULES) - set(found))
    assert not stale, f"NETWORK_MODULES lists modules that no longer import a client: {stale}"


def test_no_module_imports_a_telemetry_or_analytics_sdk():
    bad = {}
    for path in source_files():
        hits = [n for n in imported(path) if any(n == s or n.startswith(s + ".") for s in TELEMETRY_SDKS)]
        if hits:
            bad[rel(path)] = hits
    assert not bad, f"telemetry client imported: {bad}"


def test_urls_in_source_name_only_local_or_allowed_hosts():
    bad = {}
    for path in source_files():
        for host in URL.findall(path.read_text()):
            name = host.split(":")[0] if not host.startswith("[") else host.split("]")[0] + "]"
            if name in LOCAL_HOSTS or name.endswith((".invalid", ".local")) or name in ALLOWED_HOSTS:
                continue
            if re.fullmatch(r"(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.[\d.]+|100\.[\d.]+", name):
                continue
            bad.setdefault(rel(path), []).append(name)
    assert not bad, f"URL with an unlisted host: {bad}"


def test_lockfile_holds_no_telemetry_sdk_or_exporter():
    lock = (REPO / "uv.lock").read_text()
    assert LOCK_BANNED.findall(lock) == []
