import pytest

from pathsafe.join import safe_join


def test_nested_path_is_joined():
    assert safe_join("/srv/data", "a", "b.txt") == "/srv/data/a/b.txt"


def test_dotdot_escape_is_refused():
    with pytest.raises(ValueError):
        safe_join("/srv/data", "..", "etc", "passwd")


def test_sibling_with_the_same_prefix_is_refused():
    with pytest.raises(ValueError):
        safe_join("/srv/data", "../data-evil/x")


def test_absolute_part_outside_base_is_refused():
    with pytest.raises(ValueError):
        safe_join("/srv/data", "/etc/passwd")


def test_base_itself_is_allowed():
    assert safe_join("/srv/data", "a", "..") == "/srv/data"
