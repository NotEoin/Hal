"""Tests for `halhub.runner`: real worker processes on the synthetic `probe` pack.

REQ-HUB-24 to REQ-HUB-29, REQ-HUB-32 and REQ-HUB-33. Timeouts are 0.2 to 1 s so the file stays
fast; every runner is closed after its test.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest

from halhub.config import HubConfig, load_runner_config
from halhub.manifest import ModuleSpec, load_manifest
from halhub.runner import (
    MAX_MESSAGE_BYTES,
    NestedCall,
    Runner,
    RunnerConfig,
    RunOutcome,
    cut_output,
    worker_environment,
)
from halkit.testing import FakeClock

# bwrap off: the hermetic tests must not depend on user namespaces; test_runner_sandbox.py covers it
FAST = RunnerConfig(kill_grace=0.3, queue_timeout=0.3, bwrap="off")


class CountingPopen:
    """`subprocess.Popen` that records every argv it starts."""

    def __init__(self) -> None:
        self.started: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.started.append(list(argv))
        return subprocess.Popen(argv, **kwargs)


@pytest.fixture
def probe(fixtures) -> ModuleSpec:
    return load_manifest(fixtures / "probe" / "hal-module.toml", source="path")


@pytest.fixture
def make_runner():
    runners = []

    def make(config: RunnerConfig = FAST, **kwargs) -> Runner:
        runner = Runner(config, **kwargs)
        runners.append(runner)
        return runner

    yield make
    for runner in runners:
        runner.close()


def tool(module: ModuleSpec, name: str):
    return next(t for t in module.tools if t.name == name)


def run(
    runner: Runner, module: ModuleSpec, name: str, args=None, timeout: float = 5.0, **kwargs
) -> RunOutcome:
    return runner.run(module, tool(module, name), args or {}, timeout=timeout, **kwargs)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def jsonl_module(tmp_path: Path, argv: list[str], *, startup_timeout: float = 0.3) -> ModuleSpec:
    path = tmp_path / "silent" / "hal-module.toml"
    path.parent.mkdir()
    path.write_text(
        textwrap.dedent(
            f"""
            schema = 2
            [module]
            name = "silent"
            summary = "an exec worker that never says ready"
            exec = {argv!r}
            startup_timeout = {startup_timeout}
            [[tools]]
            name = "silent_go"
            description = "d"
            tier = "read"
            """
        ).replace("'", '"')
    )
    return load_manifest(path, source="path")


# ---- results and errors -----------------------------------------------------------------------


def test_warm_worker_serves_consecutive_calls(make_runner, probe):
    runner = make_runner()
    first = run(runner, probe, "probe_shout", {"text": "a"})
    second = run(runner, probe, "probe_shout", {"text": "b"})
    assert (first.ok, first.result, second.result) == (True, "A", "B")
    assert first.worker_pid == second.worker_pid and first.seconds >= 0


def test_print_in_tool_does_not_corrupt_protocol(make_runner, probe):
    outcome = run(make_runner(), probe, "probe_shout", {"text": "hi"})
    assert outcome.ok and outcome.result == "HI"


@pytest.mark.parametrize(
    ("name", "args", "error_type", "message"),
    [
        ("probe_boom", {}, "tool_error", "ValueError: no event matches 'dentist'"),
        ("probe_fail", {}, "tool_error", "quota reached"),
        ("probe_shout", {"text": "a", "loud": True}, "manifest_mismatch", None),
        ("probe_nan", {}, "tool_error", None),
    ],
)
def test_worker_errors_map_to_caller_error_types(make_runner, probe, name, args, error_type, message):
    outcome = run(make_runner(), probe, name, args)
    assert not outcome.ok and outcome.error_type == error_type
    if message is not None:
        assert outcome.message == message
    assert "Traceback" not in (outcome.message or "")


def test_tool_error_keeps_the_worker(make_runner, probe):
    runner = make_runner()
    failed = run(runner, probe, "probe_boom")
    ok = run(runner, probe, "probe_shout", {"text": "x"})
    assert failed.worker_pid == ok.worker_pid


def test_cut_output_keeps_head_and_tail_with_a_marker():
    text = "a" * 50 + "b" * 50
    cut = cut_output(text, 20)
    assert cut.startswith("a" * 10) and cut.endswith("b" * 10) and "[... 80 characters cut ...]" in cut
    assert cut_output("short", 20) == "short"


def test_protocol_line_over_eight_mebibytes_is_output_too_large(make_runner, probe):
    runner = make_runner()
    outcome = run(runner, probe, "probe_big", {"size": MAX_MESSAGE_BYTES + 10})
    assert outcome.error_type == "output_too_large"
    assert run(runner, probe, "probe_shout", {"text": "x"}).worker_pid != outcome.worker_pid


# ---- process control ---------------------------------------------------------------------------


def test_timeout_kills_tool_and_its_children(make_runner, probe, tmp_path):
    pidfile = tmp_path / "child.pid"
    started = time.monotonic()
    outcome = run(make_runner(), probe, "probe_sleep", {"seconds": 30, "pidfile": str(pidfile)}, timeout=0.5)
    elapsed = time.monotonic() - started
    assert outcome.error_type == "timeout" and "0.5" in outcome.message
    assert elapsed < 0.5 + FAST.kill_grace + 1
    assert not pid_alive(int(pidfile.read_text()))
    assert not pid_alive(outcome.worker_pid)


def test_cancel_kills_running_worker(make_runner, probe):
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    outcome = run(make_runner(), probe, "probe_sleep", {"seconds": 30}, timeout=10, cancel=cancel)
    assert outcome.error_type == "cancelled"
    assert time.monotonic() - started < 0.2 + 1
    assert not pid_alive(outcome.worker_pid)


def test_crash_is_reported_and_not_retried(make_runner, probe):
    popen = CountingPopen()
    runner = make_runner(popen=popen)
    outcome = run(runner, probe, "probe_crash", {"code": 3})
    assert outcome.error_type == "worker_crashed"
    assert "status 3" in outcome.message and "crashing on purpose" in outcome.message
    assert len(popen.started) == 1
    after = run(runner, probe, "probe_shout", {"text": "x"})
    assert after.ok and after.worker_pid != outcome.worker_pid and len(popen.started) == 2


def test_missing_ready_backs_off(make_runner, tmp_path):
    module = jsonl_module(tmp_path, ["sleep", "30"])
    clock = FakeClock()
    popen = CountingPopen()
    runner = make_runner(popen=popen, clock=clock.monotonic)
    first = run(runner, module, "silent_go")
    assert first.error_type == "worker_failed" and "no ready" in first.message
    second = run(runner, module, "silent_go")
    assert second.error_type == "worker_failed" and "recently" in second.message
    assert len(popen.started) == 1
    clock.advance(31)
    assert run(runner, module, "silent_go").error_type == "worker_failed"
    assert len(popen.started) == 2


def test_worker_that_exits_before_ready_is_worker_failed(make_runner, fixtures):
    module = load_manifest(fixtures / "broken" / "hal-module.toml", source="path")
    outcome = run(make_runner(), module, "broken_any")
    assert outcome.error_type == "worker_failed" and "halbroken cannot be imported" in outcome.message


def test_busy_when_every_worker_stays_busy(make_runner, probe):
    one = dataclasses.replace(probe, max_workers=1)
    runner = make_runner()
    background = threading.Thread(
        target=run, args=(runner, one, "probe_sleep", {"seconds": 2}), kwargs={"timeout": 1}
    )
    background.start()
    time.sleep(0.5)  # let the first call take the only worker
    outcome = run(runner, one, "probe_shout", {"text": "x"})
    background.join()
    assert outcome.error_type == "busy"


def test_worker_is_recycled_after_max_calls(make_runner, probe):
    runner = make_runner(dataclasses.replace(FAST, max_calls_per_worker=2))
    pids = [run(runner, probe, "probe_shout", {"text": "x"}).worker_pid for _ in range(3)]
    assert pids[0] == pids[1] != pids[2]


def test_idle_worker_is_reaped(make_runner, probe):
    clock = FakeClock()
    runner = make_runner(dataclasses.replace(FAST, idle_timeout=600), clock=clock.monotonic)
    first = run(runner, probe, "probe_shout", {"text": "x"})
    clock.advance(601)
    runner.reap_idle()
    assert not pid_alive(first.worker_pid) or _zombie(first.worker_pid)
    assert run(runner, probe, "probe_shout", {"text": "x"}).worker_pid != first.worker_pid


def _zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
    except FileNotFoundError:
        return True


def test_close_shuts_every_worker_down(make_runner, probe):
    runner = make_runner()
    pid = run(runner, probe, "probe_shout", {"text": "x"}).worker_pid
    runner.close()
    assert not pid_alive(pid) or _zombie(pid)
    assert run(runner, probe, "probe_shout", {"text": "x"}).error_type == "worker_failed"


# ---- nested calls ------------------------------------------------------------------------------


def test_nested_call_goes_to_the_callback(make_runner, probe):
    seen: list[NestedCall] = []

    def on_tool_call(request: NestedCall) -> RunOutcome:
        seen.append(request)
        return RunOutcome(True, result=f"echo {request.args['text']}")

    outcome = run(make_runner(), probe, "probe_nested", {"tool": "demo_echo"}, on_tool_call=on_tool_call)
    assert outcome.result == "echo hi"
    assert seen[0].tool == "demo_echo" and seen[0].parent_tool.name == "probe_nested"


def test_nested_call_without_a_callback_is_refused(make_runner, probe):
    outcome = run(make_runner(), probe, "probe_nested", {"tool": "demo_echo"})
    assert outcome.error_type == "tool_error" and "policy_denied" in outcome.message


# ---- environment and working directory --------------------------------------------------------


def test_worker_env_excludes_hub_secrets(make_runner, probe, monkeypatch):
    monkeypatch.setenv("HAL_GATEWAY_TOKEN", "gw-token-not-for-tools")
    monkeypatch.setenv("PROBE_ALLOWED", "declared")
    monkeypatch.setenv("LC_TIME", "C")
    environ = run(make_runner(), probe, "probe_env").result["environ"]
    assert "HAL_GATEWAY_TOKEN" not in environ
    assert environ["PROBE_ALLOWED"] == "declared" and environ["LC_TIME"] == "C"
    assert environ["HAL_HUB_WORKER"] == "1" and environ["HAL_MODULE"] == "probe"
    assert environ["PYTHONPATH"] == str(probe.root)


def test_environment_is_built_not_inherited(probe):
    env = worker_environment(
        probe, {"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "x", "XDG_RUNTIME_DIR": "/run/u"}
    )
    assert env["PATH"] == "/bin" and env["XDG_RUNTIME_DIR"] == "/run/u" and "AWS_SECRET_ACCESS_KEY" not in env


def test_v2_worker_runs_in_the_module_state_directory(make_runner, probe, hal_home):
    cwd = run(make_runner(), probe, "probe_cwd").result
    assert cwd == str(hal_home.state / "modules" / "probe")
    assert (Path(cwd).stat().st_mode & 0o777) == 0o700


# ---- configuration -----------------------------------------------------------------------------


def test_runner_config_reads_the_hub_runner_table(hal_home):
    hal_home.config.write_text('[hub.runner]\nkill_grace = 1\ninprocess_modules = ["fast"]\nbwrap = "off"\n')
    config = load_runner_config(environ={})
    assert (
        config.kill_grace == 1.0 and config.inprocess_modules == frozenset({"fast"}) and config.bwrap == "off"
    )


def test_runner_limits_become_manifest_defaults(hal_home):
    hal_home.config.write_text("[hub.runner]\nmax_timeout = 90\nmax_result_chars = 500\n")
    config = HubConfig.load(environ={})
    assert config.runner.max_timeout == 90 and config.manifest_defaults.max_timeout == 90
    assert config.manifest_defaults.max_result_chars == 500


# ---- bubblewrap wiring (hermetic: bwrap itself is not run) ------------------------------------


class UnwrappingPopen(CountingPopen):
    """Records the argv, then starts the command without the bwrap prefix."""

    def __call__(self, argv, **kwargs):
        self.started.append(list(argv))
        if argv[0] == "bwrap":
            argv = argv[argv.index("--") + 1 :]
        return subprocess.Popen(argv, **kwargs)


def test_offline_tool_gets_a_network_drop_and_online_tool_does_not(make_runner, probe, monkeypatch):
    monkeypatch.setattr("halhub.runner.bwrap_available", lambda: True)
    popen = UnwrappingPopen()
    runner = make_runner(dataclasses.replace(FAST, bwrap="auto"), popen=popen)
    assert run(runner, probe, "probe_shout", {"text": "x"}).ok
    assert run(runner, probe, "probe_online", {"port": 1}).error_type == "tool_error"
    offline, online = popen.started
    assert offline[:1] == ["bwrap"] and "--unshare-net" in offline and "--unshare-pid" in offline
    assert online[:1] == ["bwrap"] and "--unshare-net" not in online
    assert offline[offline.index("--") + 1 :][-4:-2] == ["--module", "halprobe.tools"]


def test_missing_bwrap_runs_workers_without_it_and_says_so(make_runner, probe, monkeypatch, caplog):
    monkeypatch.setattr("halhub.runner.bwrap_available", lambda: False)
    popen = CountingPopen()
    runner = make_runner(dataclasses.replace(FAST, bwrap="auto"), popen=popen)
    assert run(runner, probe, "probe_shout", {"text": "x"}).ok
    assert popen.started[0][0] != "bwrap"
    assert "unavailable" in runner.sandbox_status()
    assert sum("bubblewrap is unavailable" in r.getMessage() for r in caplog.records) == 1


def test_bwrap_off_never_probes(make_runner, probe, monkeypatch):
    def explode():
        raise AssertionError("probed")

    monkeypatch.setattr("halhub.runner.bwrap_available", explode)
    runner = make_runner()
    assert run(runner, probe, "probe_shout", {"text": "x"}).ok
    assert "off" in runner.sandbox_status()


# ---- one-shot v1 exec modules (REQ-HUB-30) -----------------------------------------------------


@pytest.fixture
def oneshot(fixtures) -> ModuleSpec:
    return load_manifest(fixtures / "oneshot" / "hal-module.toml", source="path")


def test_oneshot_v1_exec_module_still_runs(make_runner, oneshot):
    outcome = run(make_runner(), oneshot, "oneshot_add", {"a": 2, "b": 3})
    assert outcome.ok and outcome.result == {"sum": 5, "cwd_ok": True}


def test_oneshot_output_that_is_not_json_is_returned_as_text(make_runner, oneshot):
    assert run(make_runner(), oneshot, "oneshot_say").result == "plain words, not JSON"


def test_oneshot_failure_reports_the_end_of_stderr(make_runner, oneshot):
    outcome = run(make_runner(), oneshot, "oneshot_fail")
    assert outcome.error_type == "tool_error" and outcome.message.endswith("the calendar file is locked")


def test_oneshot_timeout_kills_the_process_group(make_runner, oneshot, tmp_path):
    pidfile = tmp_path / "child.pid"
    started = time.monotonic()
    outcome = run(make_runner(), oneshot, "oneshot_sleep", {"pidfile": str(pidfile)}, timeout=0.5)
    assert outcome.error_type == "timeout" and time.monotonic() - started < 0.5 + FAST.kill_grace + 1
    assert not pid_alive(int(pidfile.read_text()))


def test_oneshot_cancel_stops_the_call(make_runner, oneshot, tmp_path):
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    outcome = run(
        make_runner(), oneshot, "oneshot_sleep", {"pidfile": str(tmp_path / "p")}, timeout=10, cancel=cancel
    )
    assert outcome.error_type == "cancelled"


# ---- in-process modules (REQ-HUB-24) -----------------------------------------------------------


@pytest.fixture
def inprocess_demo(fixtures, monkeypatch) -> ModuleSpec:
    monkeypatch.syspath_prepend(str(fixtures / "demo"))
    module = load_manifest(fixtures / "demo" / "hal-module.toml", source="path")
    return dataclasses.replace(module, inprocess=True, secrets=())


def test_inprocess_module_listed_in_config_runs_in_the_hub(make_runner, inprocess_demo):
    runner = make_runner(dataclasses.replace(FAST, inprocess_modules=frozenset({"demo"})))
    outcome = run(runner, inprocess_demo, "demo_echo", {"text": "hi", "times": 2})
    assert outcome.ok and outcome.result == "hi hi" and outcome.worker_pid is None


def test_inprocess_flag_without_config_still_runs_in_a_worker(make_runner, inprocess_demo):
    outcome = run(make_runner(), inprocess_demo, "demo_echo", {"text": "hi"})
    assert outcome.ok and outcome.worker_pid is not None


def test_listed_module_without_the_flag_runs_in_a_worker(make_runner, inprocess_demo):
    runner = make_runner(dataclasses.replace(FAST, inprocess_modules=frozenset({"demo"})))
    module = dataclasses.replace(inprocess_demo, inprocess=False)
    assert run(runner, module, "demo_echo", {"text": "hi"}).worker_pid is not None


def test_tainted_inprocess_module_is_refused(make_runner, tmp_path, monkeypatch):
    (tmp_path / "halslow.py").write_text(
        "import time\n\n\ndef nap(seconds):\n    time.sleep(seconds)\n    return 'up'\n"
    )
    manifest = tmp_path / "hal-module.toml"
    manifest.write_text(
        'schema = 2\n[module]\nname = "slow"\nsummary = "s"\npython = "halslow"\ninprocess = true\n'
        '[[tools]]\nname = "slow_nap"\nfunction = "nap"\ndescription = "d"\ntier = "read"\n'
        '[tools.params.seconds]\ntype = "number"\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module = load_manifest(manifest, source="path")
    runner = make_runner(dataclasses.replace(FAST, inprocess_modules=frozenset({"slow"})))
    assert run(runner, module, "slow_nap", {"seconds": 1}, timeout=0.2).error_type == "timeout"
    refused = run(runner, module, "slow_nap", {"seconds": 0}, timeout=5)
    assert refused.error_type == "worker_failed" and "restarts" in refused.message


def test_inprocess_errors_map_like_worker_errors(make_runner, inprocess_demo):
    runner = make_runner(dataclasses.replace(FAST, inprocess_modules=frozenset({"demo"})))
    assert (
        run(runner, inprocess_demo, "demo_echo", {"text": "a", "loud": 1}).error_type == "manifest_mismatch"
    )


def test_v1_tools_keep_the_network_because_they_never_declared_it(make_runner, fixtures, monkeypatch):
    monkeypatch.setattr("halhub.runner.bwrap_available", lambda: True)
    legacy = load_manifest(fixtures / "legacy" / "hal-module.toml", source="path")
    popen = UnwrappingPopen()
    runner = make_runner(dataclasses.replace(FAST, bwrap="auto"), popen=popen)
    assert run(runner, legacy, "legacy_add", {"a": 1, "b": 2}).result == 3
    assert popen.started[0][0] == "bwrap" and "--unshare-net" not in popen.started[0]
