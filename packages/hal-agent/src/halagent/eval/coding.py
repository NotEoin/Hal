"""Coding evaluation: the task format, the attempt pipeline and the metrics (03-agent 3.18.2, 3.18.3).

A `BoxRunner` (implemented by halcore on halsandbox) runs the agent in a fresh box and the check
in a second fresh box. Grading never reads the agent's tree: the agent's work is taken as a patch
and applied to a clean copy of `repo/`, because the agent's tree is untrusted (REQ-AGT-95).
This module only prepares directories and runs `git`; it starts no agent and no container itself.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from halagent.eval import stats
from halagent.settings import AgentSettings

log = logging.getLogger("halagent.eval")

BASELINE_TAG = "hal-eval-baseline"
GIT_TIMEOUT_S = 60
TASK_KEYS = frozenset("id title kind task check protected timeout_s split license".split())
KINDS, SPLITS = ("bugfix", "feature", "refactor"), ("dev", "heldout")
_GIT_ENV = ("-c", "user.name=hal-eval", "-c", "user.email=eval@example.invalid", "-c", "commit.gpgsign=false")


class TaskError(ValueError):
    """A task directory is malformed; the message names the task and key."""


@dataclass(frozen=True)
class CodingTask:
    """One task: `root` holds `task.toml`, `repo/`, `hidden/` and `gold.patch`."""

    id: str
    title: str
    kind: str
    task: str
    check: str
    protected: tuple[str, ...]
    timeout_s: int
    split: str
    license: str
    root: Path


@dataclass(frozen=True)
class AgentRun:
    """What the headless agent reported for one attempt."""

    stop_reason: str
    wall_s: float
    model_requests: int = 0
    tool_calls: int = 0
    first_edit_call: int | None = None
    peak_prompt_tokens: int = 0


@dataclass(frozen=True)
class CheckResult:
    """The exit code and output of a grading command."""

    exit_code: int
    output: str = ""


class BoxRunner(Protocol):
    """Runs commands in a fresh sandbox; implemented by halcore on halsandbox (06-core-cli)."""

    def run_agent(self, workspace: Path, task: str, settings: AgentSettings, timeout_s: float) -> AgentRun:
        """Runs the headless agent on `workspace` in task mode."""
        ...

    def run_check(self, workspace: Path, command: str, timeout_s: float) -> CheckResult:
        """Runs `command` with `bash -c` in `workspace` in a second fresh box."""
        ...


@dataclass(frozen=True)
class Attempt:
    """One line of `results.jsonl`."""

    task: str
    split: str
    repeat: int
    seed: int
    model: str
    resolved: bool
    empty_patch: bool
    stop_reason: str
    wall_s: float
    model_requests: int
    tool_calls: int
    first_edit_call: int | None
    peak_prompt_tokens: int
    check_exit: int
    patch_lines: int
    protected_edit_attempts: int = 0


@dataclass
class CodingReport:
    """Attempts of one model, with the summary written next to them."""

    model: str
    attempts: list[Attempt] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        """Counts per split, the resolved rate with its Wilson interval, and pass^k / pass@k per task."""
        by_task: dict[str, list[bool]] = {}
        for a in self.attempts:
            by_task.setdefault(a.task, []).append(a.resolved)
        n = len(self.attempts)
        k = sum(a.resolved for a in self.attempts)
        low, high = stats.wilson(k, n)
        per_task = {}
        for task, runs in by_task.items():
            c, m = sum(runs), len(runs)
            per_task[task] = {"resolved": c, "attempts": m, "pass_at_k": stats.pass_at_k(c, m, m),
                              "pass_hat_k": stats.pass_hat_k(c, m, m)}  # fmt: skip
        walls = sorted(a.wall_s for a in self.attempts)
        return {
            "model": self.model, "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "attempts": n, "resolved": k, "wilson_low": round(low, 4), "wilson_high": round(high, 4),
            "per_split": {s: sum(a.split == s for a in self.attempts) for s in SPLITS},
            "per_task": per_task,
            "wall_s_median": walls[len(walls) // 2] if walls else 0.0,
            "wall_s_p90": walls[min(len(walls) - 1, int(0.9 * len(walls)))] if walls else 0.0,
            "tool_calls": sum(a.tool_calls for a in self.attempts),
            "model_requests": sum(a.model_requests for a in self.attempts),
        }  # fmt: skip

    def write(self, out_dir: Path) -> None:
        """Writes `results.jsonl` and `summary.json` (ASCII) into `out_dir`."""
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "results.jsonl").write_text(
            "".join(json.dumps(asdict(a)) + "\n" for a in self.attempts), encoding="ascii"
        )
        (out_dir / "summary.json").write_text(json.dumps(self.summary(), indent=1), encoding="ascii")


def load_tasks(root: Path, split: str | None = None) -> list[CodingTask]:
    """Reads every `<root>/<id>/task.toml`; an unknown key, kind or split fails loudly."""
    tasks = []
    for toml in sorted(root.glob("*/task.toml")):
        try:
            raw = tomllib.loads(toml.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TaskError(f"{toml}: {exc}") from exc
        tid = raw.get("id", toml.parent.name)
        if missing := (TASK_KEYS - set(raw)):
            raise TaskError(f"task {tid}: missing {sorted(missing)[0]}")
        if unknown := (set(raw) - TASK_KEYS):
            raise TaskError(f"task {tid}: unknown key {sorted(unknown)[0]}")
        if tid != toml.parent.name or raw["kind"] not in KINDS or raw["split"] not in SPLITS:
            raise TaskError(f"task {tid}: id must match its folder, kind {KINDS}, split {SPLITS}")
        for part in ("repo", "hidden"):
            if not (toml.parent / part).is_dir():
                raise TaskError(f"task {tid}: no {part}/ folder")
        if not (toml.parent / "gold.patch").is_file():
            raise TaskError(f"task {tid}: no gold.patch")
        task = CodingTask(**{**raw, "protected": tuple(raw["protected"])}, root=toml.parent)
        if split in (None, task.split):
            tasks.append(task)
    return tasks


def _git(cwd: Path, *args: str, stdin: str | None = None) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *_GIT_ENV, *args], cwd=cwd, input=stdin, capture_output=True, text=True,
        timeout=GIT_TIMEOUT_S, check=False,
    )  # fmt: skip
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def prepare_workspace(task: CodingTask, dest: Path) -> Path:
    """Copies `repo/` to `dest`, commits it and tags the commit `hal-eval-baseline`."""
    shutil.copytree(task.root / "repo", dest)
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "baseline")
    _git(dest, "tag", BASELINE_TAG)
    return dest


def take_patch(workspace: Path) -> str:
    """The agent's work as a patch against the baseline, untracked files included."""
    _git(workspace, "add", "-N", ".")
    return _git(workspace, "diff", BASELINE_TAG)


