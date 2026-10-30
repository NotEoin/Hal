"""Tests for the tunnel supervisor with scripted fakes (REQ-RMT-28, 29)."""

from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from halremote import supervisor
from halremote.model import Forward, Profile, ProfilePaths
from halremote.supervisor import ProbeResult


class FakeClock:
    """Time that only moves when the supervisor waits; `on_wait` lets a test act at a given time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []
        self.hooks: list = []

    def monotonic(self) -> float:
        return self.now

    def wait(self, stop: threading.Event, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += seconds
        for hook in self.hooks:
            hook(self.now)
        return stop.is_set()


@dataclass
class Script:
    """How one fake ssh behaves: exits `after` seconds from its start (None: runs until killed)."""

    after: float | None = None
    code: int = 255
    stderr: str = ""


class FakeChild:
    def __init__(self, clock: FakeClock, script: Script, pid: int) -> None:
        self.clock, self.script, self.pid = clock, script, pid
        self.started = clock.now
        self.code: int | None = None
        self.terminated = False

    def poll(self):
        if (
            self.code is None
            and self.script.after is not None
            and self.clock.now - self.started >= self.script.after
        ):
            self.code = self.script.code
        return self.code

    def stderr_tail(self):
        return self.script.stderr

    def terminate(self):
        self.terminated = True
        self.code = -15

    def kill(self):
        self.code = -9

    def wait(self, timeout_s):
        return self.code


@dataclass
class FakeSpawner:
    clock: FakeClock
    stop: threading.Event
    scripts: list[Script]
    children: list[FakeChild] = field(default_factory=list)

    def spawn(self, argv, env):
        assert env["LANG"] == "C"
        assert argv[-1].endswith("-tunnel")
        if not self.scripts:
            self.stop.set()
            script = Script(after=0)
        else:
            script = self.scripts.pop(0)
        child = FakeChild(self.clock, script, 5000 + len(self.children))
        self.children.append(child)
        return child


@dataclass
class FakeProber:
    results: list[ProbeResult] = field(default_factory=list)
    ready: bool = True
    seen_tokens: list[str] = field(default_factory=list)

    def listening(self, port):
        return self.ready

    def models(self, port, token, timeout_s):
        self.seen_tokens.append(token)
        return self.results.pop(0) if self.results else ProbeResult(200, "3 models")


@pytest.fixture
def rig(tmp_path):
    root = tmp_path / "cfg"
    root.mkdir()
    paths = ProfilePaths(
        root=root,
        ssh_config=root / "ssh_config",
        known_hosts=root / "known_hosts",
        tunnel_key=root / "tunnel",
        mcp_key=root / "mcp",
        token=root / "gateway-token",
        state=tmp_path / "state",
    )
    for p in (paths.ssh_config, paths.tunnel_key):
        p.write_text("x")
    paths.token.write_text("tok-abc\n")
    profile = Profile("desktop", "laptop", "desk.lan", "user", 22, (Forward("gateway", 8741, 8741),))
    clock = FakeClock()
    stop = threading.Event()

    def build(scripts, prober=None):
        spawner = FakeSpawner(clock, stop, list(scripts))
        prober = prober or FakeProber()
        sup = supervisor.Supervisor(
            profile, paths, ssh=Path("/usr/bin/ssh"), clock=clock, spawner=spawner,
            prober=prober, rng=random.Random(7),
        )  # fmt: skip
        return sup, spawner, prober

    return {"build": build, "clock": clock, "stop": stop, "paths": paths}


def status(rig):
    return json.loads((rig["paths"].state / "status.json").read_text())


def test_backoff_doubles_with_jitter_and_is_capped():
    rng = random.Random(1)
    delays = [supervisor.next_delay(n, initial=2, maximum=60, rng=rng) for n in (1, 2, 3, 4, 10)]
    for delay, base in zip(delays, (2, 4, 8, 16, 60), strict=True):
        assert 0.8 * base <= delay <= 1.2 * base


@pytest.mark.parametrize(
    "stderr",
    [
        "user@desk: Permission denied (publickey).",
        "Host key verification failed.",
        "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@",
        "bind [127.0.0.1]:8741: Address already in use",
        "Could not request local forwarding.",
        "Bad configuration option: foo",
        'Load key "x": invalid format',
        "Permissions 0644 for 'k' are too open. bad permissions",
    ],
)
def test_each_permanent_stderr_is_classified_permanent(stderr):
    assert supervisor.classify_exit(255, stderr, 1.0) == "permanent"


@pytest.mark.parametrize(
    "stderr",
    [
        "ssh: connect to host desk.lan port 22: Connection refused",
        "Connection timed out during banner exchange",
        "connect: Network is unreachable",
        "Could not resolve hostname desk.lan",
        "",
    ],
)
def test_network_trouble_is_transient(stderr):
    assert supervisor.classify_exit(255, stderr, 1.0) == "transient"


def test_transient_exits_back_off_with_growing_delays_then_stop(rig):
    refused = Script(after=1, stderr="Connection refused")
    sup, spawner, _ = rig["build"]([refused, refused, refused])
    assert sup.run(rig["stop"]) == 0
    backoffs = [w for w in rig["clock"].waits if w > 1.5]
    assert len(spawner.children) == 4
    assert 1.6 <= backoffs[0] <= 2.4
    assert 3.2 <= backoffs[1] <= 4.8
    assert 6.4 <= backoffs[2] <= 9.6
    assert status(rig)["state"] == "stopped"


def test_backoff_resets_after_a_stable_connection(rig):
    refused = Script(after=1, stderr="Connection refused")
    long_lived = Script(after=100, stderr="Connection reset")
    sup, _, _ = rig["build"]([refused, refused, long_lived])
    sup.run(rig["stop"])
    backoffs = [w for w in rig["clock"].waits if w > 1.5]
    assert 1.6 <= backoffs[0] <= 2.4 and 3.2 <= backoffs[1] <= 4.8
    assert 1.6 <= backoffs[2] <= 2.4  # attempt reset after the 100 s connection


def test_permanent_error_stops_with_exit_3_and_a_fix(rig):
    sup, spawner, _ = rig["build"]([Script(after=1, stderr="user@desk: Permission denied (publickey).")])
    assert sup.run(rig["stop"]) == 3
    st = status(rig)
    assert len(spawner.children) == 1
    assert st["state"] == "failed"
    assert "Permission denied" in st["last_error"]
    assert st["fix"] == "hal remote setup desk.lan --rotate"


def test_missing_key_file_is_a_permanent_config_error(rig):
    rig["paths"].tunnel_key.unlink()
    sup, spawner, _ = rig["build"]([Script()])
    assert sup.run(rig["stop"]) == 3
    assert spawner.children == []
    assert "tunnel key is missing" in status(rig)["last_error"]


def test_up_state_writes_the_gateway_health_with_the_token(rig):
    sup, _, prober = rig["build"]([Script()])
    rig["clock"].hooks.append(lambda now: rig["stop"].set() if now > 1000 + 40 else None)
    assert sup.run(rig["stop"]) == 0
    assert prober.seen_tokens == ["tok-abc"]
    assert not (rig["paths"].state / "supervisor.pid").exists()


def test_status_while_up_has_the_documented_fields(rig):
    sup, _, _ = rig["build"]([Script()])
    seen = {}

    def hook(now):
        if now > 1000 + 40 and not seen:
            seen.update(status(rig))
            rig["stop"].set()

    rig["clock"].hooks.append(hook)
    sup.run(rig["stop"])
    assert seen["state"] == "up"
    assert seen["schema"] == "hal.remote.status/1"
    assert seen["gateway"]["ok"] is True
    assert seen["gateway"]["detail"] == "3 models"
    assert seen["forwards"] == [{"name": "gateway", "local": "127.0.0.1:8741", "remote": "127.0.0.1:8741"}]


def test_a_rejected_token_keeps_the_tunnel_up_and_says_to_rotate(rig):
    prober = FakeProber(results=[ProbeResult(401)])
    sup, spawner, _ = rig["build"]([Script()], prober)
    seen = {}

    def hook(now):
        if now > 1000 + 40 and not seen:
            seen.update(status(rig))
            rig["stop"].set()

    rig["clock"].hooks.append(hook)
    sup.run(rig["stop"])
    assert seen["state"] == "up"
    assert seen["gateway"] == {
        **seen["gateway"],
        "ok": False,
        "status": 401,
        "detail": "token rejected: rotate it",
    }
    assert len(spawner.children) == 1


def test_two_connection_failures_in_a_row_restart_ssh(rig):
    prober = FakeProber(results=[ProbeResult(None, "reset"), ProbeResult(None, "reset")])
    sup, spawner, _ = rig["build"]([Script(), Script()], prober)
    rig["clock"].hooks.append(lambda now: rig["stop"].set() if now > 1000 + 90 else None)
    sup.run(rig["stop"])
    assert spawner.children[0].terminated
    assert len(spawner.children) >= 2


def test_one_failure_between_successes_does_not_restart(rig):
    prober = FakeProber(results=[ProbeResult(None), ProbeResult(200), ProbeResult(None)])
    sup, spawner, _ = rig["build"]([Script()], prober)
    rig["clock"].hooks.append(lambda now: rig["stop"].set() if now > 1000 + 100 else None)
    sup.run(rig["stop"])
    assert len(spawner.children) == 1


def test_a_listener_that_never_appears_is_retried(rig):
    sup, spawner, _ = rig["build"]([Script(), Script()], FakeProber(ready=False))
    sup.run(rig["stop"])
    assert spawner.children[0].terminated
    assert len(spawner.children) >= 2


def test_stop_terminates_ssh_and_writes_stopped(rig):
    sup, spawner, _ = rig["build"]([Script()])
    rig["clock"].hooks.append(lambda now: rig["stop"].set() if now > 1000 + 3 else None)
    assert sup.run(rig["stop"]) == 0
    assert spawner.children[0].terminated
    assert status(rig)["state"] == "stopped"


def test_the_stop_file_stops_the_supervisor(rig):
    sup, _, _ = rig["build"]([Script()])
    rig["clock"].hooks.append(
        lambda now: (rig["paths"].state / "stop").write_text("") if now > 1000 + 3 else None
    )
    assert sup.run(rig["stop"]) == 0
    assert status(rig)["state"] == "stopped"
