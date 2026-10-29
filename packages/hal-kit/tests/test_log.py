"""Tests for `halkit.log`."""

import json
import logging
import re

import pytest

from halkit import log


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("HAL_LOG_LEVEL", raising=False)
    yield
    for name in ("halkit_test", "halkit_test.child"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def test_setup_writes_plain_lines_with_a_utc_z_timestamp_to_stderr(capsys):
    log.setup("halkit_test").info("hello %s", "world")
    err = capsys.readouterr().err
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ INFO halkit_test: hello world\n", err)


def test_setup_json_writes_one_object_per_line(capsys):
    log.setup("halkit_test", json=True).warning("careful")
    row = json.loads(capsys.readouterr().err)
    assert (row["level"], row["logger"], row["msg"]) == ("WARNING", "halkit_test", "careful")
    assert row["ts"].endswith("Z")


def test_default_level_is_info(capsys):
    logger = log.setup("halkit_test")
    logger.debug("hidden")
    logger.info("shown")
    assert "hidden" not in capsys.readouterr().err


def test_hal_log_level_sets_the_level(monkeypatch, capsys):
    monkeypatch.setenv("HAL_LOG_LEVEL", "debug")
    log.setup("halkit_test").debug("now visible")
    assert "now visible" in capsys.readouterr().err


def test_argument_level_beats_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("HAL_LOG_LEVEL", "DEBUG")
    log.setup("halkit_test", level="ERROR").warning("hidden")
    assert capsys.readouterr().err == ""


def test_unknown_level_raises_a_value_error():
    with pytest.raises(ValueError, match="unknown log level 'LOUD'"):
        log.setup("halkit_test", level="loud")


def test_calling_setup_twice_does_not_duplicate_output(capsys):
    log.setup("halkit_test")
    log.setup("halkit_test").info("once")
    assert capsys.readouterr().err.count("once") == 1


def test_child_loggers_inherit_the_handler(capsys):
    log.setup("halkit_test")
    logging.getLogger("halkit_test.child").info("from child")
    assert "halkit_test.child: from child" in capsys.readouterr().err


def test_exception_text_is_included(capsys):
    logger = log.setup("halkit_test")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("failed")
    assert "RuntimeError: boom" in capsys.readouterr().err
