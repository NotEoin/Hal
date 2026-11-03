"""Tests of the motion module: easing maths and the app-wide `Ticker` (with a fake timer and clock)."""

from __future__ import annotations

import pytest

from halcore.tui import motion


class FakeTimer:
    def __init__(self, interval, callback):
        self.interval, self.callback = interval, callback
        self.paused = self.stopped = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        self.stopped = True


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make(enabled=True, max_fps=motion.FPS_MAX):
    timers, clock = [], Clock()

    def set_interval(interval, callback):
        timers.append(FakeTimer(interval, callback))
        return timers[-1]

    return motion.Ticker(set_interval, enabled=enabled, clock=clock, max_fps=max_fps), timers, clock


def test_ticker_starts_no_timer_until_something_subscribes():
    ticker, timers, _ = make()
    assert not timers and not ticker.running
    ticker.subscribe(lambda now: True, motion.SPINNER_HZ)
    assert len(timers) == 1 and ticker.running


def test_ticker_pauses_when_the_last_subscriber_ends():
    ticker, timers, _ = make()
    handle = ticker.subscribe(lambda now: True, motion.SPINNER_HZ)
    once = ticker.subscribe(lambda now: False, motion.SPINNER_HZ)
    ticker.frame()
    assert once.done and ticker.active == 1 and ticker.running
    handle.cancel()
    assert ticker.active == 0 and not ticker.running and timers[-1].paused
    ticker.subscribe(lambda now: True, motion.SPINNER_HZ)
    assert ticker.running and not timers[-1].paused


def test_ticker_never_exceeds_thirty_frames_per_second():
    ticker, timers, _ = make()
    ticker.subscribe(lambda now: True, 240)
    assert timers[-1].interval == pytest.approx(1 / motion.FPS_MAX)


def test_subscribers_run_at_their_own_rate_on_one_timer():
    ticker, timers, clock = make()
    fast, slow = [], []
    ticker.subscribe(lambda now: fast.append(now) is None, 10)
    ticker.every(1.0, lambda now: slow.append(now) is None)
    for step in range(31):
        clock.now = step / 10
        ticker.frame()
    assert len({id(t) for t in timers if not t.stopped}) == 1
    assert len(fast) == 31 and len(slow) == 4


def test_slow_frames_halve_the_frame_rate():
    ticker, timers, clock = make()

    def heavy(now):
        clock.now += 0.1  # each frame takes 100 ms, three times the 33 ms budget
        return True

    ticker.subscribe(heavy, 30)
    for _ in range(motion.SLOW_FRAMES):
        ticker.frame()
    assert ticker.max_fps == motion.FPS_MAX // 2
    assert timers[-1].interval == pytest.approx(1 / (motion.FPS_MAX // 2))


def test_tween_reaches_the_end_value_exactly():
    ticker, _, clock = make()
    seen = []
    handle = ticker.tween(0.0, 10.0, 0.25, seen.append)
    for step in range(1, 12):
        clock.now = step * 0.03
        ticker.frame()
    assert seen[-1] == 10.0 and handle.done and seen == sorted(seen)
    assert not ticker.running


def test_reduce_motion_jumps_tweens_and_skips_subscriptions():
    ticker, timers, _ = make(enabled=False)
    seen = []
    assert ticker.tween(0.0, 5.0, 1.0, seen.append).done
    assert seen == [5.0]
    assert ticker.subscribe(lambda now: True, 10).done
    assert not timers and ticker.active == 0


def test_every_still_runs_with_motion_off():
    ticker, timers, clock = make(enabled=False)
    calls = []
    ticker.every(1.0, lambda now: calls.append(now) is None)
    clock.now = 1.0
    ticker.frame()
    assert timers and calls == [1.0]


def test_a_failing_callback_is_dropped_not_raised():
    ticker, _, _ = make()

    def broken(now):
        raise RuntimeError("boom")

    handle = ticker.subscribe(broken, 10)
    ticker.frame()
    assert handle.done and ticker.active == 0


def test_pulse_and_blink_have_the_given_period():
    assert motion.pulse(0.0, 2.4) == pytest.approx(0.0)
    assert motion.pulse(1.2, 2.4) == pytest.approx(1.0)
    assert motion.pulse(2.4, 2.4) == pytest.approx(0.0)
    assert motion.blink(0.1, 1.0) and not motion.blink(0.6, 1.0) and motion.blink(1.1, 1.0)


def test_easing_and_blend_hit_both_ends():
    assert motion.ease_out_cubic(0) == 0 and motion.ease_out_cubic(1) == 1
    assert motion.ease_in_out_sine(0) == pytest.approx(0) and motion.ease_in_out_sine(1) == pytest.approx(1)
    assert motion.blend("#000000", "#ffffff", 0.5) == "#808080"
    assert motion.blend("#6b2a2c", "#e5484d", 1.0) == "#e5484d"
