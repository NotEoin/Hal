"""`hal-sandbox doctor` and `ls`: reports on the sandbox setup and on running boxes (step 32).

Read-only: it probes and lists, and never starts or stops anything. Every check returns a
`Check` so the CLI can print text or ASCII JSON from the same data.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from halkit import proc

from . import boximages, detect, images
from . import config as sbconfig
from .errors import SandboxError
from .labels import LABEL_SANDBOX, LIST_TIMEOUT_S
from .lockdown import Policy

OK, WARN, FAIL = "ok", "warn", "fail"
_LS_FORMAT = '{{.Names}}\t{{.Label "hal.kind"}}\t{{.Label "hal.session"}}\t{{.Status}}'


@dataclass(frozen=True)
class Check:
    """One doctor line: a name, a status (`ok`, `warn`, `fail`) and a detail with the fix."""

    name: str
    status: str
    detail: str


def run_checks(*, refresh: bool = False) -> list[Check]:
    """Probes lockdown, backend, runtime and images; never raises for a failed probe."""
    cfg = sbconfig.load_all()["sandbox"]
    level = str(cfg["lockdown"])
    try:
        policy = Policy.for_level(level)
    except SandboxError as exc:
        return [Check("lockdown", FAIL, str(exc))]
    out = [Check("lockdown", WARN if level == "off" else OK, level)]
    try:
        probe = detect.detect(cfg, policy, refresh=refresh)
    except SandboxError as exc:
        out.append(Check("backend", FAIL, str(exc)))
    else:
        out.append(
            Check("backend", OK if probe.ok else FAIL, f"{probe.backend} {probe.runtime or ''}".strip())
        )
        if probe.backend == "docker" and probe.runtime != "runsc":
            msg = f"runc shares the host kernel; {detect.RUNSC_FIX}"
            out.append(Check("runtime", FAIL if policy.require_runsc else WARN, msg))
        elif not probe.ok:
            out.append(Check("runtime", FAIL, probe.detail))
    try:
        rows = images.status()
    except SandboxError as exc:
        rows = []
        out.append(Check("images", FAIL, str(exc)))
    if not rows:
        out.append(Check("images", FAIL, "no sandbox image; run `hal box image build`"))
    for row in rows:
        mb = int(row["size"]) // 10**6
        budget = images.SIZE_BUDGET_MB.get(str(row["tag"]).partition(":")[2].split("-")[0])
        over = budget is not None and mb > budget
        out.append(
            Check(
                f"image {row['tag']}",
                WARN if over else OK,
                f"{mb} MB" + (f" (budget {budget})" if over else ""),
            )
        )
    built = [h["name"] for h in boximages.harnesses() if h["built"]]
    out.append(
        Check(
            "box images", OK if built else WARN, ", ".join(built) or "none built: `hal box image build all`"
        )
    )
    return out


def list_boxes(docker: str = "docker") -> list[dict[str, str]]:
    """The running Hal boxes of this user, from their labels."""
    done = proc.run(
        [docker, "ps", "--filter", f"label={LABEL_SANDBOX}=1", "--filter", f"label=hal.uid={os.getuid()}",
         "--format", _LS_FORMAT],
        timeout=LIST_TIMEOUT_S,
    )  # fmt: skip
    rows = []
    for line in done.stdout.decode(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            rows.append(dict(zip(("name", "kind", "session", "status"), parts, strict=True)))
    return rows


def as_dicts(checks: list[Check]) -> list[dict[str, str]]:
    """Checks as plain dicts for `--json`."""
    return [asdict(c) for c in checks]
