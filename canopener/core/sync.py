"""The SYNC pulse: one clock for the Pi, the meter Pico and the logic Pico (docs/ELECTRONICS.md,
"Time sync").

The meter Pico drives SYNC from a hardware timer (10 Hz). The pulse goes to:

* **Pi GPIO21** with ``dtoverlay=pps-gpio,gpiopin=21``: the kernel stamps each rising edge on
  the same wall clock SocketCAN frames use. ``PpsReader`` polls ``/sys/class/pps/ppsN/assert``.
* **The meter Pico** itself: it prints ``{"sync": N, "t_us": T}`` for pulse N, T on its own
  microsecond counter (``core.meters`` passes these to ``PulseClock.device_pulse``).
* **The logic Pico's D2**: the edges land in every logic capture; ``fit_edges`` maps sample
  numbers to wall time with them.

``PulseClock`` pairs each Pico pulse with the kernel's stamp for the same edge, then maps any
Pico time to wall time by interpolating between pairs, so crystal drift (tens of ppm, which is
milliseconds within minutes) is corrected every 100 ms. Pairing needs no shared pulse counter:
pulses are 100 ms apart and a USB line arrives within a few ms of its edge, so the kernel stamp
just before a sync line (within ``max_lag``) is that pulse. A source without SYNC keeps using
``core.measure.ClockMapper``.

T+0 is then just a wall-clock time like any other: the first pulse after the capture started.

Standard library only.
"""

import bisect
import glob
import os
import threading
import time
from collections import deque

PERIOD_S = 0.1                  # SYNC rate set in the meter firmware (10 Hz)
SYS_PPS = "/sys/class/pps"


class PulseClock:
    """Pico time -> wall-clock time from paired SYNC edges. Thread-safe: the pps reader and the
    meter's reader thread feed it, anything may call ``to_host``."""

    def __init__(self, period_s: float = PERIOD_S, max_lag: float = 0.03, keep: int = 600):
        self.period_s = period_s
        self.max_lag = max_lag              # sync line must arrive within this of its edge
        self._lock = threading.Lock()
        self._host: deque = deque(maxlen=keep)       # kernel edge stamps (wall seconds)
        self._waiting: deque = deque(maxlen=50)      # (device_s, rx_t) not paired yet
        self._pairs: list[tuple[float, float]] = []  # (device_s, host_s), sorted by device_s
        self.keep = keep

    def reset(self):
        with self._lock:
            self._host.clear()
            self._waiting.clear()
            self._pairs.clear()

    def host_pulse(self, t: float):
        """The kernel's time stamp of one SYNC edge."""
        with self._lock:
            if self._host and t <= self._host[-1]:
                return
            self._host.append(t)
            self._pair()

    def device_pulse(self, device_s: float, rx_t: float | None = None):
        """The Pico's time of one SYNC edge, and when its line arrived (wall clock)."""
        with self._lock:
            self._waiting.append((device_s, time.time() if rx_t is None else rx_t))
            self._pair()

    def _pair(self):
        still = deque(maxlen=self._waiting.maxlen)
        for dev, rx in self._waiting:
            edge = next((h for h in reversed(self._host) if h <= rx), None)
            if edge is not None and rx - edge <= self.max_lag:
                if not self._pairs or dev > self._pairs[-1][0]:
                    self._pairs.append((dev, edge))
                    del self._pairs[:-self.keep]
            elif not self._host or self._host[-1] < rx:
                still.append((dev, rx))        # its edge may not have been read yet
        self._waiting = still

    @property
    def locked(self) -> bool:
        """At least two pairs: drift is being corrected."""
        with self._lock:
            return len(self._pairs) >= 2

    def pairs(self) -> list[tuple[float, float]]:
        with self._lock:
            return list(self._pairs)

    def to_host(self, device_s: float) -> float | None:
        """Wall-clock time of a Pico time, or None until two pulses are paired."""
        with self._lock:
            return interpolate(self._pairs, device_s)


