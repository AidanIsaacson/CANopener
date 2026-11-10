"""Meter calibration: a zero offset and a gain per measurement channel, set against a known voltage.

The prototype's probe channels (a divider of 1% parts into an ADC whose own input resistance
loads it a little) read a percent or two off. That error is steady per channel, so two numbers
fix it: the reading with the probe on COM (the **offset**, subtracted first) and a **gain** that
makes a known voltage read right::

    shown = (raw - offset) * gain

``Calibration`` keeps them in ``core.settings`` (``meters.cal``: channel id -> values), so every
front-end and every capture uses the same correction; ``MeasurementHub`` applies it to each
sample as it arrives. ``CalSession`` is the step-by-step flow the handheld walks through (and any
other front-end can): per channel, zero with the probe on COM, then the voltage step, then on to
the next channel without going back to a menu.

* **Full:** zero (push to confirm), touch a supply and push to hold the reading, then either
  accept it or turn the encoder until it matches a second meter, and save.
* **Quick:** enter the supply's voltage once (measured with a second meter), then for each
  channel just touch COM and then the supply; each step is taken by itself once the reading
  settles, with a beep.

Each channel is saved as soon as it's done, so stopping half way keeps the finished ones.
Standard library only.
"""

import math
import threading
import time
from collections import deque
from dataclasses import dataclass

SETTING = "meters.cal"          # channel id -> ChannelCal.to_dict()
REF_SETTING = "meters.cal_ref"  # the last supply voltage typed in for Quick

FULL, QUICK = "full", "quick"

# Steps of a CalSession
REF = "ref"             # Quick: type in the supply voltage
ZERO = "zero"           # probe on COM
SPAN = "span"           # probe on the supply
CONFIRM = "confirm"     # Full: the held reading, accept or adjust
ADJUST = "adjust"       # Full: turn the encoder until it matches the second meter
SAVED = "saved"         # a moment showing "saved" before the next channel
DONE = "done"

ZERO_LIMIT = 0.5        # V: a zero reading further from 0 than this isn't a probe on COM
SPAN_MIN = 0.5          # V above the zero before a reading counts as "on the supply"
QUICK_TOLERANCE = 0.25  # Quick takes a span reading by itself within +/-25% of the typed voltage
GAIN_RANGE = (0.2, 5.0)  # a gain outside this means the divider setting is wrong, not calibration
SAVED_SECS = 1.5
NO_DATA_SECS = 3.0
REF_RANGE = (1.0, 100.0)
STEPS = [0.1, 0.01, 0.001]  # adjust step sizes (V per encoder detent)


def _num(v, default):
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else default


@dataclass(frozen=True)
class ChannelCal:
    offset: float = 0.0             # raw reading with the probe on COM
    gain: float = 1.0
    ref: float | None = None        # the voltage the gain was set against
    when: str = ""                  # date it was saved (YYYY-MM-DD)

    def apply(self, raw: float) -> float:
        return (raw - self.offset) * self.gain

    def to_dict(self) -> dict:
        d = {"offset": self.offset, "gain": self.gain, "when": self.when}
        if self.ref is not None:
            d["ref"] = self.ref
        return d

    @classmethod
    def from_dict(cls, d) -> "ChannelCal | None":
        if not isinstance(d, dict):
            return None
        gain = _num(d.get("gain"), 1.0)
        if not GAIN_RANGE[0] <= gain <= GAIN_RANGE[1]:
            gain = 1.0
        ref = d.get("ref")
        return cls(_num(d.get("offset"), 0.0), gain, _num(ref, None) if ref is not None else None,
                   str(d.get("when") or ""))

    def describe(self) -> str:
        """One line for a list: "gain 1.0123, zero +0.004 V, 2026-11-10"."""
        parts = [f"gain {self.gain:.4f}", f"zero {self.offset:+.3f} V"]
        if self.when:
            parts.append(self.when)
        return ", ".join(parts)


