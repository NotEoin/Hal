"""`hal status` and `hal doctor` lines for the lockdown level and the risk classifier.

Read-only: it reports `[sandbox].lockdown` and `[policy].paranoia` from the host files and never
writes either (RESOLUTIONS I2, I5). Design decisions:

- The classifier probe asks the gateway for its model list instead of sending a prompt, so a
  doctor run never loads a model or takes the GPU.
- "Unreachable" is reported with its consequence (every boxed call counts as high risk and
  asks), because the person needs to know why prompts appear, not only that a probe failed.
- `off` lockdown is always a problem line (red banner), never a default (I2).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field

from halgateway import config as gwconfig
from halgateway import models as gwmodels
from halhub import policy
from halhub.classifier import effective_paranoia
from halkit import llm
from halkit.config import ConfigError
from halsandbox import config as sandbox_config
from halsandbox.code import docker_runtimes
from halsandbox.lockdown import Policy

from . import modelplan

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 3.0


@dataclass
class Report:
    """What the security lines say: plain lines, warnings and problems."""

    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def collect(*, check_runtime: bool) -> Report:
    """Builds the report; `check_runtime` also asks docker which runtimes it has (doctor only)."""
    report = Report()
    level = str(sandbox_config.load()["lockdown"])
    lockdown = Policy.for_level(level)
    pol = policy.load_policy()
    paranoia = effective_paranoia(pol.classifier.paranoia, lockdown.paranoia_floor)
    report.lines.append(f"lockdown: {level}")
    floor = f", raised from {pol.classifier.paranoia} by lockdown {level}" if paranoia != pol.classifier.paranoia else ""
    report.lines.append(f"paranoia: {paranoia}{floor}")
    if level == "off":
        report.problems.append("lockdown is off: boxes are not required and nothing contains the model")
    if check_runtime:
        have = docker_runtimes()
        if "runsc" not in have:
            message = "runsc (gVisor) is not installed"
            if lockdown.require_runsc:
                report.problems.append(f"{message}: lockdown strict refuses to start")
            else:
                report.warnings.append(f"{message}: boxes run on runc and share the host kernel")
    if paranoia != "off":
        try:
            aliases = llm.ping(timeout=PROBE_TIMEOUT_S)
        except llm.GatewayError as exc:
            report.lines.append(f"classifier: UNREACHABLE ({pol.classifier.model})")
            report.warnings.append(
                f"classifier model unreachable ({exc}): every boxed call counts as high risk and asks"
            )
        else:
            report.lines.append(f"classifier: reachable ({pol.classifier.model})")
            if pol.classifier.model not in aliases:
                report.warnings.append(f"gateway does not list the classifier model {pol.classifier.model}")
            unpinned = unpinned_ollama_model(pol.classifier.model)
            if unpinned:
                report.warnings.append(
                    f"classifier model {unpinned} is not pinned in models.toml, so the gateway refuses it"
                    " and every boxed call asks; run `hal models` to pin it"
                )
    else:
        report.lines.append("classifier: off")
    return report


def unpinned_ollama_model(model: str) -> str | None:
    """The Ollama model a `backend/model` name points at when `models.toml` does not pin it, else None.

    The gateway answers a reachable but unpinned model with 503 `model_unverified`, which a ping
    cannot see; aliases and other backends are checked by the gateway's own commands instead.
    """
    backend, sep, name = model.partition("/")
    if not sep:
        return None
    try:
        backends = gwconfig.load_config().backends
        pinned = {e.name for e in gwmodels.list_entries(modelplan.manifest_store())}
    except (ConfigError, OSError, ValueError) as exc:
        log.warning("could not check whether %s is pinned: %s", model, exc)
        return None
    if str((backends.get(backend) or {}).get("type")) != "ollama":
        return None
    return None if name in pinned or f"{name}:latest" in pinned else name


def render(report: Report) -> str:
    """The report as text; problems first so they are not missed."""
    out = [f"PROBLEM: {p}" for p in report.problems]
    out += [f"warning: {w}" for w in report.warnings]
    return "\n".join([*out, *report.lines])


def main(argv: list[str]) -> int:
    """`hal status` (`status`) and `hal doctor` (`doctor`) for the security lines; exit 1 on a problem."""
    cmd = argv[0] if argv else "status"
    if cmd not in ("status", "doctor"):
        print("usage: hal security [status|doctor]", file=sys.stderr)
        return 2
    report = collect(check_runtime=cmd == "doctor")
    print(render(report))
    return 1 if cmd == "doctor" and report.problems else 0
