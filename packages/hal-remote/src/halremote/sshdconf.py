"""Read-only reader for `sshd_config` that follows `Include` (REQ-RMT-16).

It never runs `sshd` and never writes. It answers "what would sshd read first" for the
server checks, and says "unknown" (`effective` returns `None`) whenever the file set cannot
settle the answer: an unreadable file, or a keyword that a `Match` block also sets. Rules:
first value wins; `AllowUsers` and friends accumulate; keywords are case-insensitive;
arguments may be double-quoted; `Include` globs expand in lexical order, relative to
`/etc/ssh`.
"""

from __future__ import annotations

import glob
import logging
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_ROOT = Path("/etc/ssh")
MAIN_FILE = "sshd_config"
MAX_INCLUDE_DEPTH = 16  # the limit OpenSSH applies to nested Include
ACCUMULATING = frozenset({"allowusers", "denyusers", "allowgroups", "denygroups"})


@dataclass(frozen=True)
class SshdConfig:
    """What the static reader found."""

    values: Mapping[str, tuple[str, ...]]  # lower-case keyword -> first value's args
    allow_users: tuple[str, ...]
    conditional: frozenset[str]  # keywords set inside Match blocks
    files: tuple[Path, ...]
    unreadable: tuple[Path, ...]


class _State:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.values: dict[str, tuple[str, ...]] = {}
        self.conditional: set[str] = set()
        self.files: list[Path] = []
        self.unreadable: list[Path] = []


def _split(line: str) -> list[str]:
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split = True
    lex.quotes = '"'  # sshd honours double quotes only
    lex.commenters = ""
    return list(lex)


def _resolve(pattern: str, root: Path) -> list[Path]:
    path = Path(pattern)
    if path.is_absolute():
        # fixture trees and chroot-style roots: /etc/ssh/... means "under root"
        try:
            path = root / path.relative_to(DEFAULT_ROOT)
        except ValueError:
            pass
    else:
        path = root / path
    return [Path(p) for p in sorted(glob.glob(str(path)))]


def _read_file(path: Path, state: _State, in_match: bool, depth: int) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        state.unreadable.append(path)
        return
    state.files.append(path)
    match_here = in_match  # Match scope ends with its file (OpenSSH servconf.c; unverified here)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line.split(None, 1)[0]:
            line = line.replace("=", " ", 1)
        try:
            words = _split(line)
        except ValueError:
            log.warning("%s: unbalanced quote, line ignored: %r", path, raw)
            continue
        if not words:
            continue
        keyword, args = words[0].lower(), tuple(words[1:])
        if keyword == "match":
            match_here = args != ("all",)  # `Match all` ends the conditional block
            continue
        if keyword == "include":
            if depth >= MAX_INCLUDE_DEPTH:
                log.warning("%s: Include nested deeper than %d, ignored", path, MAX_INCLUDE_DEPTH)
                continue
            for pattern in args:
                for found in _resolve(pattern, state.root):
                    _read_file(found, state, match_here, depth + 1)
            continue
        if match_here:
            state.conditional.add(keyword)
        elif keyword in ACCUMULATING:
            state.values[keyword] = state.values.get(keyword, ()) + args
        else:
            state.values.setdefault(keyword, args)


def read(root: Path = DEFAULT_ROOT) -> SshdConfig:
    """Reads `root/sshd_config` and everything it includes."""
    state = _State(Path(root))
    _read_file(state.root / MAIN_FILE, state, False, 0)
    return SshdConfig(
        values=dict(state.values),
        allow_users=state.values.get("allowusers", ()),
        conditional=frozenset(state.conditional),
        files=tuple(state.files),
        unreadable=tuple(state.unreadable),
    )


def effective(cfg: SshdConfig, key: str, default: str) -> str | None:
    """The value sshd would use for `key` (lower-cased), `default` if unset, `None` if unknown."""
    key = key.lower()
    if cfg.unreadable or key in cfg.conditional:
        return None
    args = cfg.values.get(key)
    return " ".join(args) if args is not None else default
