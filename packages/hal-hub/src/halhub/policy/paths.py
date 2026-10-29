"""Path rules of the policy: resolution before `decide`, the pure check, and the pack helpers.

REQ-SEC-09: every `format = "path"` argument is resolved (`~/` expanded, relative paths
refused, `realpath`) by `prepare` before `decide`, and `check_path` denies, in this order:

1. `path:workspace`: under an active session workspace, judged on the resolved path and on
   the path as written, so a link planted in a workspace is refused as a workspace path
   (ATK-13) rather than followed to its target;
2. `path:hard-deny`: under a built-in hard-deny path, or a file name matching a built-in
   hard-deny pattern (`.env`, `*.pem`, ...); nothing in `policy.toml` removes these;
3. `path:denied`: under a `[paths].deny` entry, or matching a `[paths].deny` glob (`fnmatch`
   on the resolved path);
4. `path:hal-dir`: under `state_dir()`, `config_dir()`, `data_dir()` or `runtime_dir()`,
   wherever `HAL_STATE_DIR` and friends put them;
5. `path:outside-roots`: not under the tool's `read_roots` or `write_roots` for its `access`.

`check_path` is pure: it reads the user's home from `Context.home` and the facts that need
the file system (the `realpath` of roots and deny entries that are symlinks, Hal's own
directories) from `Policy`, where `load_policy` recorded them.

REQ-SEC-100: packs apply the same rule to paths they derive themselves through
`check_host_path` (one path) and `walk_policy` (a `halkit.fsafe.walk`), both built from the
same `resolve` and the same check, so their answers cannot drift from `decide`'s.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from halkit.fsafe import WalkPolicy, is_beneath

from .model import Access, Context, Policy, ResolvedPath, ToolCall, ToolMeta, ToolPolicy
from .profiles import HARD_DENY_GLOBS, HARD_DENY_PATHS, expand

MAX_PATH_BYTES = 4096  # PATH_MAX on Linux; longer arguments are refused before realpath
_GLOB_CHARS = frozenset("*?[")


def is_glob(entry: str) -> bool:
    """True when a `[paths].deny` entry is a glob rather than a path."""
    return any(c in _GLOB_CHARS for c in entry)


def resolve(value: str, *, home: Path) -> ResolvedPath:
    """`value` with `~/` expanded and symlinks resolved; the error names why it failed."""
    if "\x00" in value:
        return ResolvedPath(value, None, "nul")
    text = str(home) + value[1:] if value == "~" or value.startswith("~/") else value
    if len(os.fsencode(text)) > MAX_PATH_BYTES:
        return ResolvedPath(value, None, "too_long")
    if not os.path.isabs(text):
        return ResolvedPath(value, None, "relative")
    try:
        return ResolvedPath(value, Path(os.path.realpath(text)), None)
    except (OSError, ValueError):
        return ResolvedPath(value, None, "unresolvable")


def prepare(call: ToolCall, meta: ToolMeta, *, home: Path) -> Mapping[str, ResolvedPath]:
    """Resolves every `format = "path"` string argument of `call` (the one impure step before `decide`)."""
    return {
        p.name: resolve(value, home=home)
        for p in meta.params
        if p.format == "path" and isinstance(value := call.args.get(p.name), str)
    }


def _lexical(rp: ResolvedPath, home: Path) -> Path | None:
    text = rp.original
    if text == "~" or text.startswith("~/"):
        text = str(home) + text[1:]
    return Path(os.path.normpath(text)) if os.path.isabs(text) else None


def _forms(entry: str, policy: Policy, home: Path) -> tuple[Path, ...]:
    """A configured path as written (expanded) and, when it is a symlink, its resolved form."""
    lexical = expand(entry, home=home, environ={})
    if lexical is None:
        return ()
    real = policy.real_paths.get(lexical)
    return (lexical, real) if real is not None and real != lexical else (lexical,)


def _under_any(path: Path, bases: tuple[Path, ...]) -> bool:
    return any(is_beneath(base, path) for base in bases)


def _hard_deny_bases(policy: Policy, home: Path) -> tuple[Path, ...]:
    bases: list[Path] = []
    for entry in HARD_DENY_PATHS:
        if entry == "$XDG_RUNTIME_DIR":
            continue  # recorded in Policy.hal_dirs by the loader: it needs the environment
        bases.extend(_forms(entry, policy, home))
    return tuple(bases)


def _check(
    rp: ResolvedPath,
    access: Access,
    read_roots: tuple[str, ...],
    write_roots: tuple[str, ...],
    policy: Policy,
    workspaces: tuple[Path, ...],
    home: Path,
) -> str | None:
    if rp.resolved is None:
        return f"path:{rp.error or 'unresolved'}"
    resolved = rp.resolved
    lexical = _lexical(rp, home)
    candidates = (resolved,) if lexical is None else (resolved, lexical)
    if any(_under_any(c, workspaces) for c in candidates):
        return "path:workspace"
    if any(_under_any(c, _hard_deny_bases(policy, home)) for c in candidates):
        return "path:hard-deny"
    if any(fnmatch.fnmatchcase(c.name, pattern) for c in candidates for pattern in HARD_DENY_GLOBS):
        return "path:hard-deny"
    for entry in policy.paths.deny:
        if is_glob(entry):
            pattern = str(expand(entry, home=home, environ={}) or entry)
            if any(fnmatch.fnmatchcase(str(c), pattern) for c in candidates):
                return "path:denied"
        elif any(_under_any(c, _forms(entry, policy, home)) for c in candidates):
            return "path:denied"
    if any(_under_any(c, policy.hal_dirs) for c in candidates):
        return "path:hal-dir"
    roots = read_roots if access == "read" else write_roots
    allowed = tuple(form for root in roots for form in _forms(root, policy, home))
    if not _under_any(resolved, allowed):
        return "path:outside-roots"
    return None


def check_path(rp: ResolvedPath, access: Access, tp: ToolPolicy, policy: Policy, ctx: Context) -> str | None:
    """The deny rule for one resolved path argument of a call, or None when it is allowed (pure)."""
    return _check(rp, access, tp.read_roots, tp.write_roots, policy, ctx.workspaces, ctx.home)


def tool_roots(policy: Policy, tool: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(read_roots, write_roots)` for `tool` by name, with `Policy.tool`'s precedence for roots."""
    read_roots, write_roots = policy.paths.read_roots, policy.paths.write_roots
    for rule in (policy.tools.get(tool), policy.profile_tools.get(tool)):
        if rule is not None and rule.read_roots is not None:
            read_roots = rule.read_roots
        if rule is not None and rule.write_roots is not None:
            write_roots = rule.write_roots
    return read_roots, write_roots


