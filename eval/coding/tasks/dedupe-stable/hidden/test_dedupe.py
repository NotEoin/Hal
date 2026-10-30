from listtools.dedupe import dedupe


def test_first_occurrence_order_is_kept():
    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]
    assert dedupe([]) == []
    assert dedupe(["b", "a", "b"]) == ["b", "a"]
