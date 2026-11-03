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


def test_every_glyph_has_an_ascii_fallback():
    assert set(tokens.UNICODE_GLYPHS) == set(tokens.ASCII_GLYPHS)
    assert set(tokens.UNICODE_EYE) == set(tokens.ASCII_EYE) and set(tokens.UNICODE_PLAN) == set(tokens.ASCII_PLAN)
    assert len(tokens.UNICODE_GLYPHS["spark"]) == len(tokens.ASCII_GLYPHS["spark"]) == tokens.SPARK_LEVELS


def test_ascii_glyph_set_is_pure_ascii():
    values = [*tokens.ASCII_GLYPHS.values(), *tokens.ASCII_SPINNER_FRAMES, *tokens.ASCII_EYE.values(), *tokens.ASCII_PLAN.values()]
    assert all(v.isascii() for v in values)


def test_unicode_glyphs_are_single_cell():
    from rich.cells import cell_len

    singles = [v for k, v in tokens.UNICODE_GLYPHS.items() if k != "spark"]
    for glyph in [*singles, *tokens.UNICODE_GLYPHS["spark"], *tokens.UNICODE_SPINNER_FRAMES]:
        assert cell_len(glyph) == 1, repr(glyph)


def test_bar_fills_in_proportion_and_clamps():
    assert tokens.bar(0.41, 10, "#", "-") == "####------"
    assert tokens.bar(2.0, 4, "#", "-") == "####"
    assert tokens.bar(-1.0, 4, "#", "-") == "----"


def test_spark_scales_to_the_window_max():
    assert tokens.spark([0, 5, 10], 3, " .:-=+*#") == " =#"
    assert tokens.spark([1, 2, 3, 4, 8], 2, " .:-=+*#") == "=#"


def test_spark_of_all_zero_is_flat():
    assert tokens.spark([0, 0, 0], 3, " .:-=+*#") == "   "
    assert tokens.spark([], 3, " .:-=+*#") == ""


def test_spinner_frame_wraps_around():
    assert tokens.spinner_frame(("a", "b"), 3) == "b"


@pytest.mark.parametrize(("requested", "screen", "width"), [(34, 120, 34), (10, 120, 26), (90, 200, 60), (55, 80, 40), (34, 40, 20)])
def test_side_width_is_clamped_to_its_limits_and_half_the_screen(requested, screen, width):
    assert tokens.side_width(requested, screen) == width
