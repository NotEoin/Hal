"""Statistics for evaluation results (03-agent 3.18.4): small-sample tests, no third-party code.

Noise is large at the sizes used here (38 assistant cases, 12 coding tasks), so every
comparison goes through these functions and `promote` is the only way to name a winner.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence

Z_95 = 1.959964  # two-sided 95 percent normal quantile
PROMOTE_MIN_GAP = 2  # b - c must also be at least this many cases
BOOTSTRAP_LOW, BOOTSTRAP_HIGH = 0.025, 0.975


def wilson(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """The Wilson score interval for `k` successes in `n` trials; (0, 1) when `n` is 0."""
    if n < 0 or not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n, got k={k} n={n}")
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p value; `b` cases A passes and B fails, `c` the reverse."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2 * tail / 2**n)


def _rate(results: Sequence[bool]) -> float:
    return sum(results) / len(results)


def paired_bootstrap(
    a: Mapping[str, Sequence[bool]], b: Mapping[str, Sequence[bool]], *, iters: int = 10_000, seed: int = 0
) -> tuple[float, float, float]:  # fmt: skip
    """Mean difference (A minus B) and its 95 percent interval, resampling tasks, not attempts.

    Repeats of one task are not independent, so the task is the unit that is resampled.
    """
    ids = sorted(set(a) & set(b))
    if not ids:
        raise ValueError("no case ids in common")
    diffs = [_rate(a[i]) - _rate(b[i]) for i in ids]
    rng = random.Random(seed)  # noqa: S311 - resampling, not security
    means = sorted(sum(rng.choices(diffs, k=len(diffs))) / len(diffs) for _ in range(iters))
    low = means[int(BOOTSTRAP_LOW * (iters - 1))]
    high = means[int(BOOTSTRAP_HIGH * (iters - 1))]
    return sum(diffs) / len(diffs), low, high


def pass_hat_k(c: int, n: int, k: int) -> float:
    """pass^k: the chance that `k` attempts drawn from `n` (with `c` passes) all pass."""
    _check_counts(c, n, k)
    return math.comb(c, k) / math.comb(n, k)


def pass_at_k(c: int, n: int, k: int) -> float:
    """pass@k: the chance that at least one of `k` attempts drawn from `n` passes."""
    _check_counts(c, n, k)
    return 1 - math.comb(n - c, k) / math.comb(n, k)


def _check_counts(c: int, n: int, k: int) -> None:
    if not 0 <= c <= n or not 1 <= k <= n:
        raise ValueError(f"need 0 <= c <= n and 1 <= k <= n, got c={c} n={n} k={k}")


def flip_rate(runs: Sequence[Mapping[str, bool]]) -> float:
    """The share of cases whose outcome differs between runs of one configuration."""
    if len(runs) < 2:
        raise ValueError("flip rate needs at least two runs")
    ids = set(runs[0]).intersection(*runs[1:])
    if not ids:
        raise ValueError("no case ids in common")
    return sum(len({run[i] for run in runs}) > 1 for i in ids) / len(ids)


def promote(b: int, c: int) -> bool:
    """The promotion rule: `b - c >= ceil(sqrt(b + c))` and `b - c >= 2`."""
    gap = b - c
    return gap >= PROMOTE_MIN_GAP and gap >= math.ceil(math.sqrt(b + c))
