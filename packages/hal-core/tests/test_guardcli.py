"""Tests for `halcore.guardcli`: weight checks, the worker command, latency numbers, `status`."""

import json
import socket
import threading

import pytest
from halcore import guardcli
from halhub.guard import GuardClient
from halhub.policy.model import GuardSettings
from halkit.testing import TempHalHome


class FakeRelay:
    """A relay on a unix socket that answers every scan with `reply` (an `error` or a `score`)."""

    def __init__(self, path, **reply):
        self.reply, self.requests = reply, []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                request = json.loads(conn.makefile("rb").readline())
                self.requests.append(request)
                body = {"v": 1, "id": request["id"], "windows": 1, "ms": 7, "model": "fake", **self.reply}
                if "score" in body:
                    body["label"] = "benign"
                conn.sendall(json.dumps(body).encode() + b"\n")

    def close(self):
        self.listener.close()


@pytest.fixture
def home():
    with TempHalHome() as h:
        h.config.write_text("")
        yield h


def weights(path, *extra):
    path.mkdir(parents=True, exist_ok=True)
    for name in (*guardcli.REQUIRED_FILES, *extra):
        (path / name).write_bytes(b"synthetic")
    return path


def test_a_complete_weights_directory_has_no_problem(tmp_path):
    assert guardcli.weights_problem(weights(tmp_path / "m")) is None


def test_a_missing_file_is_named(tmp_path):
    assert "tokenizer.json" in guardcli.weights_problem(_without(weights(tmp_path / "m"), "tokenizer.json"))


def _without(path, name):
    (path / name).unlink()
    return path


def test_a_directory_with_a_pickle_file_is_refused(tmp_path):
    problem = guardcli.weights_problem(weights(tmp_path / "m", "pytorch_model.bin"))
    assert "pickle" in problem


def test_a_missing_directory_is_refused(tmp_path):
    assert "not a directory" in guardcli.weights_problem(tmp_path / "none")


def test_worker_command_has_no_network_a_read_only_mount_and_no_privilege(tmp_path):
    argv = guardcli.worker_argv(tmp_path, "runsc", uid=1000, gid=1000)
    assert argv[argv.index("--network") + 1] == "none" and "--read-only" in argv
    assert "--runtime=runsc" in argv and "1000:1000" in argv
    assert argv[argv.index("--mount") + 1].endswith("dst=/model,readonly")
    assert "--privileged" not in argv and argv[argv.index("--cap-drop") + 1] == "ALL"


def test_percentiles_give_the_median_and_the_slow_tail():
    assert guardcli.percentiles([10, 20, 30, 40, 100]) == {"p50": 30, "p95": 100}


def test_bench_times_a_window_and_a_result(home):
    path = home.runtime / "guard.sock"
    fake = FakeRelay(path, score=0.01)
    try:
        data = guardcli.bench(GuardClient(GuardSettings(), path=path), rounds=3)
    finally:
        fake.close()
    assert set(data) == {"at", "window", "result"} and data["at"].endswith("Z")
    assert len(fake.requests) == 6


def test_bench_fails_loudly_when_a_scan_errors(home):
    path = home.runtime / "guard.sock"
    fake = FakeRelay(path, error="no weights")
    try:
        with pytest.raises(RuntimeError, match="no weights"):
            guardcli.bench(GuardClient(GuardSettings(), path=path), rounds=1)
    finally:
        fake.close()


def test_status_says_what_is_missing_and_exits_0(home, capsys):
    assert guardcli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "enabled: no" in out and "not installed" in out and "NOT reachable" in out


def test_status_shows_recorded_latency(home, capsys):
    target = home.state / guardcli.BENCH_FILE
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"at": "2026-10-30T00:00:00Z", "window": {"p50": 5, "p95": 9}, "result": {"p50": 40, "p95": 80}}))
    guardcli.main(["status"])
    assert "latency per window: p50 5 ms, p95 9 ms" in capsys.readouterr().out


def test_install_without_a_terminal_changes_nothing(home, tmp_path, monkeypatch, capsys):
    image = tmp_path / "image"
    image.mkdir()
    (image / "Dockerfile").write_text("FROM scratch\n")
    model = weights(tmp_path / "m")
    assert guardcli.main(["install", "--model-dir", str(model), "--image-dir", str(image)]) == 1
    assert "needs a terminal" in capsys.readouterr().err
    assert not (home.state / guardcli.WORKER_FILE).exists()
