from calendar_utils.leap import is_leap


def test_century_rules():
    assert not is_leap(1900)
    assert is_leap(2000)
    assert is_leap(2024)
    assert not is_leap(2023)
