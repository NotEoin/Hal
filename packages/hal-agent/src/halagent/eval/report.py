"""Scenario runs and reports, and the paired comparison of two reports (REQ-AGT-93, 97).

A report is JSON with ASCII escapes: one row per attempt (case id, repeat index, verdict,
problems) and a summary with the Wilson interval. `compare` pairs rows on (id, repeat) and names
a winner only when the promotion rule holds.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from halagent.eval import stats
from halagent.eval.scenarios import AssistantRunner, Case, check

log = logging.getLogger("halagent.eval")
REPORT_NAME = "report.json"


@dataclass(frozen=True)
class Attempt:
    """The verdict of one run of one case."""

    id: str
    repeat: int
    passed: bool
    problems: list[str]
    reply: str = ""
    seconds: float = 0.0


@dataclass(frozen=True)
class Report:
    """All attempts of one model plus the summary."""

    model: str
    at: str
    attempts: list[Attempt]

    @property
    def passed(self) -> int:
        """Attempts that passed."""
        return sum(a.passed for a in self.attempts)

    def summary(self) -> dict[str, object]:
        """Counts and the 95 percent Wilson interval of the pass rate."""
        low, high = stats.wilson(self.passed, len(self.attempts))
        return {"passed": self.passed, "total": len(self.attempts), "wilson_low": round(low, 4),
                "wilson_high": round(high, 4)}  # fmt: skip

    def write(self, out_dir: Path) -> Path:
        """Writes `report.json` in `out_dir` and returns its path."""
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / REPORT_NAME
        doc = {"model": self.model, "at": self.at, "summary": self.summary(),
               "attempts": [asdict(a) for a in self.attempts]}  # fmt: skip
        path.write_text(json.dumps(doc, indent=1), encoding="ascii")
        return path


def run_scenarios(
    cases: Sequence[Case], runner: AssistantRunner, *, model: str, repeat: int = 1,
    seed: int | None = None, out_dir: Path | None = None,
) -> Report:  # fmt: skip
    """Runs every case `repeat` times through `runner`; a raising runner fails that attempt."""
    attempts = []
    for case in cases:
        for n in range(repeat):
            started = time.monotonic()
            try:
                turns = runner.run_case(case, model=model, seed=None if seed is None else seed + n)
                problems = check(case, turns, runner)
                reply = turns[-1].reply if turns else ""
            except Exception as exc:  # noqa: BLE001 - a broken turn fails the case, not the run
                log.warning("case %s failed: %s", case.id, exc)
                problems, reply = [f"error: {exc}"], ""
            attempts.append(
                Attempt(case.id, n, not problems, problems, reply, round(time.monotonic() - started, 2))
            )
    report = Report(model, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), attempts)
    if out_dir:
        report.write(out_dir)
    return report


def load_report(path: Path) -> Report:
    """Reads a report written by `Report.write`."""
    doc = json.loads(path.read_text(encoding="ascii"))
    return Report(doc["model"], doc["at"], [Attempt(**a) for a in doc["attempts"]])


def compare(a: Report, b: Report) -> dict[str, object]:
    """Pairs two reports on (id, repeat); `winner` is "a", "b" or "no detectable difference"."""
    pa = {(x.id, x.repeat): x.passed for x in a.attempts}
    pb = {(x.id, x.repeat): x.passed for x in b.attempts}
    keys = sorted(set(pa) & set(pb))
    if not keys:
        raise ValueError("the reports share no (id, repeat) pairs")
    wins_a = sum(pa[k] and not pb[k] for k in keys)
    wins_b = sum(pb[k] and not pa[k] for k in keys)
    by_case_a: dict[str, list[bool]] = {}
    by_case_b: dict[str, list[bool]] = {}
    for cid, n in keys:
        by_case_a.setdefault(cid, []).append(pa[(cid, n)])
        by_case_b.setdefault(cid, []).append(pb[(cid, n)])
    mean, low, high = stats.paired_bootstrap(by_case_a, by_case_b)
    winner = "a" if stats.promote(wins_a, wins_b) else "b" if stats.promote(wins_b, wins_a) else None
    return {"pairs": len(keys), "b_only_a_passes": wins_a, "c_only_b_passes": wins_b,
            "mcnemar_p": stats.mcnemar_exact(wins_a, wins_b), "mean_diff": mean, "ci": [low, high],
            "winner": winner or "no detectable difference"}  # fmt: skip
