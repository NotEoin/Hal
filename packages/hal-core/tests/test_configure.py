"""Tests for `hal configure`: a first config that names no model, and the plan it prints (REQ-CLI-113)."""

from __future__ import annotations

import tomllib

import pytest

from halcore import configure, tune


def test_configure_names_no_model_and_prints_the_plan(tmp_path, monkeypatch, capsys):
    target = tmp_path / "config.toml"
    monkeypatch.setenv("HAL_CONFIG", str(target))
    planned = []
    monkeypatch.setattr(tune, "main", lambda argv: planned.append(argv) or tune.EXIT_NOTHING)
    assert configure.main([]) == 0
    data = tomllib.loads(target.read_text())
    assert "chat_model" not in data["hal"] and "profile" not in data["hal"]
    assert "context_cap" not in data["models"]
    assert planned == [[]] and "wrote" in capsys.readouterr().out


def test_the_old_profile_option_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "config.toml"))
    with pytest.raises(SystemExit):
        configure.main(["--profile", "max"])
