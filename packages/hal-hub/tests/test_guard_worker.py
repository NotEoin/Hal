"""Tests for the guard image's `worker.py`: protocol, windowing and model checks, without torch.

The worker is not part of an importable package (01-security REQ-SEC-36), so it is loaded by
path. A fake scorer stands in for the model; `tests/e2e/test_guard_box.py` runs the real image.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

WORKER = Path(__file__).resolve().parents[1] / "guard-image" / "worker.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hal_guard_worker", WORKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = _load()
OPTIONS = {"model": "fake-guard", "threshold": 0.5, "load_error": ""}


def request(text: str = "hello", **extra: object) -> bytes:
    return json.dumps({"v": 1, "id": "g_1", "text": text, "source_tool": "mail_read", **extra}).encode()


def test_short_text_is_one_window():
    assert worker.windows(list(range(10)), 512, 64) == [list(range(10))]


def test_long_text_windows_overlap_and_cover_every_token():
    ids = list(range(1200))
    parts = worker.windows(ids, 512, 64)
    assert [len(p) for p in parts] == [512, 512, 304]
    assert parts[1][:64] == parts[0][-64:] and parts[-1][-1] == 1199


def test_window_must_be_longer_than_the_overlap():
    with pytest.raises(ValueError, match="overlap"):
        worker.windows([1, 2, 3], 64, 64)


def test_score_at_the_threshold_is_an_injection():
    reply = worker.handle(request(), lambda text: (0.5, 2), **OPTIONS)
    assert reply == {
        "v": 1,
        "id": "g_1",
        "label": "injection",
        "score": 0.5,
        "windows": 2,
        "ms": reply["ms"],
        "model": "fake-guard",
    }


def test_score_below_the_threshold_is_benign():
    assert worker.handle(request(), lambda text: (0.1, 1), **OPTIONS)["label"] == "benign"


def test_scorer_failure_is_an_error_reply():
    def broken(text):
        raise MemoryError("out of memory")

    reply = worker.handle(request(), broken, **OPTIONS)
    assert reply["id"] == "g_1" and "out of memory" in reply["error"] and "label" not in reply


def test_model_that_did_not_load_answers_every_request_with_an_error():
    reply = worker.handle(request(), None, **{**OPTIONS, "load_error": "no weights"})
    assert reply["error"] == "RuntimeError: model unavailable: no weights"


@pytest.mark.parametrize(
    "line",
    [
        b"not json",
        b"[1]",
        json.dumps({"v": 2, "id": "g_1", "text": "x"}).encode(),
        json.dumps({"v": 1, "id": 1, "text": "x"}).encode(),
    ],
    ids=["not-json", "not-object", "version", "id-type"],
)
def test_malformed_request_is_an_error_reply(line):
    assert "error" in worker.handle(line, lambda text: (0.0, 1), **OPTIONS)


def test_oversized_request_is_refused_before_parsing():
    line = b" " * (worker.MAX_LINE_BYTES + 1)
    assert "over" in worker.handle(line, lambda text: (0.0, 1), **OPTIONS)["error"]


def test_serve_answers_each_line_in_order_and_skips_blank_lines():
    out = io.StringIO()
    worker.serve([request("a", id="g_1"), b"\n", request("b", id="g_2")], out, lambda t: (0.9, 1), **OPTIONS)
    assert [json.loads(line)["id"] for line in out.getvalue().splitlines()] == ["g_1", "g_2"]


def test_model_directory_with_a_pickle_file_is_refused(tmp_path):
    for name in worker.REQUIRED_FILES:
        (tmp_path / name).write_text("{}")
    (tmp_path / "pytorch_model.bin").write_bytes(b"\x80\x04")
    with pytest.raises(ValueError, match="pickle"):
        worker.check_model_dir(tmp_path)


def test_model_directory_missing_the_safetensors_file_is_refused(tmp_path):
    (tmp_path / "tokenizer.json").write_text("{}")
    with pytest.raises(ValueError, match="model.safetensors"):
        worker.check_model_dir(tmp_path)


def test_malicious_class_index_comes_from_the_label_names():
    assert worker.malicious_index({"BENIGN": 0, "MALICIOUS": 1}) == 1
    assert worker.malicious_index({"injection": 0, "benign": 1}) == 0
    assert worker.malicious_index({"LABEL_0": 0, "LABEL_1": 1}) == 1


def test_main_without_a_model_still_serves_error_replies(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(worker, "_stdin_lines", lambda: iter([request()]))
    assert worker.main(["--model-dir", str(tmp_path / "absent")]) == 0
    out, err = capsys.readouterr()
    assert "does not exist" in json.loads(out)["error"] and "does not exist" in err


def test_worker_imports_only_the_stdlib_at_module_level():
    tree = ast.parse(WORKER.read_text())
    top = {
        alias.name.split(".")[0] for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    top |= {
        node.module.split(".")[0] for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
    }
    assert top <= set(sys.stdlib_module_names) | {"__future__"}
