"""Logging setup for Hal entry points, plain or JSON, always to stderr.

Only entry points (`__main__.py`, `cli.py`) call `setup`. Library modules use
`logging.getLogger(__name__)` and never configure handlers or print. Output goes to stderr
because systemd captures it for units and stdout is reserved for program output.
Timestamps are UTC with a `Z` (REQ-SYS-14).
"""

from __future__ import annotations

import json as jsonlib
import logging
import os
import sys
from datetime import UTC, datetime

from .timeutil import iso_z

DEFAULT_LEVEL = "INFO"
LEVEL_ENV = "HAL_LOG_LEVEL"
_HANDLER_MARK = "_halkit_handler"  # lets a second `setup` replace its own handler only


class _PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = iso_z(datetime.fromtimestamp(record.created, UTC))
        text = f"{stamp} {record.levelname} {record.name}: {record.getMessage()}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        row = {
            "ts": iso_z(datetime.fromtimestamp(record.created, UTC), ms=True),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            row["exc"] = self.formatException(record.exc_info)
        return jsonlib.dumps(row, ensure_ascii=True)


def _resolve_level(level: str | None) -> int:
    name = (level or os.environ.get(LEVEL_ENV) or DEFAULT_LEVEL).strip().upper()
    number = logging.getLevelNamesMapping().get(name)
    if not isinstance(number, int):
        raise ValueError(f"unknown log level {name!r}; use DEBUG, INFO, WARNING, ERROR or CRITICAL")
    return number


def setup(name: str, *, level: str | None = None, json: bool = False) -> logging.Logger:
    """Configures logger `name` (and so its children) to write to stderr and returns it.

    The level is `level`, else `HAL_LOG_LEVEL`, else `INFO`; raises `ValueError` for an
    unknown name. Calling it again replaces the handler it installed before, so output is
    never duplicated.
    """
    logger = logging.getLogger(name)
    logger.setLevel(_resolve_level(level))
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if json else _PlainFormatter())
    setattr(handler, _HANDLER_MARK, True)
    logger.addHandler(handler)
    logger.propagate = False
    return logger
