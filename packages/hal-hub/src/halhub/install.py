"""Declared install steps, run after a typed confirmation (04-hub.md, "Install steps and units").

`plan(module)` shows each `[[install]]` step with the state of its last run; `run_steps`
asks `confirm` before each step that needs to run, runs it, and records the outcome.
`hal-hub setup MODULE` and 06-core-cli's `hal packs add` are the two callers; both supply a
`confirm` that needs a `y` typed on a terminal (REQ-HUB-59, 01 REQ-SEC-84).

Design decisions:

- No shell and always a timeout: each step runs through `halkit.proc.run(argv,
  timeout=step.timeout, cwd=...)`, and the first step that fails, times out or cannot start
  stops the run. This replaces v1's `_install_step`, which ran with none of these.
- A step whose last run succeeded with the same digest (sha256 of the rendered argv, working
  directory and timeout) is skipped, so re-running setup after an upgrade repeats only what
  changed; `force=True` runs every step again.
- State lives in `state_dir()/hub/install.json`, written atomically. Two concurrent setups of
  different modules can lose one record; the cost is one repeated step on the next run, so
  the file is not locked.
- A `False` from `confirm` stops the run there: later steps may depend on the skipped one.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from halkit import files, paths, proc
from halkit.timeutil import iso_z, utc_now

from .manifest import InstallStep, ModuleSpec

log = logging.getLogger(__name__)

__all__ = ["StepState", "plan", "run_steps", "state_file", "step_digest"]

STATE_FILE = Path("hub") / "install.json"  # under state_dir()
STDERR_TAIL_CHARS = 2000  # what a failed step's log line keeps of its stderr


@dataclass(frozen=True)
class StepState:
    """The recorded outcome of one install step's last run."""

    module: str
    step: str
    digest: str  # sha256 of the rendered argv, cwd and timeout
    status: Literal["ok", "failed"]
    at: str  # UTC Z
    exit_code: int | None  # None: timed out or could not start


def state_file() -> Path:
    """`state_dir()/hub/install.json`."""
    return paths.state_dir() / STATE_FILE


def step_digest(module: ModuleSpec, step: InstallStep) -> str:
    """sha256 hex of the step's rendered argv, working directory and timeout."""
    doc = {"run": list(step.run), "cwd": str(_cwd(module, step)), "timeout": step.timeout}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def plan(module: ModuleSpec) -> list[tuple[InstallStep, StepState | None]]:
    """Each step with its last recorded run, or None when it never ran with today's digest."""
    states = _load()
    out = []
    for step in module.install:
        prior = states.get(f"{module.name}/{step.id}")
        out.append((step, prior if prior is not None and prior.digest == step_digest(module, step) else None))
    return out


def run_steps(
    module: ModuleSpec, *, confirm: Callable[[InstallStep], bool], force: bool = False
) -> list[StepState]:
    """Runs the steps that need to run, each after `confirm`; returns the states of the steps run.

    Stops at the first `False` from `confirm` or the first failed step.
    """
    ran: list[StepState] = []
    for step, prior in plan(module):
        if prior is not None and prior.status == "ok" and not force:
            log.info("%s/%s already done; skipped", module.name, step.id)
            continue
        if not confirm(step):
            log.info("%s/%s not confirmed; stopping", module.name, step.id)
            break
        state = _run(module, step)
        _save(state)
        ran.append(state)
        if state.status != "ok":
            break
    return ran


def _run(module: ModuleSpec, step: InstallStep) -> StepState:
    cwd = _cwd(module, step)
    if step.cwd == "state":
        paths.ensure_private_dir(cwd)
    exit_code: int | None
    try:
        done = proc.run(step.run, timeout=step.timeout, cwd=cwd, check=False)
        exit_code = done.returncode
        if exit_code != 0:
            tail = done.stderr.decode(errors="replace")[-STDERR_TAIL_CHARS:]
            log.error("%s/%s exited with %d: %s", module.name, step.id, exit_code, tail)
    except proc.ProcTimeout:
        log.error("%s/%s took longer than %g s and was stopped", module.name, step.id, step.timeout)
        exit_code = None
    except OSError as exc:
        log.error("%s/%s could not start: %s", module.name, step.id, exc)
        exit_code = None
    return StepState(
        module=module.name,
        step=step.id,
        digest=step_digest(module, step),
        status="ok" if exit_code == 0 else "failed",
        at=iso_z(utc_now()),
        exit_code=exit_code,
    )


def _cwd(module: ModuleSpec, step: InstallStep) -> Path:
    return module.root if step.cwd == "root" else module.state_dir()


def _load() -> dict[str, StepState]:
    path = state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:  # a damaged record only means steps run again
        log.warning("ignoring unreadable install state %s: %s", path, exc)
        return {}
    states = {}
    for key, record in (data.get("steps") or {}).items():
        try:
            states[key] = StepState(**record)
        except TypeError:
            log.warning("ignoring a malformed install record %s in %s", key, path)
    return states


def _save(state: StepState) -> None:
    states = _load()
    states[f"{state.module}/{state.step}"] = state
    doc = {"steps": {key: asdict(value) for key, value in sorted(states.items())}}
    path = state_file()
    paths.ensure_private_dir(path.parent)
    files.write_atomic(path, json.dumps(doc, indent=1) + "\n")
