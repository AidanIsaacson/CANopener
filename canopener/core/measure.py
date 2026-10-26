"""Measurements: anything the handheld reads besides CAN, on the same timebase as the bus.

A DMM probe, the Pico's analog channels, a wireless satellite meter, a Bluetooth BMS: each is a
``MeasurementSource`` offering one or more ``Channel``s and pushing ``Sample``s into a
``MeasurementHub``. The hub keeps the latest value and a short history per channel (for the live
views) and hands every sample to its listeners, which is how a capture records them
(``MeasurementLog`` -> ``measurements.csv`` next to ``capture.log``).

Timebase: ``Sample.t`` is wall-clock seconds (``time.time()``), the same clock ``CanFrame``
timestamps use, so a probe voltage and a CAN frame line up in the capture report and on the
chart. A source with its own clock (a Pico counting microseconds since the sync pulse) converts
with a ``ClockMapper``.

This file is for meter-rate readings (up to roughly a hundred per second per channel). Waveform
and logic-analyzer captures at kHz-MHz rates will be their own files in the capture folder on the
same timebase, listed in ``capture.json``; they don't belong in a CSV row per sample.

Sources live in ``core/meters.py``. Standard library only.
"""

import csv
import io
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

from .live import LiveData

MEASUREMENTS_CSV = "measurements.csv"
CSV_HEADER = ["time", "channel", "name", "value", "unit", "flag"]
HISTORY = 6000                  # samples kept per channel for the live views

# Source status levels (the same lamp colours as core.link)
OK, WAIT, BAD, OFF, SIM = "ok", "wait", "bad", "off", "sim"


@dataclass(frozen=True)
class Channel:
    """One measured quantity. ``id`` is stable across runs ("<source>.<name>") so a capture's
    columns and a user's picked values survive a restart."""

    id: str
    name: str
    unit: str = ""
    source: str = ""
    decimals: int = 3
    kind: str = ""              # voltage / resistance / current / temperature / ... (for icons, ranges)
    low: float | None = None    # normal range, as for CAN signals
    high: float | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}

    @classmethod
    def from_dict(cls, d: dict) -> "Channel":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in fields})


@dataclass(frozen=True)
class Sample:
    channel: str                # Channel.id
    t: float                    # wall-clock seconds, like CanFrame.timestamp
    value: float | None         # None = no valid reading (see flag)
    flag: str = ""              # "" = good; "OL" = over range; or a short reason ("3.1 V on probe")


@dataclass(frozen=True)
class SourceStatus:
    level: str                  # OK / WAIT / BAD / OFF / SIM
    text: str
    detail: str = ""


class MeasurementSource:
    """Base class. Subclasses set ``id`` / ``name``, return their ``channels()`` and, once
    ``start(sink)`` is called, call ``sink(Sample)`` from their own thread until ``stop()``."""

    id = "source"
    name = "Source"

    def __init__(self):
        self.status = SourceStatus(OFF, "Off")
        self._sink = None

    def channels(self) -> list[Channel]:
        return []

    def start(self, sink):
        self._sink = sink

    def stop(self):
        self._sink = None

    def emit(self, channel: str, value, t: float | None = None, flag: str = ""):
        sink = self._sink
        if sink is not None:
            sink(Sample(channel, time.time() if t is None else t, value, flag))


class MeasurementHub:
    """Every source's channels in one place, safe to push into from any thread."""

    def __init__(self, history: int = HISTORY):
        self.lock = threading.RLock()
        self._sources: dict[str, MeasurementSource] = {}
        self._latest: dict[str, Sample] = {}
        self._history: dict[str, deque] = {}
        self._history_len = history
        self._listeners: list = []
        self.sample_count = 0

    # --- sources ---
    def add_source(self, src: MeasurementSource, start: bool = True):
        self.remove_source(src.id)
        with self.lock:
            self._sources[src.id] = src
        if start:
            src.start(self.push)

    def remove_source(self, source_id: str):
        with self.lock:
            src = self._sources.pop(source_id, None)
        if src is not None:
            src.stop()

    def sources(self) -> list[MeasurementSource]:
        with self.lock:
            return list(self._sources.values())

    def stop(self):
        for src in self.sources():
            src.stop()

    def channels(self) -> list[Channel]:
        return [ch for src in self.sources() for ch in src.channels()]

    def channel(self, channel_id: str) -> Channel | None:
        return next((c for c in self.channels() if c.id == channel_id), None)

    # --- samples ---
    def push(self, sample: Sample):
        with self.lock:
            self._latest[sample.channel] = sample
            h = self._history.get(sample.channel)
            if h is None:
                h = self._history[sample.channel] = deque(maxlen=self._history_len)
            if sample.value is not None:
                h.append((sample.t, sample.value))
            self.sample_count += 1
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(sample)
            except Exception:       # a broken listener must not stop a source's thread
                pass

    def latest(self, channel_id: str) -> Sample | None:
        with self.lock:
            return self._latest.get(channel_id)

    def latest_all(self) -> dict[str, Sample]:
        with self.lock:
            return dict(self._latest)

    def history(self, channel_id: str) -> list[tuple[float, float]]:
        with self.lock:
            return list(self._history.get(channel_id, ()))

    def add_listener(self, fn):
        """Call ``fn(sample)`` for every sample, on the source's thread. Keep it quick."""
        with self.lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn):
        with self.lock:
            if fn in self._listeners:
                self._listeners.remove(fn)


