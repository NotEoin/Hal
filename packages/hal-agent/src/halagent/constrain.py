"""Constrained tool calls for the text fallback: a GBNF grammar or a JSON schema for the reply.

With `tool_format = "prompt"` the model writes `<tool_call>` blocks as plain text, and a small
model sometimes breaks the JSON. llama.cpp can force the shape while it samples: `grammar` takes
GBNF and `response_format` takes a JSON schema. Both are built only from the names of the offered
tools, in sorted order, so the request stays byte-stable between turns (the prompt cache needs
that). The profile key `tool_constraint` picks the mode: `none` (default), `grammar` or
`json_schema`. Servers that do not know the keys ignore them (Ollama), so the fallback parsers
stay in place as the safety net.
"""

from __future__ import annotations

import json

MODES = ("none", "grammar", "json_schema")
ANSWER_KEY = "answer"

# The JSON value grammar is the one llama.cpp ships as grammars/json.gbnf, trimmed to objects.
_JSON_RULES = r"""
object ::= "{" ws ( string ":" ws value ( "," ws string ":" ws value )* )? "}" ws
value ::= object | array | string | number | ("true" | "false" | "null") ws
array ::= "[" ws ( value ( "," ws value )* )? "]" ws
string ::= "\"" ( [^"\\\x7F\x00-\x1F] | "\\" ( ["\\bfnrt] | "u" [0-9a-fA-F]{4} ) )* "\"" ws
number ::= ("-"? ([0-9] | [1-9] [0-9]{0,15})) ("." [0-9]+)? ([eE] [-+]? [0-9]+)? ws
ws ::= | " " | "\n" [ \t]{0,20}
"""


def tool_grammar(names: list[str]) -> str:
    """GBNF for a reply that is either one `<tool_call>` block of an offered tool or plain text."""
    if not names:
        raise ValueError("a tool grammar needs at least one tool name")
    alternatives = " | ".join(json.dumps(json.dumps(n)) for n in sorted(names))
    return (
        "root ::= call | answer\n"
        'call ::= "<tool_call>\\n{\\"name\\": " name ", \\"arguments\\": " object "}\\n</tool_call>"\n'
        f"name ::= {alternatives}\n"
        "answer ::= [^<] [\\x01-\\U0010FFFF]*\n" + _JSON_RULES
    )


def tool_schema(names: list[str]) -> dict:
    """JSON schema for a reply that is a tool call or `{"answer": text}`."""
    if not names:
        raise ValueError("a tool schema needs at least one tool name")
    call = {
        "type": "object",
        "properties": {"name": {"enum": sorted(names)}, "arguments": {"type": "object"}},
        "required": ["name", "arguments"],
    }
    answer = {"type": "object", "properties": {ANSWER_KEY: {"type": "string"}}, "required": [ANSWER_KEY]}
    return {"anyOf": [call, answer]}


def request_keys(mode: str, names: list[str]) -> dict:
    """The extra request-body keys for `mode`; empty for `none`. Raises `ValueError` on a bad mode."""
    if mode not in MODES:
        raise ValueError(f"tool_constraint must be one of {', '.join(MODES)}, not {mode!r}")
    if mode == "grammar":
        return {"grammar": tool_grammar(names)}
    if mode == "json_schema":
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "tool_reply", "schema": tool_schema(names)},
            }
        }
    return {}


def unwrap_answer(text: str) -> str:
    """The text of an `{"answer": ...}` reply, or `text` unchanged when it is not one."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict) and set(data) == {ANSWER_KEY} and isinstance(data[ANSWER_KEY], str):
        return data[ANSWER_KEY]
    return text
