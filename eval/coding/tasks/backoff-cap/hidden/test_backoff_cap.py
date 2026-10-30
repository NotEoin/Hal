import pytest

from retrylib.backoff import delay


def test_delay_doubles_until_the_cap():
    assert [delay(n, 1.0, 10.0) for n in range(5)] == [1.0, 2.0, 4.0, 8.0, 10.0]


def test_huge_attempt_returns_the_cap_without_overflow():
    assert delay(5000, 0.5, 30.0) == 30.0


def test_negative_attempt_is_rejected():
    with pytest.raises(ValueError):
        delay(-1)
