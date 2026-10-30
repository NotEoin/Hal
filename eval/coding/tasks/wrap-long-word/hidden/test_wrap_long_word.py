from textwrapx.wrap import wrap


def test_short_words_wrap_greedily():
    assert wrap("aa bb cc dd", 5) == ["aa bb", "cc dd"]


def test_long_word_is_split_into_width_chunks():
    assert wrap("abcdefghij", 4) == ["abcd", "efgh", "ij"]


def test_words_after_a_long_word_start_a_new_line():
    assert wrap("hi abcdefg yo", 3) == ["hi", "abc", "def", "g", "yo"]