def grade(task: CodingTask, patch: str, runner: BoxRunner, scratch: Path) -> CheckResult:
    """Applies `patch` to a clean copy of `repo/`, resets protected paths, adds `hidden/`, runs the check."""
    clean = prepare_workspace(task, scratch / "grade")
    if patch.strip():
        _git(clean, "apply", "--whitespace=nowarn", "-", stdin=patch)
        for glob in task.protected:  # a patch may not edit what the grader trusts
            for path in clean.glob(glob):
                rel = str(path.relative_to(clean))
                if _git(clean, "ls-tree", "-r", "--name-only", BASELINE_TAG, "--", rel).strip():
                    _git(clean, "checkout", BASELINE_TAG, "--", rel)
                elif path.is_file():
                    path.unlink()  # added by the patch, absent from the baseline
    shutil.copytree(task.root / "hidden", clean / "hidden", dirs_exist_ok=True)
    return runner.run_check(clean, task.check, task.timeout_s)


def gold_null_check(task: CodingTask, runner: BoxRunner) -> tuple[bool, bool]:
    """(gold passes, empty patch fails): REQ-AGT-94 for one task."""
    with tempfile.TemporaryDirectory(prefix="hal-gold-") as tmp:
        gold = grade(task, (task.root / "gold.patch").read_text(encoding="utf-8"), runner, Path(tmp) / "g")
        null = grade(task, "", runner, Path(tmp) / "n")
    return gold.exit_code == 0, null.exit_code != 0


def run_coding(
    model: str, tasks: Sequence[CodingTask], *, repeat: int, seed: int, runner: BoxRunner,
    out_dir: Path, settings: AgentSettings,
) -> CodingReport:  # fmt: skip
    """Runs every task `repeat` times; each attempt gets a fresh workspace and a separate grading copy."""
    report = CodingReport(model)
    for task in tasks:
        for n in range(repeat):
            with tempfile.TemporaryDirectory(prefix="hal-eval-") as tmp:
                work = prepare_workspace(task, Path(tmp) / "work")
                try:
                    run = runner.run_agent(work, task.task, settings, float(task.timeout_s))
                    patch = take_patch(work)
                    check = grade(task, patch, runner, Path(tmp))
                except Exception as exc:  # noqa: BLE001 - a broken attempt is a failed attempt, not a failed run
                    log.warning("attempt %s/%d failed: %s", task.id, n, exc)
                    run, patch, check = AgentRun(f"error: {exc}", 0.0), "", CheckResult(-1, str(exc))
            lines = sum(1 for ln in patch.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))
            report.attempts.append(Attempt(
                task.id, task.split, n, seed + n, model, check.exit_code == 0 and bool(patch.strip()),
                not patch.strip(), run.stop_reason, run.wall_s, run.model_requests, run.tool_calls,
                run.first_edit_call, run.peak_prompt_tokens, check.exit_code, lines,
            ))  # fmt: skip
    report.write(out_dir)
    return report