# ----------------------------------------------------------------------------
# Recording
# ----------------------------------------------------------------------------
class MeasurementLog:
    """Writes every sample from a hub to a CSV while a capture records.

    Long format (one row per sample) so channels at different rates, and channels that appear
    mid-recording (a meter plugged in later), need nothing special. Rows are flushed about once
    a second, so a crash loses at most that much."""

    def __init__(self, path: str, hub: MeasurementHub, flush_every: float = 1.0):
        self.path = path
        self.hub = hub
        self.count = 0
        self.channels: dict[str, Channel] = {}
        self._lock = threading.Lock()
        self._flush_every = flush_every
        self._last_flush = time.monotonic()
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._f = open(path, "a", newline="")
        self._w = csv.writer(self._f)
        if new:
            self._w.writerow(CSV_HEADER)
        for ch in hub.channels():
            self.channels[ch.id] = ch
        hub.add_listener(self.write)

    def write(self, s: Sample):
        ch = self.channels.get(s.channel)
        if ch is None:
            ch = self.hub.channel(s.channel) or Channel(s.channel, s.channel)
            self.channels[s.channel] = ch
        value = "" if s.value is None else repr(float(s.value))
        with self._lock:
            if self._f is None:
                return
            self._w.writerow([f"{s.t:.6f}", s.channel, ch.name, value, ch.unit, s.flag])
            self.count += 1
            now = time.monotonic()
            if now - self._last_flush >= self._flush_every:
                self._f.flush()
                self._last_flush = now

    def close(self) -> int:
        self.hub.remove_listener(self.write)
        with self._lock:
            if self._f is not None:
                self._f.close()
                self._f = None
        return self.count


@dataclass
class ChannelSeries:
    channel: Channel
    points: list            # [(t, value)] in time order (value None = flagged / no reading)
    flags: list             # [(t, flag)] for samples with a flag


def read_measurements(path: str, channels: list[dict] | None = None) -> dict[str, ChannelSeries]:
    """Read a ``measurements.csv`` back: channel id -> its series. ``channels`` (from
    capture.json) supplies decimals / ranges; the CSV alone gives names and units."""
    known = {d["id"]: Channel.from_dict(d) for d in (channels or []) if "id" in d}
    out: dict[str, ChannelSeries] = {}
    try:
        f = open(path, newline="", errors="replace")
    except OSError:
        return out
    with f:
        for row in csv.DictReader(f):
            try:
                t = float(row["time"])
            except (KeyError, TypeError, ValueError):
                continue
            cid = row.get("channel") or ""
            s = out.get(cid)
            if s is None:
                ch = known.get(cid) or Channel(cid, row.get("name") or cid, row.get("unit") or "")
                s = out[cid] = ChannelSeries(ch, [], [])
            try:
                v = float(row["value"]) if row.get("value") not in (None, "") else None
            except ValueError:
                v = None
            s.points.append((t, v))
            if row.get("flag"):
                s.flags.append((t, row["flag"]))
    return out


def trim_measurements(path: str, lo: float, hi: float) -> tuple[int, int]:
    """Keep only rows with ``lo <= time <= hi`` (for ``Capture.trim``). Returns (kept, dropped)."""
    if not os.path.exists(path):
        return 0, 0
    kept = dropped = 0
    tmp = path + ".trim"
    with open(path, newline="", errors="replace") as fin, open(tmp, "w", newline="") as fout:
        r, w = csv.reader(fin), csv.writer(fout)
        for i, row in enumerate(r):
            if i == 0:
                w.writerow(row)
                continue
            try:
                t = float(row[0])
            except (IndexError, ValueError):
                continue
            if lo - 1e-6 <= t <= hi + 1e-6:      # (times are written to the microsecond)
                w.writerow(row)
                kept += 1
            else:
                dropped += 1
    os.replace(tmp, path)
    return kept, dropped


