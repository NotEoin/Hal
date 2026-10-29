"""Tests for `halkit.termsafe`: approval escaping (REQ-SEC-25) and TUI stripping (REQ-SEC-72).

Non-ASCII test characters are built with `chr` so this file stays ASCII.
"""

from __future__ import annotations

import pytest

from halkit.termsafe import escape, strip_terminal_controls

ESC = chr(0x1B)
BEL = chr(0x07)
ST = ESC + "\\"

ESCAPED_CODE_POINTS = [
    *range(0x00, 0x09),
    *range(0x0B, 0x20),
    0x7F,
    *range(0x80, 0xA0),
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
    *range(0x200B, 0x2010),
    0x2060,
    0xFEFF,
    0x00AD,
]


@pytest.mark.parametrize("code", ESCAPED_CODE_POINTS, ids=lambda c: f"U+{c:04X}")
def test_every_hiding_character_is_shown_as_visible_hex(code):
    assert escape(f"a{chr(code)}b") == f"a\\u{code:04x}b"


def test_tab_shows_as_backslash_t_and_newline_depends_on_the_view():
    assert escape("a\tb\nc") == "a\\tb\\nc"
    assert escape("a\tb\nc", multiline=True) == "a\\tb\nc"


def test_ansi_sequence_keeps_its_bytes_visible_but_loses_its_effect():
    out = escape(f"{ESC}[2Kann@example.org")
    assert out == "\\u001b[2Kann@example.org" and ESC not in out


def test_bidi_override_in_an_address_is_visible_in_the_prompt():
    spoof = "ann@example.org" + chr(0x202E) + "gro.live"
    assert escape(spoof) == "ann@example.org\\u202egro.live"


def test_printable_text_including_other_scripts_is_unchanged():
    text = "Notes for " + chr(0x00C9) + "ire " + chr(0x4E2D)
    assert escape(text) == text


def test_osc_52_clipboard_write_is_removed_from_model_text():
    assert strip_terminal_controls(f"hi{ESC}]52;c;ZXZpbA=={BEL} there") == "hi there"


def test_osc_8_hyperlink_markup_is_removed_and_the_label_kept():
    text = f"{ESC}]8;;https://evil.example/{ST}click{ESC}]8;;{ST}"
    assert strip_terminal_controls(text) == "click"


@pytest.mark.parametrize(
    "seq",
    [
        f"{ESC}[31m",
        f"{ESC}[2J",
        f"{ESC}[?1049h",
        chr(0x9B) + "1;2H",
        f"{ESC}P1$r{ST}",
        f"{ESC}_payload{ST}",
        f"{ESC}^pm{ST}",
        chr(0x9D) + "0;title" + chr(0x9C),
        f"{ESC}(B",
        f"{ESC}7",
    ],
    ids=["sgr", "clear", "altscreen", "c1-csi", "dcs", "apc", "pm", "c1-osc", "charset", "save"],
)
def test_terminal_control_sequences_are_removed(seq):
    assert strip_terminal_controls(f"a{seq}b") == "ab"


def test_unterminated_string_sequence_is_removed_to_the_end():
    assert strip_terminal_controls(f"safe{ESC}]52;c;AAAA rest") == "safe"


def test_stray_controls_go_but_newline_and_tab_stay():
    assert strip_terminal_controls("a\rb\x00c\x7fd\n\te") == "abcd\n\te"