class Calibration:
    """Every channel's correction, read from and saved to ``core.settings``. ``apply`` is called
    from the sources' threads for every sample, so it's a lock-free dictionary lookup."""

    def __init__(self, settings=None):
        self.settings = settings
        self._lock = threading.Lock()
        self._cal: dict[str, ChannelCal] = {}
        self.reload()

    def reload(self):
        raw = self.settings.get(SETTING) if self.settings is not None else {}
        cal = {}
        for cid, d in (raw or {}).items() if isinstance(raw, dict) else ():
            c = ChannelCal.from_dict(d)
            if c is not None:
                cal[str(cid)] = c
        self._cal = cal

    def get(self, channel_id: str) -> ChannelCal | None:
        return self._cal.get(channel_id)

    def apply(self, channel_id: str, value):
        c = self._cal.get(channel_id)
        return value if c is None or value is None else c.apply(value)

    def channels(self) -> dict[str, ChannelCal]:
        return dict(self._cal)

    def _store(self, cal: dict):
        self._cal = cal
        if self.settings is not None:
            self.settings.set(SETTING, {cid: c.to_dict() for cid, c in sorted(cal.items())})

    def save(self, channel_id: str, cal: ChannelCal):
        with self._lock:
            self._store({**self._cal, channel_id: cal})

    def reset(self, channel_id: str | None = None):
        """Back to uncalibrated: one channel, or all of them."""
        with self._lock:
            self._store({} if channel_id is None else {k: v for k, v in self._cal.items() if k != channel_id})

    def reset_source(self, source_id: str) -> int:
        """Drop every channel of one source ("iio"): its scaling changed, so the old numbers are wrong."""
        with self._lock:
            keep = {k: v for k, v in self._cal.items() if not k.startswith(source_id + ".")}
            dropped = len(self._cal) - len(keep)
            if dropped:
                self._store(keep)
            return dropped

    def snapshot(self) -> dict:
        """For a capture's metadata: which correction its readings were taken with."""
        return {cid: c.to_dict() for cid, c in sorted(self._cal.items())}


def calibratable(channels) -> list:
    """The channels a voltage calibration applies to (``core.measure.Channel``s), in order."""
    return [c for c in channels if c.unit == "V" or c.kind == "voltage"]


class Steady:
    """Has a reading settled? True once the last ``window`` seconds hold at least ``min_samples``
    readings within a small spread (``abs_tol`` volts, or ``rel_tol`` of the reading if larger)."""

    def __init__(self, window: float = 1.0, min_samples: int = 5, abs_tol: float = 0.02, rel_tol: float = 0.003):
        self.window, self.min_samples, self.abs_tol, self.rel_tol = window, min_samples, abs_tol, rel_tol
        self._pts: deque = deque()

    def reset(self):
        self._pts.clear()

    def add(self, t: float, v: float):
        self._pts.append((t, v))
        while self._pts and self._pts[0][0] < t - self.window:
            self._pts.popleft()

    @property
    def last(self):
        return self._pts[-1][1] if self._pts else None

    @property
    def mean(self):
        return sum(v for _, v in self._pts) / len(self._pts) if self._pts else None

    @property
    def steady(self) -> bool:
        if len(self._pts) < self.min_samples:
            return False
        if self._pts[-1][0] - self._pts[0][0] < self.window * 0.75:
            return False
        vs = [v for _, v in self._pts]
        return max(vs) - min(vs) <= max(self.abs_tol, self.rel_tol * abs(self.mean))


@dataclass
class ChannelResult:
    channel_id: str
    label: str
    state: str = "pending"          # pending / current / saved / skipped
    cal: ChannelCal | None = None


