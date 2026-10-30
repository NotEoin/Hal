"""Git checkpoints of a work tree for `/undo` and `/diff` (02-sandbox REQ-BOX-45, 46, 49).

A checkpoint is a commit of the current work tree built from a temporary copy of the index, so
the user's index, branches and stash are never touched. It is stored under
`refs/hal/checkpoints/<sid>/<n>`. Design decisions:

- Files over `max_file_bytes`, FIFOs, sockets and devices are left out and listed, because a
  checkpoint of a data file would copy it on every turn.
- Masked files (secrets, REQ-BOX-45) are left out and listed unless the project is unmasked:
  checkpoint objects land in `.git/objects`, which the box can read, so a checkpoint must not be
  a second copy of a secret.
- Ignored paths are recorded by name in a metadata blob under `refs/hal/checkpoint-meta/`, outside
  the checkpoint tree, so a later restore can say what it cannot bring back.
- A tree equal to the session's previous checkpoint creates no new ref (REQ-BOX-46).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import stat
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from halkit import timeutil

from .config import DEFAULT_CHECKPOINT_FILE_BYTES
from .errors import SandboxError
from .gitops import GIT_TIMEOUT_S, GitRunner
from .mask import MASK_DEFAULT

log = logging.getLogger(__name__)

IGNORED_LIST_MAX = 10000  # more ignored entries than this are cut, and `truncated` says so
CKPT_PREFIX = "refs/hal/checkpoints"
META_PREFIX = "refs/hal/checkpoint-meta"
INTERNAL_DIRS = (".hal/",)  # rebuilt by `hal code --deps`, never worth listing or restoring
_NOT_REGULAR = (stat.S_ISFIFO, stat.S_ISSOCK, stat.S_ISCHR, stat.S_ISBLK)


class CheckpointError(SandboxError):
    """A checkpoint could not be taken or restored; the turn must not start (REQ-BOX-49)."""


@dataclass(frozen=True)
class CheckpointConfig:
    """The settings one `Checkpoints` object needs."""

    max_file_bytes: int = DEFAULT_CHECKPOINT_FILE_BYTES
    mask: Sequence[str] = MASK_DEFAULT
    unmasked: bool = False
    git_timeout_s: float = GIT_TIMEOUT_S


@dataclass(frozen=True)
class Checkpoint:
    """One stored checkpoint; `skipped` and `masked` hold names only."""

    sid: str
    n: int
    ref: str
    commit: str
    tree: str
    created: str
    label: str
    skipped: tuple[str, ...] = ()
    masked: tuple[str, ...] = ()
    ignored: tuple[str, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class RestoreResult:
    """What a restore did; `not_restorable` lists recorded paths that are gone and cannot come back."""

    restored: tuple[str, ...]
    deleted: tuple[str, ...]
    not_restorable: tuple[tuple[str, Literal["ignored", "skipped", "masked"]], ...]
    safety: Checkpoint


SECONDS_PER_DAY = 86400


def is_masked(rel: str, patterns: Sequence[str]) -> bool:
    """True when the last component of `rel` matches a mask pattern (the rule `find_masked` uses)."""
    name = rel.rstrip("/").rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _literal(paths: Sequence[str]) -> bytes:
    return b"".join(b":(literal)" + p.encode() + b"\0" for p in paths)


class Checkpoints:
    """Snapshots of the work tree for `/undo` and `/diff`; never touches the user's index or refs."""

    def __init__(self, work: Path, sid: str, git: GitRunner, cfg: CheckpointConfig) -> None:
        self.work = work
        self.sid = sid
        self.git = git
        self.cfg = cfg

    @classmethod
    def for_work(cls, work: Path, sid: str, cfg: CheckpointConfig | None = None) -> Checkpoints:
        """Checkpoints for a git work tree; raises `CheckpointError` when `work/.git` is not a directory."""
        cfg = cfg or CheckpointConfig()
        if not (work / ".git").is_dir():
            raise CheckpointError(f"{work} is not a git work tree with a .git directory")
        return cls(work, sid, GitRunner(work, timeout_s=cfg.git_timeout_s), cfg)

    # -- listing ----------------------------------------------------------------------------

    def _refs(self) -> list[tuple[int, str, str, str]]:
        """`(n, ref, commit, tree)` of this session's checkpoints, oldest first."""
        out = self.git.out(
            "for-each-ref", "--format=%(refname) %(objectname) %(tree)", f"{CKPT_PREFIX}/{self.sid}/"
        )
        rows = []
        for line in out.splitlines():
            ref, commit, tree = line.split()
            rows.append((int(ref.rsplit("/", 1)[1]), ref, commit, tree))
        return sorted(rows)

    def _meta(self, n: int) -> dict[str, object]:
        done = self.git.run("cat-file", "blob", f"{META_PREFIX}/{self.sid}/{n}", check=False)
        if done.returncode != 0:
            return {}
        return json.loads(done.stdout)  # type: ignore[no-any-return]  # we wrote it

    def _build(self, n: int, ref: str, commit: str, tree: str) -> Checkpoint:
        meta = self._meta(n)
        msg = self.git.out("log", "-1", "--format=%B", commit).splitlines()
        label = next((m[7:] for m in msg if m.startswith("label: ")), "")
        created = next((m[9:] for m in msg if m.startswith("created: ")), "")
        return Checkpoint(
            self.sid,
            n,
            ref,
            commit,
            tree,
            created,
            label,
            tuple(s["path"] for s in meta.get("skipped", [])),  # type: ignore[attr-defined]  # json
            tuple(meta.get("masked", [])),  # type: ignore[arg-type]  # json
            tuple(meta.get("ignored", [])),  # type: ignore[arg-type]  # json
        )

    def list(self) -> list[Checkpoint]:
        """This session's checkpoints, oldest first."""
        return [self._build(*row) for row in self._refs()]

    # -- taking -----------------------------------------------------------------------------

    def _candidates(self, idx: Path) -> list[str]:
        raw = self.git.run("ls-files", "-z", "--cached", "--others", "--exclude-standard", index=idx).stdout
        return sorted({p for p in raw.decode(errors="surrogateescape").split("\0") if p})

    def _classify(self, rels: Sequence[str]) -> tuple[list[str], list[dict[str, object]], list[str]]:
        """Splits paths into `(include, skipped, masked)`; a vanished path stays in `include` (a deletion)."""
        include: list[str] = []
        skipped: list[dict[str, object]] = []
        masked: list[str] = []
        for rel in rels:
            if not self.cfg.unmasked and is_masked(rel, self.cfg.mask):
                masked.append(rel)
                continue
            try:
                st = os.lstat(self.work / rel)
            except OSError:
                include.append(rel)
                continue
            if any(test(st.st_mode) for test in _NOT_REGULAR) or (
                stat.S_ISREG(st.st_mode) and st.st_size > self.cfg.max_file_bytes
            ):
                skipped.append({"path": rel, "bytes": st.st_size})
            else:
                include.append(rel)
        return include, skipped, masked

    def _ignored(self) -> tuple[list[str], bool]:
        raw = self.git.run(
            "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"
        ).stdout
        names = [p for p in raw.decode(errors="surrogateescape").split("\0") if p and p not in INTERNAL_DIRS]
        return names[:IGNORED_LIST_MAX], len(names) > IGNORED_LIST_MAX

    def tree_of_work(self, idx: Path) -> tuple[str, list[dict[str, object]], list[str]]:
        """Stages the work tree into the temporary index `idx`; returns `(tree, skipped, masked)`."""
        include, skipped, masked = self._classify(self._candidates(idx))
        left_out = [str(s["path"]) for s in skipped] + masked
        if include:
            self.git.run(
                "add",
                "-A",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                index=idx,
                input=_literal(include),
            )
        if left_out:
            self.git.run(
                "rm", "--cached", "--quiet", "--ignore-unmatch", "--pathspec-from-file=-",
                "--pathspec-file-nul", index=idx, input=_literal(left_out),
            )  # fmt: skip
        return self.git.out("write-tree", index=idx), skipped, masked

    def _temp_index(self) -> Path:
        fd, name = tempfile.mkstemp(prefix="hal-idx-")
        os.close(fd)
        idx = Path(name)
        real = self.git.git_dir / "index"
        if real.is_file():
            idx.write_bytes(real.read_bytes())  # keeps the stat cache so `add` rehashes little
        else:
            idx.unlink()
        return idx

    def take(self, label: str) -> Checkpoint:
        """Takes a checkpoint, or returns the previous one when the tree is unchanged.

        Raises `CheckpointError` on any failure so the caller can refuse to start the turn.
        """
        idx = self._temp_index()
        try:
            tree, skipped, masked = self.tree_of_work(idx)
            rows = self._refs()
            if rows and rows[-1][3] == tree:
                return self._build(*rows[-1])
            return self._commit(tree, label, skipped, masked, rows)
        except SandboxError as exc:
            raise CheckpointError(f"checkpoint failed: {exc}") from exc
        finally:
            idx.unlink(missing_ok=True)

    def _commit(
        self, tree: str, label: str, skipped: list[dict[str, object]], masked: list[str], rows: list
    ) -> Checkpoint:
        n = rows[-1][0] + 1 if rows else 0
        created = timeutil.iso_z()
        parent = rows[-1][2] if rows else self._head()
        ignored, truncated = self._ignored()
        lines = [f"hal checkpoint {self.sid} {n}", "", f"label: {label}", f"created: {created}"]
        lines += [f"skipped: {_ascii(str(s['path']))} ({s['bytes']} bytes)" for s in skipped]
        lines += [f"masked: {_ascii(m)}" for m in masked]
        args = ["commit-tree", tree, "-m", "\n".join(lines)]
        if parent:
            args += ["-p", parent]
        commit = self.git.out(*args)
        ref = f"{CKPT_PREFIX}/{self.sid}/{n}"
        self.git.run("update-ref", ref, commit)
        meta = json.dumps({"ignored": ignored, "masked": masked, "skipped": skipped, "truncated": truncated})
        blob = self.git.out("hash-object", "-w", "--stdin", input=meta.encode())
        self.git.run("update-ref", f"{META_PREFIX}/{self.sid}/{n}", blob)
        log.debug("checkpoint %s %d %s", self.sid, n, commit)
        return Checkpoint(
            self.sid, n, ref, commit, tree, created, label,
            tuple(str(s["path"]) for s in skipped), tuple(masked), tuple(ignored),
        )  # fmt: skip

    def restore(self, target: Checkpoint) -> RestoreResult:
        """Makes every non-ignored path equal to `target` after taking a safety checkpoint.

        Ignored, skipped and masked files and the user's index are not touched (REQ-BOX-47).
        """
        safety = self.take(f"before restore to {target.n}")
        idx = self._temp_index()
        try:
            self.tree_of_work(idx)  # the index of the tree as it is now, so restore sees what to delete
            changes = self.git.out(
                "diff-tree", "-r", "--name-status", "--no-renames", "-z", safety.tree, target.tree
            )
            self.git.run("restore", f"--source={target.commit}", "--worktree", "--", ":/", index=idx)
        except SandboxError as exc:
            raise CheckpointError(f"restore failed: {exc}") from exc
        finally:
            idx.unlink(missing_ok=True)
        restored: list[str] = []
        deleted: list[str] = []
        parts = changes.split("\0")
        for status, path in zip(parts[0::2], parts[1::2], strict=False):
            (deleted if status == "D" else restored).append(path)
        return RestoreResult(tuple(restored), tuple(deleted), self._not_restorable(target), safety)

    def _not_restorable(
        self, target: Checkpoint
    ) -> tuple[tuple[str, Literal["ignored", "skipped", "masked"]], ...]:
        missing = []
        for reason, names in (
            ("ignored", target.ignored),
            ("skipped", target.skipped),
            ("masked", target.masked),
        ):
            missing += [(n, reason) for n in names if not os.path.lexists(self.work / n)]
        return tuple(missing)  # type: ignore[arg-type]  # reason is one of the three literals

    def prune(self, *, keep_sessions: int, max_age_days: int, now: float | None = None) -> int:
        """Deletes sessions older than `max_age_days` beyond the newest `keep_sessions`; returns the count.

        The running session is never pruned.
        """
        out = self.git.out("for-each-ref", "--format=%(refname) %(committerdate:unix)", f"{CKPT_PREFIX}/")
        newest: dict[str, int] = {}
        for line in out.splitlines():
            ref, stamp = line.split()
            sid = ref.split("/")[3]
            newest[sid] = max(newest.get(sid, 0), int(stamp))
        cutoff = (time.time() if now is None else now) - max_age_days * SECONDS_PER_DAY
        by_age = sorted(newest, key=lambda s: newest[s], reverse=True)
        doomed = [s for s in by_age[keep_sessions:] if newest[s] < cutoff and s != self.sid]
        for sid in doomed:
            for prefix in (CKPT_PREFIX, META_PREFIX):
                refs = self.git.out("for-each-ref", "--format=%(refname)", f"{prefix}/{sid}/").split()
                for ref in refs:
                    self.git.run("update-ref", "-d", ref)
        return len(doomed)

    def _head(self) -> str | None:
        done = self.git.run("rev-parse", "--verify", "-q", "HEAD", check=False)
        return done.stdout.decode().strip() if done.returncode == 0 else None


def _ascii(text: str) -> str:
    """Escapes non-ASCII and control characters so a commit message stays one clean ASCII line."""
    return text.encode("ascii", "backslashreplace").decode().replace("\n", "\\n").replace("\r", "\\r")