def value_at(points: list, t: float, max_age: float | None = None):
    """The latest value at or before ``t`` (binary search over time-ordered points), or None."""
    lo, hi = 0, len(points)
    while lo < hi:
        mid = (lo + hi) // 2
        if points[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    pt, v = points[lo - 1]
    if max_age is not None and t - pt > max_age:
        return None
    return v


# ----------------------------------------------------------------------------
# Clocks
# ----------------------------------------------------------------------------
class ClockMapper:
    """Map a device's own clock (e.g. a Pico's microsecond counter) onto wall-clock time.

    Each reading arrives some transport delay after it was taken; the smallest observed
    ``host_time - device_time`` is the best estimate of the offset (the reading that came
    through fastest). Keeping the minimum over a sliding window follows slow drift between the
    two crystals without letting one slow USB packet shift the timeline."""

    def __init__(self, window: float = 30.0):
        self.window = window
        self._obs: deque = deque()       # (host_t, offset)

    def reset(self):
        self._obs.clear()

    def to_host(self, device_s: float, host_t: float | None = None) -> float:
        host_t = time.time() if host_t is None else host_t
        offset = host_t - device_s
        self._obs.append((host_t, offset))
        while self._obs and host_t - self._obs[0][0] > self.window:
            self._obs.popleft()
        return device_s + min(o for _, o in self._obs)


# ----------------------------------------------------------------------------
# Live view
# ----------------------------------------------------------------------------
@dataclass(eq=False)
class ChannelSignal:
    """A measurement channel shaped like a J1939 ``Signal`` so the live views can list it."""

    channel: Channel
    spn = None
    pgn = None
    enum = False

    @property
    def key(self) -> str:
        return f"M:{self.channel.id}"

    @property
    def name(self) -> str:
        return self.channel.name

    @property
    def unit(self) -> str:
        return self.channel.unit

    @property
    def decimals(self) -> int:
        return self.channel.decimals

    @property
    def group(self) -> str:
        return "Measurements"

    @property
    def low(self):
        return self.channel.low

    @property
    def high(self):
        return self.channel.high

    def text(self, value) -> str | None:
        return None

    def out_of_range(self, value) -> bool:
        c = self.channel
        return value is not None and ((c.low is not None and value < c.low)
                                      or (c.high is not None and value > c.high))

    def accepts(self, sa) -> bool:
        return True


class MeasurementLiveData(LiveData):
    """Measurement channels as live rows (keyed (signal key, source id)), so a Live Data page
    can list and chart a probe voltage beside engine speed. ``flags`` holds the latest flag per
    row key ("OL", a warning) for the view to show instead of a number."""

    source_header = "Source"
    id_headers = ("Channel", "")

    def __init__(self):
        super().__init__()
        self._sigs: dict[str, ChannelSignal] = {}
        self._names: dict[str, str] = {}
        self.flags: dict[tuple, str] = {}

    def clear(self):
        super().clear()
        self._sigs.clear()
        self.flags.clear()

    def sample(self, hub: MeasurementHub) -> bool:
        n = len(self.order)
        latest = hub.latest_all()
        for src in hub.sources():
            self._names[src.id] = src.name
            for ch in src.channels():
                s = latest.get(ch.id)
                if s is None:
                    continue
                sig = self._sigs.get(ch.id)
                if sig is None or sig.channel != ch:
                    sig = self._sigs[ch.id] = ChannelSignal(ch)
                key = (sig.key, ch.source)
                row = self.rows.get(key)
                if row is None:
                    row = self._row(sig, ch.source, 0.0)
                    row.sig = sig
                elif row.sig is not sig:
                    row.sig = sig
                self.flags[key] = s.flag
                if s.t != row.t:
                    if s.value is None:
                        row.t, row.value = s.t, None
                    else:
                        row._update(s.t, s.value)
        return len(self.order) != n

    def load_history(self, series: dict[str, ChannelSeries]):
        """For a saved capture: every reading in its measurements.csv."""
        self.clear()
        for cid, s in series.items():
            sig = self._sigs[cid] = ChannelSignal(s.channel)
            row = self._row(sig, s.channel.source, 0.0)
            for t, v in s.points:
                if v is not None:
                    row._update(t, v)

    def source_label(self, source) -> str:
        return self._names.get(source, str(source))

    def ids(self, row) -> tuple[str, str]:
        return row.sig.channel.id, ""

    def matches(self, key, query: str = "", group: str | None = None, only_selected: bool = False) -> bool:
        r = self.rows[key]
        if only_selected and key not in self.selected:
            return False
        if group is not None and r.sig.group != group:
            return False
        q = query.strip().lower()
        return not q or q in f"{r.sig.name} {r.sig.channel.id} {r.sig.unit}".lower()


def format_csv(samples: list[Sample], channels: dict[str, Channel]) -> str:
    """Samples as measurements.csv text (tests, and exporting a snapshot)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for s in samples:
        ch = channels.get(s.channel) or Channel(s.channel, s.channel)
        w.writerow([f"{s.t:.6f}", s.channel, ch.name, "" if s.value is None else repr(float(s.value)),
                    ch.unit, s.flag])
    return buf.getvalue()
