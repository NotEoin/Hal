"""Tests for `halagent.agentfile` (strict `agent.json`) and `map_brain`."""

import copy
import json

import pytest

from halagent.agentfile import AgentFileError, load_agent_file
from halagent.cli import EXIT_CONFIG, main
from halagent.settings import map_brain
from halkit.testing.model_server import Reply

BUDGET = {"max_tool_calls": 100, "max_model_requests": 200, "max_wall_s": 1800}
GOOD = {
    "v": 1, "session_id": "3f9a1c0b7e2d", "mode": "code", "toolset": "coding", "channel": "tui",
    "sandbox": "docker-runsc", "workspace": "/work", "host_root": "/home/u/proj", "scratch": "/scratch",
    "endpoint": {"url": "unix:///run/hal/model.sock", "api": "openai"},
    "broker": "", "broker_call_timeout_s": 3900, "model": "hal-code",
    "profile": {"family": "qwen35", "values": {"context": 32768, "temperature": 0.6}},
    "budget": {"interactive": BUDGET, "task": {**BUDGET, "max_tool_calls": 50, "max_wall_s": 900},
               "warn_calls": 10, "max_calls_per_response": 8, "max_consecutive_nudges": 3,
               "loop_repeat_limit": 3, "max_consecutive_tool_errors": 6},
    "context": {"window": 16384, "mask_at": 0.6, "mask_to": 0.4, "summarize_at": 0.85,
                "keep_recent": 3, "safety_tokens": 512},
    "tools": {"read_max_lines": 200, "read_max_chars": 12000, "grep_max_matches": 100,
              "grep_max_chars": 8000, "glob_max": 200, "run_timeout_s": 120, "run_max_timeout_s": 600,
              "run_head_chars": 2000, "run_tail_chars": 6000, "write_max_chars": 200000,
              "max_result_chars": 12000, "syntax_guard": True, "protect": [".git"]},
    "permissions": "auto",
    "instructions": {"agents_md": True, "max_chars": 16000, "user_file": ""},
    "skills": {"enabled": True, "dirs": [], "max_listed": 12, "max_chars": 3000},
    "mcp": {"servers": [], "project_servers": "box", "restart_limit": 1},
    "assistant": {"history_turns": 10, "max_tool_rounds": 5, "tools_per_turn": 8, "claims_check": True,
                  "log_conversation": True, "persona": "", "user_name": "", "timezone": "UTC"},
    "prompt": {"extra": "Be brief."},
}  # fmt: skip


def write(tmp_path, data):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(data))
    return path


def test_good_file_maps_to_settings(tmp_path):
    f = load_agent_file(write(tmp_path, GOOD))
    s, t = f.to_settings(), f.to_settings(task=True)
    assert (s.endpoint, s.model, s.context_window, s.max_turns) == (
        "unix:///run/hal/model.sock",
        "hal-code",
        16384,
        200,
    )
    assert (t.max_tool_calls, t.max_wall_s) == (50, 900)
    assert f.profile().family == "qwen35" and f.session_id == "3f9a1c0b7e2d"


def test_unknown_key_fails_with_its_path(tmp_path):
    bad = copy.deepcopy(GOOD)
    bad["tools"]["allow_host_run"] = True
    with pytest.raises(AgentFileError, match=r"tools\.allow_host_run: unknown key"):
        load_agent_file(write(tmp_path, bad))


def test_missing_key_fails(tmp_path):
    bad = copy.deepcopy(GOOD)
    del bad["budget"]["warn_calls"]
    with pytest.raises(AgentFileError, match=r"budget\.warn_calls: missing"):
        load_agent_file(write(tmp_path, bad))


@pytest.mark.parametrize(
    ("path", "value"),
    [(("v",), 2), (("mode",), "tui"), (("session_id",), "XYZ"), (("context", "window"), True),
     (("permissions",), "yolo"), (("profile", "values", "nope"), 1)],
)  # fmt: skip
def test_bad_values_fail(tmp_path, path, value):
    bad = copy.deepcopy(GOOD)
    node = bad
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(AgentFileError):
        load_agent_file(write(tmp_path, bad))


def test_cli_uses_the_run_file_endpoint_budget_and_prompt_extra(server, tmp_path):
    data = copy.deepcopy(GOOD)
    data["endpoint"]["url"] = server.url
    data["model"] = "fake-model"
    data["prompt"]["extra"] = "RUNFILE-EXTRA"
    server.script(Reply(tool_calls=[("finish", {"summary": "ok"})]))
    code = main(["--config", str(write(tmp_path, data)), "-p", "go", "-C", str(tmp_path), "--no-session"])
    system = server.requests[0]["body"]["messages"][0]["content"]
    assert code == 0 and "RUNFILE-EXTRA" in system


def test_cli_rejects_a_bad_run_file(tmp_path, capsys):
    bad = copy.deepcopy(GOOD)
    bad["v"] = 9
    assert main(["--config", str(write(tmp_path, bad)), "-p", "x"]) == EXIT_CONFIG
    assert "agent.json" in capsys.readouterr().err


def test_map_brain_moves_known_keys_and_drops_with_reasons():
    brain = {"model": "m", "num_ctx": 8192, "think": False, "user_name": "Eoin", "keep_alive": "5m",
             "ollama_url": "http://x", "deep_work": "claude", "deep_work_dir": "/d"}  # fmt: skip
    out = map_brain(brain)
    assert out.assistant == {"model": "m", "context": 8192, "think": False}
    assert out.hal == {"user_name": "Eoin"}
    assert set(out.dropped) == {"keep_alive", "ollama_url", "deep_work", "deep_work_dir"}
    assert len(out.warnings) == 2


def test_map_brain_keeps_an_existing_hal_user_name():
    out = map_brain({"user_name": "A"}, hal_user_name="B")
    assert out.hal == {} and "user_name" in out.dropped


def test_map_brain_rejects_an_unknown_key():
    with pytest.raises(ValueError, match=r"\[brain\]\.colour"):
        map_brain({"colour": "red"})
