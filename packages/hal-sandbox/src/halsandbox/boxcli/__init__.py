"""Per-CLI box profiles; `PROFILES` maps a CLI name to its writer."""

from __future__ import annotations

from ..errors import SandboxError
from .base import BoxContext, BoxLaunch, CliProfile
from .claude import Claude
from .codex import Codex
from .crush import Crush
from .goose import Goose
from .opencode import Opencode
from .qwen import Qwen

PROFILES: dict[str, CliProfile] = {
    p.name: p for p in (Claude(), Qwen(), Opencode(), Codex(), Crush(), Goose())
}


def profile(name: str) -> CliProfile:
    """The profile for `name`; raises `SandboxError` for an unsupported CLI."""
    try:
        return PROFILES[name]
    except KeyError:
        raise SandboxError(f"unsupported agent CLI {name!r}; choose one of {', '.join(PROFILES)}") from None


__all__ = ["PROFILES", "BoxContext", "BoxLaunch", "CliProfile", "profile"]
