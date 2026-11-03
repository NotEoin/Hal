"""Tests of the design tokens: colour contrast, glyph sets and the drawing helpers."""

from __future__ import annotations

import pytest

from halcore.tui import tokens


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_text_roles_meet_wcag_contrast_in_both_themes(theme):
    roles = tokens.ROLES[theme]
    for role in tokens.TEXT_ROLES:
        assert tokens.contrast(roles[role], roles["bg"]) >= tokens.WCAG_TEXT, (theme, role)
    for role in tokens.HINT_ROLES:
        assert tokens.contrast(roles[role], roles["bg"]) >= tokens.WCAG_HINT, (theme, role)


def test_contrast_of_black_on_white_is_twenty_one():
    assert tokens.contrast("#000000", "#ffffff") == pytest.approx(21.0)


def test_every_role_is_a_textual_css_variable():
    variables = tokens.css_variables("dark")
    assert variables["hal-faint"] == tokens.ROLES["dark"]["faint"]
    assert variables["hal-accent-dim"] == tokens.ROLES["dark"]["accent_dim"]
    assert set(tokens.ROLES["dark"]) == set(tokens.ROLES["light"])
