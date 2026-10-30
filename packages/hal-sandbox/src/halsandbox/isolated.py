"""Isolated coding sessions: the box works on a snapshot copy, the real project is never mounted.

RESOLUTIONS I3 makes this the default for `hal code`. Changes reach the project only through
`accept` (diff report, risk flags, checkpoint, atomic `git apply`) or `export` (patch or bundle).

Design decisions:
- The copy is plain files with no `.git`. The spec's `git worktree` (REQ-BOX-53) leaves a gitfile
  that points into the real repository, so the box would see host paths; a plain copy plus a
  private shadow repository under `$STATE/isolated/<sid>/git` avoids that and works the same for
  git and non-git projects.
- Every git call runs with hooks, fsmonitor and system/global config off and a minimal
  environment, because the work tree is written by an untrusted agent and its config is hostile.
- Any `.git` the agent created inside the copy is deleted before diffing, so a nested repository
  cannot turn into a gitlink entry.
- Symlinks are not copied in and are refused in the diff: a link is how a box would point the
  host at `~/.ssh`.
- Masked secret files (`.env*`, keys) are left out of the copy, so the box never sees them.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from halkit import fsafe, paths, proc, timeutil

from . import scratch
from .errors import SandboxError

log = logging.getLogger(__name__)

GIT_TIMEOUT_S = 120.0
SID_RANDOM_BYTES = 3
MASK_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials*",
)
MASK_DIRS = (".ssh", ".aws", ".gnupg")
SKIP_DIRS = (".git", "node_modules", ".venv", "__pycache__")  # rebuilt inside the box, never worth copying
# paths whose change can run code on the host later, so accept refuses them without allow_flags
HIGH_RISK_PATTERNS = (
    ".git/*", ".gitmodules", ".gitattributes", ".github/*", ".gitlab-ci.yml", ".husky/*",
    ".envrc", ".pre-commit-config.yaml", ".vscode/tasks.json", ".claude/*", ".mcp.json", ".env", ".env.*",
)  # fmt: skip
PRIVATE_KEY_RE = re.compile(rb"^\+.*-----BEGIN [A-Z ]*PRIVATE KEY-----", re.M)
_GIT_BASE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.quotepath=off")
_IDENT = ("-c", "user.name=hal", "-c", "user.email=hal@localhost")


class AcceptRefused(SandboxError):
    """Accept stopped before changing the project."""


@dataclass(frozen=True)
class Change:
    """One changed path in a diff report."""

    status: str  # A, M, D or T (type change)
    path: str
    flag: str = ""  # empty, or the reason the change is high risk


@dataclass(frozen=True)
class Isolated:
    """One isolated session: its id, the project and the directories it uses."""

    sid: str
    project: Path
    root: Path

    @property
    def work(self) -> Path:
        """The copy that is mounted at `/work`."""
        return self.root / "work"

    @property
    def gitdir(self) -> Path:
        """The shadow repository holding the base snapshot."""
        return self.root / "git"


def isolated_root() -> Path:
    """Where isolated sessions live."""
    return paths.state_dir() / "isolated"


def _base_env() -> dict[str, str]:
    """A minimal environment for git: no system or global config, no prompts."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def _git(
    gitdir: Path,
    work: Path | None,
    *args: str,
    input: bytes | None = None,
    cwd: Path | None = None,
    index: Path | None = None,
) -> bytes:
    env = {**_base_env(), "GIT_DIR": str(gitdir)}
    if work is not None:
        env["GIT_WORK_TREE"] = str(work)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        done = proc.run(
            ["git", *_GIT_BASE, *args], timeout=GIT_TIMEOUT_S, env=env, input=input, cwd=cwd or work
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as SandboxError with the git command named
        raise SandboxError(f"git {args[0]} failed: {exc}") from exc
    return done.stdout


def _masked(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in MASK_DIRS for p in parts[:-1]) or any(
        fnmatch.fnmatch(parts[-1], pat) for pat in MASK_PATTERNS
    )


def _project_files(project: Path) -> Iterator[str]:
    """Relative file paths to copy: git's non-ignored files, or a plain walk outside a repository."""
    if (project / ".git").exists():
        out = proc.run(
            ["git", *_GIT_BASE, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            timeout=GIT_TIMEOUT_S,
            cwd=project,
            env=_base_env(),
        ).stdout
        yield from (p for p in out.decode().split("\0") if p)
        return
    for dirpath, dirnames, filenames in os.walk(project, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(project)
        for name in filenames:
            yield (rel_dir / name).as_posix()


def _copy_snapshot(project: Path, work: Path, max_bytes: int) -> int:
    total = 0
    for rel in _project_files(project):
        if _masked(rel) or rel.split("/")[0] in SKIP_DIRS:
            continue
        src = project / rel
        try:
            st = os.lstat(src)
        except OSError:
            continue  # listed by git but deleted from disk
        if not stat.S_ISREG(st.st_mode):
            continue  # symlinks, sockets and submodule directories stay out of the copy
        total += st.st_size
        if total > max_bytes:
            raise SandboxError(
                f"{project} is over the {max_bytes} byte copy cap; raise [sandbox].max_copy_bytes"
            )
        dst = work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd = fsafe.open_beneath(project, rel)
        with os.fdopen(fd, "rb") as fh, open(dst, "wb") as out:
            shutil.copyfileobj(fh, out)
        dst.chmod(st.st_mode & 0o777)
    return total


def create(project: Path, max_copy_bytes: int) -> Isolated:
    """Copies `project` into a new isolated session and records its base snapshot."""
    project = project.resolve()
    if not project.is_dir():
        raise SandboxError(f"not a directory: {project}")
    sid = f"{timeutil.iso_z().replace('-', '').replace(':', '')}-{secrets.token_hex(SID_RANDOM_BYTES)}"
    root = paths.ensure_private_dir(isolated_root()) / sid
    root.mkdir(mode=0o700)
    iso = Isolated(sid, project, root)
    try:
        iso.work.mkdir()
        _copy_snapshot(project, iso.work, max_copy_bytes)
        _git(iso.gitdir, None, "init", "-q", "--bare", cwd=root)
        _commit_all(iso, "base", ref="refs/heads/base")
        (root / "session.json").write_text(
            json.dumps({"sid": sid, "project": str(project), "created": timeutil.iso_z()})
        )
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return iso


def load(sid: str) -> Isolated:
    """The session `sid`; raises `SandboxError` when it is unknown."""
    if not re.fullmatch(r"[0-9TZ]+-[0-9a-f]+", sid):
        raise SandboxError(f"bad session id {sid!r}")
    meta = isolated_root() / sid / "session.json"
    if not meta.is_file():
        raise SandboxError(f"no isolated session {sid}")
    return Isolated(sid, Path(json.loads(meta.read_text())["project"]), meta.parent)


def sessions() -> list[str]:
    """Ids of the sessions that have not been accepted or rejected, oldest first."""
    root = isolated_root()
    return sorted(p.name for p in root.iterdir() if (p / "session.json").is_file()) if root.is_dir() else []


def _strip_nested_git(work: Path) -> None:
    for dirpath, dirnames, _ in os.walk(work, followlinks=False):
        if ".git" in dirnames:
            shutil.rmtree(Path(dirpath) / ".git", ignore_errors=True)
            dirnames.remove(".git")
        for name in list(dirnames):
            if (Path(dirpath) / name).is_symlink():
                dirnames.remove(name)
    for gitfile in work.rglob(".git"):
        if gitfile.is_file() or gitfile.is_symlink():
            gitfile.unlink()


def _commit_all(iso: Isolated, message: str, ref: str) -> str:
    _strip_nested_git(iso.work)
    _git(iso.gitdir, iso.work, "add", "-A", "--force", cwd=iso.work)
    tree = _git(iso.gitdir, iso.work, "write-tree", cwd=iso.work).decode().strip()
    parents: list[str] = []
    if ref != "refs/heads/base":
        parents = [
            "-p",
            _git(iso.gitdir, None, "rev-parse", "refs/heads/base", cwd=iso.root).decode().strip(),
        ]
    commit = (
        _git(iso.gitdir, None, *_IDENT, "commit-tree", tree, *parents, "-m", message, cwd=iso.root)
        .decode()
        .strip()
    )
    _git(iso.gitdir, None, "update-ref", ref, commit, cwd=iso.root)
    return commit


def _flag(path: str, mode: str, patch: bytes) -> str:
    if mode == "120000":
        return "symlink"
    for pat in HIGH_RISK_PATTERNS:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path.split("/")[-1], pat):
            return f"touches {pat}"
    return "private key in added lines" if PRIVATE_KEY_RE.search(patch) else ""


def changes(iso: Isolated) -> list[Change]:
    """The diff report: every path the agent changed against the base, with a risk flag."""
    _commit_all(iso, "work", ref="refs/heads/work")
    raw = _git(
        iso.gitdir,
        None,
        "diff",
        "--raw",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        "refs/heads/base",
        "refs/heads/work",
        cwd=iso.root,
    )
    fields = raw.decode().split("\0")
    out: list[Change] = []
    i = 0
    while i + 1 < len(fields) and fields[i].startswith(":"):
        meta = fields[i].split()
        status, path = meta[4][0], fields[i + 1]
        new_mode = meta[1]
        patch = _git(
            iso.gitdir,
            None,
            "diff",
            "--no-ext-diff",
            "--binary",
            "refs/heads/base",
            "refs/heads/work",
            "--",
            path,
            cwd=iso.root,
        )
        out.append(Change(status, path, _flag(path, new_mode, patch)))
        i += 2
    return out


def patch_bytes(iso: Isolated) -> bytes:
    """The full binary patch from base to the agent's current state."""
    _commit_all(iso, "work", ref="refs/heads/work")
    return _git(
        iso.gitdir,
        None,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-ext-diff",
        "refs/heads/base",
        "refs/heads/work",
        cwd=iso.root,
    )


def _checkpoint(iso: Isolated) -> str:
    """Snapshots the real project into `$STATE/checkpoints/<project-key>`, which outlives the session."""
    gitdir = paths.ensure_private_dir(paths.state_dir() / "checkpoints") / scratch.project_key(iso.project)
    if not gitdir.is_dir():
        _git(gitdir, None, "init", "-q", "--bare", cwd=gitdir.parent)
    index = gitdir / "index"
    _git(
        gitdir, iso.project, "add", "-A", cwd=iso.project, index=index
    )  # the project's .gitignore keeps caches out
    tree = _git(gitdir, iso.project, "write-tree", cwd=iso.project, index=index).decode().strip()
    msg = f"checkpoint before accept {iso.sid}"
    commit = _git(gitdir, None, *_IDENT, "commit-tree", tree, "-m", msg, cwd=gitdir).decode().strip()
    _git(gitdir, None, "update-ref", f"refs/checkpoints/{iso.sid}", commit, cwd=gitdir)
    return commit


def accept(iso: Isolated, *, allow_flags: bool = False) -> list[Change]:
    """Applies the agent's changes to the project, all or nothing; returns what was applied.

    Raises `AcceptRefused` on a high-risk flag (unless `allow_flags`) or when the patch does not
    apply; in the second case the patch is kept under `$STATE/patches/<sid>.patch`.
    """
    report = changes(iso)
    flagged = [c for c in report if c.flag]
    if flagged and not allow_flags:
        listing = "; ".join(f"{c.path} ({c.flag})" for c in flagged)
        raise AcceptRefused(f"high-risk changes: {listing}; review them, then pass --allow-flags")
    if not report:
        return []
    patch = patch_bytes(iso)
    _checkpoint(iso)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    apply = ["git", *_GIT_BASE, "apply", "--binary", "--whitespace=nowarn"]
    try:
        proc.run([*apply, "--check", "-"], timeout=GIT_TIMEOUT_S, cwd=iso.project, env=env, input=patch)
        proc.run([*apply, "-"], timeout=GIT_TIMEOUT_S, cwd=iso.project, env=env, input=patch)
    except Exception as exc:  # noqa: BLE001 - any failure keeps the patch and leaves the project unchanged
        kept = paths.ensure_private_dir(paths.state_dir() / "patches") / f"{iso.sid}.patch"
        kept.write_bytes(patch)
        raise AcceptRefused(
            f"patch does not apply ({exc}); saved to {kept}; the project is unchanged"
        ) from exc
    reject(iso)
    return report


def export(iso: Isolated, dest: Path, kind: str = "patch") -> Path:
    """Writes the agent's changes to `dest` as a `patch` or a git `bundle`; the project is untouched."""
    if kind not in ("patch", "bundle"):
        raise SandboxError("export kind must be patch or bundle")
    patch = patch_bytes(iso)
    dest = dest.resolve()
    if kind == "patch":
        dest.write_bytes(patch)
    else:
        _git(
            iso.gitdir,
            None,
            "bundle",
            "create",
            str(dest),
            "refs/heads/work",
            "^refs/heads/base",
            cwd=iso.root,
        )
    return dest


def reject(iso: Isolated) -> None:
    """Deletes the session copy and its shadow repository."""
    shutil.rmtree(iso.root, ignore_errors=True)