def check_host_path(
    path: str,
    *,
    access: Literal["read", "write"],
    tool: str,
    policy: Policy,
    workspaces: tuple[Path, ...],
    home: Path,
) -> tuple[str | None, Path | None]:
    """`(deny rule or None, resolved path when allowed)` for a path a pack derived itself."""
    rp = resolve(path, home=home)
    read_roots, write_roots = tool_roots(policy, tool)
    rule = _check(rp, access, read_roots, write_roots, policy, workspaces, home)
    return (rule, None) if rule else (None, rp.resolved)


def walk_policy(tool: str, *, policy: Policy, workspaces: tuple[Path, ...], home: Path) -> WalkPolicy:
    """The `halkit.fsafe.WalkPolicy` that matches `check_host_path` for reads by `tool`."""
    read_roots, _ = tool_roots(policy, tool)
    allow = tuple(form for root in read_roots for form in _forms(root, policy, home))
    deny = list(_hard_deny_bases(policy, home))
    globs = list(HARD_DENY_GLOBS)
    for entry in policy.paths.deny:
        if is_glob(entry):
            globs.append(str(expand(entry, home=home, environ={}) or entry))
        else:
            deny.extend(_forms(entry, policy, home))
    deny.extend(policy.hal_dirs)
    deny.extend(workspaces)
    return WalkPolicy(
        allow_roots=tuple(dict.fromkeys(allow)),
        deny=tuple(dict.fromkeys(deny)),
        deny_globs=tuple(dict.fromkeys(globs)),
    )
