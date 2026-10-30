"""Hardened host-side git for trees the model can write (02-sandbox D5, REQ-BOX-48).

A nested repository's `core.fsmonitor` once ran on the host through a parent `git status`
[verified: `cprepo` experiment]. Every host git call therefore goes through `GitRunner`:
explicit `--git-dir` and `--work-tree`, a working directory outside the work tree, every
inherited `GIT_*` variable removed, no system or global config (only the user's
`core.excludesFile` is passed on, so globally ignored files stay out of checkpoints), the
hardening `-c` options and a timeout.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from halkit import proc

from .errors import SandboxError

log = logging.getLogger(__name__)

GIT_TIMEOUT_S = 60.0
IDENT = "Hal <hal@localhost>"
HARDEN_ARGS = (
    "-c", "core.fsmonitor=false",  # nested repo config cannot run commands
    "-c", "core.hooksPath=/dev/null",  # no hooks from host-side plumbing
    "-c", "diff.ignoreSubmodules=all",  # never recurse into nested repos
    "-c", "status.submoduleSummary=false",
    "-c", "submodule.recurse=false",
    "-c", "core.untrackedCache=false",
    "-c", "protocol.allow=never",  # no transport, ever
    "-c", "core.pager=cat",
    "-c", "core.quotepath=off",
)  # fmt: skip
_DIFF_COMMANDS = ("diff", "log", "show")


class GitError(SandboxError):
    """A host git call failed; carries the argv, exit status and stderr."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        super().__init__(f"git {argv[0] if argv else ''} failed ({returncode}): {stderr.strip()[:300]}")
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


def _global_excludes() -> str | None:
    """The user's `core.excludesFile`, read once with the user's own config."""
    try:
        done = proc.run(["git", "config", "--global", "--get", "core.excludesFile"], timeout=10, check=False)
    except (proc.ProcTimeout, OSError):
        return None
    value = done.stdout.decode(errors="replace").strip()
    return str(Path(value).expanduser()) if done.returncode == 0 and value else None


_EXCLUDES: list[str | None] = []


class GitRunner:
    """Runs host git on a model-written tree with hardening, a timeout and a clean config."""

    def __init__(
        self, work_tree: Path, *, git_dir: Path | None = None, timeout_s: float = GIT_TIMEOUT_S
    ) -> None:
        self.work_tree = Path(work_tree)
        self.git_dir = Path(git_dir) if git_dir else self.work_tree / ".git"
        self.timeout_s = timeout_s
        self._cwd = Path(tempfile.gettempdir())  # outside every work tree a session can write

    def _env(self, index: Path | None) -> dict[str, str]:
        if not _EXCLUDES:
            _EXCLUDES.append(_global_excludes())
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            "GIT_AUTHOR_NAME": "Hal",
            "GIT_AUTHOR_EMAIL": "hal@localhost",
            "GIT_COMMITTER_NAME": "Hal",
            "GIT_COMMITTER_EMAIL": "hal@localhost",
        }
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        return env

    def argv(self, *args: str) -> list[str]:
        """The full command line for `args`."""
        head = ["git", *HARDEN_ARGS]
        if _EXCLUDES and _EXCLUDES[0]:
            head += ["-c", f"core.excludesFile={_EXCLUDES[0]}"]
        if args and args[0] == "init":  # init takes its target as an argument, never --git-dir
            return [*head, *args]
        head += ["--git-dir", str(self.git_dir), "--work-tree", str(self.work_tree)]
        if args and args[0] in _DIFF_COMMANDS:
            return [*head, args[0], "--no-ext-diff", "--no-textconv", *args[1:]]
        return [*head, *args]

    def run(
        self, *args: str, index: Path | None = None, input: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        """Runs `git <args>`; raises `GitError` on failure when `check`, and on a timeout."""
        env = self._env(index)
        argv = self.argv(*args)
        try:
            done = proc.run(argv, timeout=self.timeout_s, cwd=self._cwd, env=env, input=input, check=False)
        except proc.ProcTimeout as exc:
            raise GitError(list(args), -1, f"timed out after {self.timeout_s:g}s") from exc
        except OSError as exc:
            raise GitError(list(args), -1, str(exc)) from exc
        if check and done.returncode != 0:
            raise GitError(list(args), done.returncode, done.stderr.decode(errors="replace"))
        return done

    def out(self, *args: str, index: Path | None = None, input: bytes | None = None) -> str:
        """Runs git and returns stdout, decoded and stripped."""
        return self.run(*args, index=index, input=input).stdout.decode(errors="replace").strip()
