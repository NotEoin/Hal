from inilite.parser import parse


def test_inline_comment_after_space_is_dropped():
    assert parse("name = hal ; the agent\nmode = x # y") == {"name": "hal", "mode": "x"}


def test_hash_without_space_stays_in_value():
    assert parse("colour = a#b") == {"colour": "a#b"}


def test_whole_line_comments_and_blank_lines_are_skipped():
    assert parse("; c\n\n# d\na = 1") == {"a": "1"}
