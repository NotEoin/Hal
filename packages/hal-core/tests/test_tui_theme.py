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


def test_look_names_its_theme_and_no_color_means_mono():
    for name in theme.THEMES:
        assert theme.load_look({"theme": name}, environ={}).theme == name
    assert theme.load_look({"theme": "light"}, environ={"NO_COLOR": "1"}).theme == "mono"
    assert theme.load_look({"theme": "nonsense"}, environ={}).theme == "dark"


def test_every_theme_has_a_textual_theme_with_the_hal_accent():
    built = {t.name: t for t in theme.textual_themes()}
    assert set(built) == {"hal-dark", "hal-light", "hal-mono"}
    assert built["hal-dark"].primary == built["hal-light"].primary == theme.ACCENT
    assert built["hal-mono"].ansi and not built["hal-light"].dark
    for name in theme.THEMES:
        assert theme.load_look({"theme": name}, environ={}).textual_theme in built


def test_importing_the_theme_does_not_load_textual():
    import subprocess
    import sys

    code = "import sys, halcore.tui.theme, halcore.tui.status; print('textual' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True)
    assert done.stdout.strip() == "False"