class CalSession:
    """One run through the channels. Single-threaded: the front-end feeds it the current
    channel's *raw* (uncorrected) readings with ``feed``, calls ``update`` every frame, and passes
    the user's input to ``select`` / ``adjust`` / ``choose_adjust`` / ``skip`` / ``redo``.

    ``events`` collects sounds to play ("cal_step": a reading was taken, "cal_saved": a channel
    was saved, "error": that didn't work); the front-end drains it with ``pop_events``."""

    def __init__(self, cal: Calibration, channels: list[tuple[str, str]], mode: str = FULL,
                 ref: float | None = None, clock=time.monotonic, today=None):
        self.cal = cal
        self.mode = mode if mode in (FULL, QUICK) else FULL
        self.channels = [ChannelResult(cid, label) for cid, label in channels]
        self.clock = clock
        self.today = today or (lambda: time.strftime("%Y-%m-%d"))
        self.ref = min(max(_num(ref, 12.0), REF_RANGE[0]), REF_RANGE[1])
        self.step_size = 0.01
        self.index = 0
        self.steady = Steady()
        self.events: list[str] = []
        self.problem = ""               # what's wrong right now ("Reads 3.2 V: ...")
        self.new_offset: float | None = None
        self.held: float | None = None  # the raw reading held on the supply
        self.value = 0.0                # Full: the reading being adjusted (what the channel will show)
        self.confirm_focus = 0          # Full, CONFIRM: 0 = looks right, 1 = adjust
        self._entered = self.clock()
        self._last_feed: float | None = None
        self._auto_blocked = False      # Quick: a reading was refused; wait for a new one
        self.step = DONE if not self.channels else (REF if self.mode == QUICK else ZERO)
        if self.channels and self.step != REF:
            self._start_channel()

    # --- state ---
    @property
    def current(self) -> ChannelResult | None:
        return self.channels[self.index] if self.step != DONE and self.index < len(self.channels) else None

    @property
    def old(self) -> ChannelCal:
        """The current channel's saved calibration (or none: gain 1, no offset)."""
        c = self.current
        return (self.cal.get(c.channel_id) if c else None) or ChannelCal()

    @property
    def live(self):
        """The latest raw reading of the current channel."""
        return self.steady.last

    def shown(self, raw):
        """What the channel would display for ``raw`` with the zero just taken and the old gain."""
        if raw is None:
            return None
        offset = self.new_offset if self.new_offset is not None else self.old.offset
        return (raw - offset) * self.old.gain

    def pop_events(self) -> list[str]:
        out, self.events = self.events, []
        return out

    def _go(self, step: str):
        self.step = step
        self.problem = ""
        self.steady.reset()
        self._entered = self.clock()
        self._auto_blocked = False

    def _start_channel(self):
        for i, c in enumerate(self.channels):
            if i == self.index:
                c.state = "current"
        self.new_offset = None
        self.held = None
        self._go(ZERO)

    def _next_channel(self):
        self.index += 1
        while self.index < len(self.channels) and self.channels[self.index].state in ("saved", "skipped"):
            self.index += 1
        if self.index >= len(self.channels):
            self._go(DONE)
        else:
            self._start_channel()

    # --- input ---
    def feed(self, t: float, raw):
        """A raw reading of the current channel (None = no valid reading)."""
        self._last_feed = self.clock()
        if raw is None or self.step not in (ZERO, SPAN):
            return
        self.steady.add(t, raw)

    def update(self, now: float | None = None):
        """Time-driven steps: Quick's automatic readings, the pause after a save, no data."""
        now = self.clock() if now is None else now
        if self.step == SAVED and now - self._entered >= SAVED_SECS:
            self._next_channel()
            return
        if self.step not in (ZERO, SPAN):
            return
        quiet_since = self._last_feed if self._last_feed is not None else self._entered
        if now - quiet_since > NO_DATA_SECS:
            label = self.current.label if self.current else "the channel"
            self.problem = f"No readings from {label}. Is the meter connected?"
            return
        if self.problem.startswith("No readings"):
            self.problem = ""
        if self.step == ZERO:
            self._check_zero(auto=self.mode == QUICK)
        else:
            self._check_span(auto=self.mode == QUICK)

    def _check_zero(self, auto: bool) -> bool:
        if not self.steady.steady:
            return False
        m = self.steady.mean
        if abs(m) > ZERO_LIMIT:
            self.problem = f"Reads {m:.2f} V. Touch the probe to the COM lead."
            return False
        self.problem = ""
        if auto:
            self._take_zero()
        return True

    def _take_zero(self):
        self.new_offset = self.steady.mean
        self.events.append("cal_step")
        self._go(SPAN)

    def _check_span(self, auto: bool) -> bool:
        if not self.steady.steady:
            self._auto_blocked = False
            return False
        if self._auto_blocked:
            return False
        m = self.steady.mean
        if abs(m - self.new_offset) < SPAN_MIN:
            self.problem = ""
            return False
        if self.mode == QUICK:
            shown = self.shown(m)
            if abs(shown - self.ref) > QUICK_TOLERANCE * self.ref:
                self.problem = (f"Reads {shown:.2f} V, expected {self.ref:.2f} V. Check the supply, "
                                f"or push to use it anyway.")
                return False
            self.problem = ""
            if auto:
                self._save_span(m, self.ref)
            return True
        self.problem = ""
        return True

    def select(self):
        """The encoder push (or Enter): whatever the current step's main action is."""
        if self.step == REF:
            self.events.append("cal_step")
            self._start_channel()
        elif self.step == ZERO:
            if self._check_zero(auto=False):
                self._take_zero()
            else:
                self.events.append("error")
                if not self.problem:
                    self.problem = "Hold the probe on COM until the reading settles."
        elif self.step == SPAN:
            ok = self._check_span(auto=False)
            m = self.steady.mean
            if self.mode == QUICK and m is not None and self.steady.steady and abs(m - self.new_offset) >= SPAN_MIN:
                self._save_span(m, self.ref)            # push = use it anyway, even outside the tolerance
            elif ok:
                self.held = m
                self.value = self.shown(m)
                self.confirm_focus = 0
                self.events.append("cal_step")
                self._go(CONFIRM)
            else:
                self.events.append("error")
                if not self.problem:
                    self.problem = "Hold the probe on the supply until the reading settles."
        elif self.step == CONFIRM:
            if self.confirm_focus == 1:
                self.choose_adjust()
            else:
                self._save_span(self.held, self.value)
        elif self.step == ADJUST:
            self._save_span(self.held, self.value)
        elif self.step == SAVED:
            self._next_channel()

    def choose_adjust(self):
        if self.step == CONFIRM:
            self._go(ADJUST)

    def toggle_confirm(self):
        if self.step == CONFIRM:
            self.confirm_focus = 1 - self.confirm_focus

    def back(self) -> bool:
        """One step back within the channel (adjust -> confirm -> retake). False: nothing to go back to."""
        if self.step == ADJUST:
            self.value = self.shown(self.held)
            self._go(CONFIRM)
            return True
        if self.step == CONFIRM:
            self._go(SPAN)
            return True
        return False

    def adjust(self, steps: int):
        """Turn the encoder: the typed supply voltage (REF) or the reading being matched (ADJUST)."""
        if self.step == REF:
            self.ref = round(min(max(self.ref + steps * self.step_size, REF_RANGE[0]), REF_RANGE[1]), 4)
        elif self.step == ADJUST:
            self.value = round(self.value + steps * self.step_size, 4)

    def cycle_step(self):
        self.step_size = STEPS[(STEPS.index(self.step_size) + 1) % len(STEPS)] if self.step_size in STEPS else 0.01

    def skip(self):
        """Leave this channel as it was and go on to the next."""
        c = self.current
        if c is None:
            return
        c.state = "skipped"
        self._next_channel()

    def redo(self):
        """Start the current channel again from the zero."""
        if self.current is not None:
            self._start_channel()

    # --- saving ---
    def _save_span(self, raw: float, value: float):
        c = self.current
        delta = raw - self.new_offset
        gain = value / delta if delta else 0.0
        if not GAIN_RANGE[0] <= gain <= GAIN_RANGE[1]:
            self.events.append("error")
            self._auto_blocked = True
            self.problem = (f"That needs a gain of {gain:.2f}, too far to be calibration. Is the ADC input "
                            f"divider set right (System > Meters)?")
            return
        cal = ChannelCal(self.new_offset, gain, value, self.today())
        self.cal.save(c.channel_id, cal)
        c.cal, c.state = cal, "saved"
        self.events.append("cal_saved")
        self._go(SAVED)

    @property
    def saved(self) -> list[ChannelResult]:
        return [c for c in self.channels if c.state == "saved"]
