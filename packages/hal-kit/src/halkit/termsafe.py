"""Terminal-safe text: escaping for approval prompts, stripping for model text in the TUI.

Two jobs with different rules (01-security, REQ-SEC-25 and REQ-SEC-72):

- `escape` is for approval displays, where the user must see exactly what a call holds.
  Nothing is removed: every character that could hide, reorder or restyle text becomes a
  visible `\\uXXXX` (tab as `\\t`, and newline as `\\n` in single-line views). An ANSI escape
  sequence shows as `\\u001b[...`, so its effect is gone but its bytes are still visible.
- `strip_terminal_controls` is for model text and tool output rendered in the TUI, where
  readability matters and nothing is approved: control sequences (CSI, OSC including OSC 8
  hyperlinks and OSC 52 clipboard writes, DCS, APC, PM, SOS) and stray control characters are
  removed. An unterminated string sequence is removed to the end of the text, because a
  terminal would swallow everything after it too.

Escaped classes: C0 controls, DEL, C1 controls, the bidirectional controls U+202A to U+202E
and U+2066 to U+2069, the zero-width characters U+200B to U+200F, U+2060 and U+FEFF, and the
soft hyphen U+00AD (REQ-SEC-25). U+061C (Arabic letter mark, also a bidi control) and the
line and paragraph separators U+2028 and U+2029 (which break a line on some terminals) are
escaped too; the requirement sets a minimum, and these hide text the same way.
"""

from __future__ import annotations

import re

_ESCAPED_CLASS = (
    "\\x00-\\x1f\\x7f-\\x9f\\u00ad\\u061c\\u200b-\\u200f\\u2028\\u2029\\u202a-\\u202e"
    "\\u2060\\u2066-\\u2069\\ufeff"
)
_UNSAFE_RE = re.compile(f"[{_ESCAPED_CLASS}]")

# ECMA-48: string sequences (OSC, DCS, SOS, PM, APC) in 7-bit (ESC + byte) and 8-bit (C1) form,
# ended by ST (ESC \ or 0x9c) or, as xterm accepts for OSC, BEL.
_STRING_SEQ_RE = re.compile(r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f])[\s\S]*?(?:\x07|\x1b\\|\x9c|\Z)")
_CSI_RE = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?")
_OTHER_ESC_RE = re.compile(r"\x1b[ -/]*[0-~]?")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _hex(match: re.Match[str]) -> str:
    return f"\\u{ord(match.group()):04x}"


def escape(text: str, *, multiline: bool = False) -> str:
    """`text` with every hiding or restyling character shown as visible `\\uXXXX` text.

    Tabs show as `\\t`. With `multiline` false (single-line views) newlines show as `\\n`;
    with it true they are kept.
    """
    text = text.replace("\t", "\\t")
    if multiline:
        return "\n".join(_UNSAFE_RE.sub(_hex, line) for line in text.split("\n"))
    return _UNSAFE_RE.sub(_hex, text.replace("\n", "\\n"))


def strip_terminal_controls(text: str) -> str:
    """`text` without terminal control sequences or control characters; newline and tab stay."""
    text = _STRING_SEQ_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    text = _OTHER_ESC_RE.sub("", text)
    return _CONTROL_RE.sub("", text)
