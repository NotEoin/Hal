"""Motion for the full-screen UIs: easing maths and one frame clock (`Ticker`) per app.

Motion is only for things that change on their own: streaming, running tools, subagents, the
HAL eye and gauges easing to a new value. Rules:

- One timer per app, created on the first subscription and paused when the last one ends, so
  an idle app wakes no one (the old working line ticked 4 times a second forever).
- At most `FPS_MAX` frames a second (a terminal redraw costs more above it). When a frame takes
  longer than its budget `SLOW_FRAMES` times in a row, the cap halves.
- Reduce motion (`[ui].animations = false`): `subscribe` starts nothing, `tween` jumps to its end
  value; `every` still runs, because elapsed seconds and speed samples are information, not motion.

The module never imports Textual: `Ticker` takes a `set_interval`-like callable, so the maths is
tested without a terminal and the line UI may use it.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

FPS_MAX = 30  # the design cap; a terminal redraw costs more above it
SPINNER_HZ = 10  # braille frames per second
EYE_PULSE_PERIOD_S = 2.4  # thinking: one slow breath
EYE_BLINK_PERIOD_S = 1.0  # approval: on 0.5 s, off 0.5 s
EYE_HZ = 12  # enough steps for a smooth pulse at 2.4 s
GAUGE_TWEEN_S = 0.25  # a gauge eases to a new value
SLOW_FRAMES = 3  # frames over budget in a row before the cap halves
MIN_FPS = 1
DUE_SLACK_S = 0.002  # a timer that fires a hair early still counts as on time
clock = time.monotonic  # one name to read time through, so snapshot tests can freeze it


class Timer(Protocol):
    """What `set_interval` returns (Textual's `Timer` fits)."""

    def pause(self) -> None:
        """Stops calling back until `resume`."""

    def resume(self) -> None:
        """Calls back again."""

    def stop(self) -> None:
        """Ends the timer."""


def lerp(a: float, b: float, t: float) -> float:
    """The value `t` of the way from `a` to `b`."""
    return a + (b - a) * t


def ease_out_cubic(t: float) -> float:
    """Fast start, gentle stop; `t` clamped to 0..1."""
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out_sine(t: float) -> float:
    """Gentle start and stop; `t` clamped to 0..1."""
    t = min(1.0, max(0.0, t))
    return -(math.cos(math.pi * t) - 1) / 2


def pulse(t: float, period: float) -> float:
    """0 to 1 and back to 0 once per `period` seconds, sine shaped."""
    return (1 - math.cos(2 * math.pi * (t % period) / period)) / 2 if period > 0 else 0.0


def blink(t: float, period: float) -> bool:
    """True for the first half of each `period`."""
    return period <= 0 or (t % period) < period / 2


def blend(hex_a: str, hex_b: str, t: float) -> str:
    """The colour `t` of the way from `#rrggbb` `hex_a` to `hex_b`."""
    t = min(1.0, max(0.0, t))
    a, b = hex_a.lstrip("#"), hex_b.lstrip("#")
    parts = [round(lerp(int(a[i : i + 2], 16), int(b[i : i + 2], 16), t)) for i in (0, 2, 4)]
    return "#" + "".join(f"{p:02x}" for p in parts)


class Handle:
    """A subscription or tween; `cancel()` stops it, `done` says it has ended."""

    def __init__(self, ticker: Ticker | None = None) -> None:
        self.ticker = ticker
        self.done = ticker is None

    def cancel(self) -> None:
        """Stops it; a second call does nothing."""
        if not self.done:
            self.done = True
            if self.ticker is not None:
                self.ticker._drop(self)  # noqa: SLF001 - the handle belongs to its ticker


@dataclass
class _Sub:
    callback: Callable[[float], bool | None]
    period: float
    due: float
    handle: Handle


class Ticker:
    """One app-wide frame clock: runs only while something subscribes, at most `max_fps` frames a second."""

    def __init__(
        self, set_interval: Callable[[float, Callable[[], None]], Timer], *, enabled: bool,
        clock: Callable[[], float] | None = None, max_fps: int = FPS_MAX,
    ) -> None:  # fmt: skip
        self.set_interval = set_interval
        self.enabled = enabled
        self.clock = clock or (lambda: globals()["clock"]())
        self.max_fps = max(MIN_FPS, max_fps)
        self._subs: list[_Sub] = []
        self._timer: Timer | None = None
        self._interval = 0.0
        self._paused = False
        self._slow = 0

    @property
    def active(self) -> int:
        """Live subscriptions."""
        return len(self._subs)

    @property
    def running(self) -> bool:
        """True while the interval timer exists and is not paused."""
        return self._timer is not None and not self._paused

    def subscribe(self, callback: Callable[[float], bool | None], hz: float) -> Handle:
        """Calls `callback(now)` about `hz` times a second until it returns False or is cancelled; nothing with motion off."""
        if not self.enabled:
            return Handle()
        return self._add(callback, hz)

    def every(self, seconds: float, callback: Callable[[float], bool | None]) -> Handle:
        """Calls `callback(now)` every `seconds`, also with motion off (for numbers, not motion)."""
        return self._add(callback, 1 / seconds if seconds > 0 else self.max_fps)

    def tween(
        self, start: float, end: float, duration_s: float, on_value: Callable[[float], None],
        ease: Callable[[float], float] = ease_out_cubic,
    ) -> Handle:  # fmt: skip
        """Eases from `start` to `end` over `duration_s`; with motion off calls `on_value(end)` once, at once."""
        if not self.enabled or duration_s <= 0:
            on_value(end)
            return Handle()
        began = self.clock()

        def step(now: float) -> bool:
            t = (now - began) / duration_s
            if t >= 1:
                on_value(end)
                return False
            on_value(lerp(start, end, ease(t)))
            return True

        return self._add(step, self.max_fps)

    def frame(self) -> None:
        """One tick: calls each subscriber that is due; the timer calls it, tests may call it directly."""
        now = self.clock()
        for sub in list(self._subs):
            if sub.handle.done or sub.due > now + DUE_SLACK_S:
                continue
            sub.due = now + sub.period
            try:
                keep = sub.callback(now)
            except Exception:  # noqa: BLE001 - a broken animation is logged and dropped, never raised into the app
                log.exception("animation callback failed; dropped")
                keep = False
            if keep is False:
                sub.handle.cancel()
        self._budget(self.clock() - now)

    def _budget(self, took: float) -> None:
        if took > 1 / self.max_fps:
            self._slow += 1
            if self._slow >= SLOW_FRAMES and self.max_fps > MIN_FPS:
                self.max_fps = max(MIN_FPS, self.max_fps // 2)
                self._slow = 0
                log.debug("frames over budget; motion capped at %d fps", self.max_fps)
                self._schedule()
        else:
            self._slow = 0

    def _add(self, callback: Callable[[float], bool | None], hz: float) -> Handle:
        handle = Handle(self)
        period = 1 / max(hz, 1e-6)
        self._subs.append(_Sub(callback, period, self.clock(), handle))
        self._schedule()
        return handle

    def _drop(self, handle: Handle) -> None:
        self._subs = [s for s in self._subs if s.handle is not handle]
        self._schedule()

    def _schedule(self) -> None:
        """Pauses the timer when nothing subscribes; else runs it at the fastest rate a subscriber needs."""
        if not self._subs:
            if self._timer is not None and not self._paused:
                self._timer.pause()
                self._paused = True
            return
        fastest = max(1 / s.period for s in self._subs)
        interval = 1 / min(self.max_fps, fastest)
        if self._timer is not None and abs(interval - self._interval) > 1e-9:
            self._timer.stop()
            self._timer = None
        if self._timer is None:
            self._timer = self.set_interval(interval, self.frame)
            self._interval = interval
            self._paused = False
        elif self._paused:
            self._timer.resume()
            self._paused = False

    def stop(self) -> None:
        """Ends every subscription and the timer (app shutdown)."""
        for sub in list(self._subs):
            sub.handle.done = True
        self._subs.clear()
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
