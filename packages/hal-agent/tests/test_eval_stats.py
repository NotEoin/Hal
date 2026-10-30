"""Tests for `halagent.eval.stats` against known values (REQ-AGT-96)."""

import math

import pytest

from halagent.eval import stats


def test_wilson_matches_the_textbook_interval():
    low, high = stats.wilson(8, 10)
    assert (round(low, 3), round(high, 3)) == (0.490, 0.943)


def test_wilson_edges_stay_inside_zero_and_one():
    assert stats.wilson(0, 10)[0] == pytest.approx(0.0, abs=1e-12)
    assert stats.wilson(10, 10)[1] == pytest.approx(1.0)
    assert stats.wilson(0, 0) == (0.0, 1.0)


def test_wilson_rejects_impossible_counts():
    with pytest.raises(ValueError):
        stats.wilson(5, 3)


def test_mcnemar_exact_known_values():
    assert stats.mcnemar_exact(0, 0) == 1.0
    assert stats.mcnemar_exact(5, 5) == 1.0
    assert math.isclose(stats.mcnemar_exact(0, 8), 2 / 256)
    assert math.isclose(stats.mcnemar_exact(1, 9), 2 * (1 + 10) / 1024)


def test_pass_hat_k_and_pass_at_k_known_values():
    assert stats.pass_hat_k(3, 5, 2) == pytest.approx(3 / 10)
    assert stats.pass_at_k(3, 5, 2) == pytest.approx(1 - 1 / 10)
    assert stats.pass_at_k(0, 5, 3) == 0.0 and stats.pass_hat_k(5, 5, 5) == 1.0


def test_flip_rate_counts_cases_that_change_between_runs():
    runs = [{"a": True, "b": True, "c": False, "d": False}, {"a": True, "b": False, "c": False, "d": True}]
    assert stats.flip_rate(runs) == 0.5


def test_flip_rate_needs_two_runs():
    with pytest.raises(ValueError):
        stats.flip_rate([{"a": True}])


def test_bootstrap_of_identical_results_is_zero_and_is_reproducible():
    a = {"t1": [True, True], "t2": [False, False]}
    assert stats.paired_bootstrap(a, a, iters=200) == (0.0, 0.0, 0.0)


def test_bootstrap_resamples_tasks_and_is_seeded():
    a = {f"t{i}": [True] for i in range(10)}
    b = {f"t{i}": [i < 4] for i in range(10)}
    first = stats.paired_bootstrap(a, b, iters=500, seed=1)
    assert first == stats.paired_bootstrap(a, b, iters=500, seed=1)
    mean, low, high = first
    assert mean == pytest.approx(0.6) and low < mean < high


@pytest.mark.parametrize(
    ("b", "c", "expected"),
    [(2, 0, True), (1, 0, False), (5, 3, False), (10, 2, True), (6, 6, False), (4, 0, True)],
)
def test_promotion_needs_a_gap_of_two_and_sqrt_of_discordant(b, c, expected):
    assert stats.promote(b, c) is expected
