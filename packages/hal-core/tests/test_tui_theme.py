"""Tests for the TUI look: glyph choice, NO_COLOR, themes and the markup-free console."""

import io

from halcore.tui import theme


class Utf8(io.StringIO):
    encoding = "utf-8"


class Latin(io.StringIO):
    encoding = "latin-1"


def test_utf8_terminal_gets_unicode_glyphs_by_default():
    assert theme.load_look(stream=Utf8(), environ={}).unicode


def test_non_utf8_terminal_falls_back_to_ascii():
    assert not theme.load_look(stream=Latin(), environ={}).unicode


def test_unicode_false_forces_ascii_even_on_utf8():
    assert not theme.load_look({"unicode": False}, stream=Utf8(), environ={}).unicode


def test_no_color_and_mono_theme_turn_colour_off():
    assert not theme.load_look(stream=Utf8(), environ={"NO_COLOR": "1"}).color
    assert not theme.load_look({"theme": "mono"}, stream=Utf8(), environ={}).color
    assert theme.load_look({"theme": "light"}, stream=Utf8(), environ={}).color


def test_every_unicode_glyph_has_an_ascii_twin_and_ascii_is_ascii():
    for name in theme.ASCII.__dataclass_fields__:
        value = getattr(theme.ASCII, name)
        text = value if isinstance(value, str) else "".join(value.values()) if isinstance(value, dict) else ""
        assert text.isascii(), name


def test_console_does_not_parse_markup_in_printed_strings():
    out = io.StringIO()
    console = theme.make_console(theme.load_look(stream=Utf8(), environ={}), out)
    console.print("[bold red]hi[/] :smile:")
    assert out.getvalue().strip() == "[bold red]hi[/] :smile:"


def test_ui_table_defaults_load():
    from halkit import config

    assert config.load(theme.UI_TABLE, environ={}, path=None)["theme"] == "dark"
