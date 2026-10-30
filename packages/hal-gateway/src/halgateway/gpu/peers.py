"""Peer GPU guard: refuse to load while another llama-server holds the GPU (REQ-GW-56).

Ported from the `HAL_PEER_LLAMA_URL` check of `b279740`, which grepped the body; this one
parses the JSON. An unreachable or unparsable peer counts as not holding the GPU, with a
warning, so a dead peer never blocks Hal.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..errors import Conflict

log = logging.getLogger(__name__)

LOADED = "loaded"
MAX_BODY_BYTES = 1_000_000  # a model list is a few KiB; refuse a runaway answer


class PeerBusy(Conflict):
    """A peer server holds the GPU."""

    code = "gpu_busy_peer"


class PeerConfigError(ValueError):
    """`[[gateway.gpu.peers]]` is malformed."""


@dataclass(frozen=True)
class Peer:
    """One peer GPU server."""

    url: str
    api_key_file: str = ""


def parse_peers(raw: Sequence[object]) -> list[Peer]:
    """Validates the `peers` list of the gpu table; raises `PeerConfigError`."""
    peers = []
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping) or not isinstance(item.get("url"), str):
            raise PeerConfigError(f"gateway.gpu.peers[{i}] needs a string `url`")
        url = str(item["url"]).rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise PeerConfigError(f"gateway.gpu.peers[{i}].url must be http:// or https://")
        unknown = set(item) - {"url", "api_key_file"}
        if unknown:
            raise PeerConfigError(f"gateway.gpu.peers[{i}] has unknown keys {sorted(unknown)}")
        peers.append(Peer(url, str(item.get("api_key_file") or "")))
    return peers


def fetch_models(peer: Peer, timeout_s: float) -> object:
    """`GET <url>/models` of `peer` as parsed JSON; raises `OSError` or `ValueError`."""
    headers = {"Accept": "application/json"}
    if peer.api_key_file:
        key = Path(peer.api_key_file).expanduser().read_text(encoding="utf-8").strip()
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{peer.url}/models", headers=headers)  # noqa: S310 - scheme checked in parse_peers
    with urllib.request.urlopen(request, timeout=timeout_s) as resp:  # noqa: S310 - same
        return json.loads(resp.read(MAX_BODY_BYTES))


def holds_gpu(doc: object) -> list[str]:
    """Ids of the models a `/models` document reports with `status.value == "loaded"`."""
    data = doc.get("data", []) if isinstance(doc, dict) else []
    out = []
    for entry in data if isinstance(data, list) else []:
        status = entry.get("status") if isinstance(entry, dict) else None
        if isinstance(status, dict) and status.get("value") == LOADED:
            out.append(str(entry.get("id", "?")))
    return out


class PeerGuard:
    """Checks every configured peer; `check` raises `PeerBusy` naming the first busy one."""

    def __init__(
        self,
        peers: Sequence[Peer],
        timeout_s: float,
        fetch: Callable[[Peer, float], object] = fetch_models,
    ) -> None:
        self.peers, self.timeout_s, self._fetch = list(peers), timeout_s, fetch

    def check(self) -> None:
        """Raises `PeerBusy` when a peer reports a loaded model."""
        for peer in self.peers:
            try:
                doc = self._fetch(peer, self.timeout_s)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                log.warning("peer %s is unreachable, treated as not holding the GPU: %s", peer.url, exc)
                continue
            loaded = holds_gpu(doc)
            if loaded:
                raise PeerBusy(f"peer {peer.url} holds the GPU with {', '.join(loaded)}; unload it first")

    def status(self) -> list[str]:
        """The peer URLs, for the status document."""
        return [p.url for p in self.peers]