def interpolate(pairs: list[tuple[float, float]], x: float) -> float | None:
    """Piecewise-linear map through sorted (x, y) pairs, extrapolating from the end segments."""
    if len(pairs) < 2:
        return None
    xs = [p[0] for p in pairs]
    i = bisect.bisect_right(xs, x)
    i = min(max(i, 1), len(pairs) - 1)
    (x0, y0), (x1, y1) = pairs[i - 1], pairs[i]
    if x1 == x0:
        return y0
    return y0 + (x - x0) * (y1 - y0) / (x1 - x0)


def fit_edges(edge_samples: list[int], samplerate: float, host_pulses: list[float],
              approx_start: float, period_s: float = PERIOD_S) -> list[tuple[float, float]]:
    """Pair the SYNC edges in a logic capture (sample numbers on D2) with kernel pulse stamps.

    ``approx_start`` is the wall time the capture started (when sigrok-cli was told to go: good
    to a few ms, well inside half a period). Returns (sample, wall time) pairs for
    ``interpolate``, so any sample number becomes a wall time."""
    out = []
    host = sorted(host_pulses)
    for s in edge_samples:
        guess = approx_start + s / samplerate
        j = bisect.bisect_left(host, guess)
        near = [h for h in host[max(j - 1, 0):j + 1] if abs(h - guess) < period_s / 2]
        if near:
            out.append((float(s), min(near, key=lambda h: abs(h - guess))))
    return out


def pps_devices(sys_pps: str = SYS_PPS) -> list[tuple[str, str]]:
    """(sysfs folder, name) of each pps device; pps-gpio's name contains "gpio"."""
    out = []
    for d in sorted(glob.glob(os.path.join(sys_pps, "pps*"))):
        try:
            with open(os.path.join(d, "name")) as f:
                out.append((d, f.read().strip()))
        except OSError:
            pass
    return out


def parse_assert(text: str) -> tuple[float, int] | None:
    """``/sys/class/pps/ppsN/assert`` -> (wall seconds, sequence). Format: "1730471234.123456789#42"."""
    stamp, sep, seq = text.strip().partition("#")
    if not sep:
        return None
    sec, _, nsec = stamp.partition(".")
    try:
        return int(sec) + int(nsec or 0) / 10 ** len(nsec or "0"), int(seq)
    except ValueError:
        return None


class PpsReader:
    """Polls a pps device's ``assert`` file and passes each new edge to ``clock.host_pulse``.

    Polling at 50 Hz catches every 10 Hz edge, and the stamp is the kernel's (taken in the
    interrupt), so the poll interval doesn't add jitter. Never blocks: a missing device is a
    status, retried."""

    def __init__(self, clock: PulseClock, folder: str | None = None, poll_s: float = 0.02,
                 sys_pps: str = SYS_PPS):
        self.clock = clock
        self.folder = folder
        self.poll_s = poll_s
        self.sys_pps = sys_pps
        self.edges = 0
        self.status = "not started"
        self._last_seq = None
        self._stop = threading.Event()
        self._thread = None

    def _find(self) -> str | None:
        if self.folder:
            return self.folder if os.path.isdir(self.folder) else None
        return next((d for d, name in pps_devices(self.sys_pps) if "gpio" in name), None)

    def poll_once(self, folder: str) -> bool:
        try:
            with open(os.path.join(folder, "assert")) as f:
                got = parse_assert(f.read())
        except OSError:
            return False
        if got and got[1] != self._last_seq and got[0] > 0:
            self._last_seq = got[1]
            self.edges += 1
            self.clock.host_pulse(got[0])
        return True

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pps", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            folder = self._find()
            if folder is None:
                self.status = "no pps-gpio device (dtoverlay=pps-gpio,gpiopin=21); retrying"
                self._stop.wait(2.0)
                continue
            self.status = f"reading {folder}"
            while not self._stop.is_set() and self.poll_once(folder):
                self._stop.wait(self.poll_s)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
