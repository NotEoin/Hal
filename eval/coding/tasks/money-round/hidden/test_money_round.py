from moneyfmt.fmt import format_cents


def test_half_cent_rounds_up():
    assert format_cents(10, 5) == 1
    assert format_cents(30, 5) == 2


def test_negative_half_rounds_away_from_zero():
    assert format_cents(-10, 5) == -1


def test_fractional_rate_is_exact():
    assert format_cents(1000, 7.5) == 75
    assert format_cents(1, 50) == 1


def test_whole_results_are_unchanged():
    assert format_cents(200, 10) == 20
