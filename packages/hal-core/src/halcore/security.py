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

import sys
from dataclasses import dataclass, field

from halhub import policy
from halhub.classifier import effective_paranoia
from halkit import llm
from halsandbox import config as sandbox_config
from halsandbox.code import docker_runtimes
from halsandbox.lockdown import Policy

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
    else:
        report.lines.append("classifier: off")
    return report


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
