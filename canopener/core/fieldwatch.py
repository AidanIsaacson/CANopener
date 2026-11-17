"""Every field on the bus, live: the current value, a short history and how it behaves.

``core.changes`` cuts a capture into phases and says what differs between them. This keeps the
same view of a bus that is running now: each message stream split into bytes, 16-bit numbers and
bits (the same ``where`` names: ``b3``, ``w2le``, ``b3.5``), with

* its value now (``value``), and the values it had over the last minutes (``series``),
* how often it changed, in any stretch of time (``transitions``), and when it last did,
* what it *is*, judged from the whole window rather than a few seconds (``refresh``): a byte that
  only wobbles between neighbouring numbers is a reading (a voltage, a temperature), not eight
  switches, so its bits are never offered as switches (``plausible``),
* the name a decoder (the profile in use, or the standard tables) gives it, when it has one.

The Profile builder (``core.guide``) asks it which of its candidates are believable and which
moved while the technician worked a control; the bus explorer lists every field from it. Fed
from the receive thread (``feed``), read from the UI thread; ``refresh`` is the heavy part and
can run on its own thread (``refresh_soon``). No Qt, no Pygame.
"""

import re
import threading
import time
from collections import deque
from dataclasses import dataclass

from ..j1939.ids import parse_id
from . import changes

WINDOW = 120.0              # seconds of history kept per stream
KEEP_EVERY = 0.05           # an unchanged payload is kept at most this often (every change is kept)
MAX_SAMPLES = 4000          # payloads kept per stream (oldest dropped first)
REFRESH_EVERY = 3.0         # seconds between re-judging the fields (``refresh_soon``)
MOVES_KEPT = 64             # change times remembered per byte and per bit, for 'moving now'
NUMERIC_STEP = 2            # neighbouring readings: a byte that mostly steps by this little is a number

_WHERE = re.compile(r"w(\d+)(le|be)|b(\d+)(?:\.(\d))?$")


@dataclass
class FieldInfo:
    """One field as last judged by ``refresh``."""

    key: str                # "PGN FF04 from 27 b1.2": the stream's label and the field's place
    stream: tuple           # ("j1939", pgn, sa) or ("11-bit", can_id, None)
    label: str              # "PGN FF04 from 27"
    name: str               # what the message is, when known ("Proprietary B (Management computer #1)")
    where: str              # "b1.2"
    kind: str               # changes.CONSTANT / COUNTER / NOISE / SWITCH / VALUE
    known: str = ""         # a decoder's name for it ("" = unnamed)
    scale: tuple = ()       # (scale, offset, unit) when ``known`` is a number

    @property
    def is_bit(self) -> bool:
        return "." in self.where

    def describe(self) -> str:
        """In words, for a technician: 'PGN FF04 from 27, byte 2 bit 3' (counted from 1)."""
        return f"{self.label}, {place_words(self.where)}"


def place_words(where: str) -> str:
    """'b1.2' -> 'byte 2 bit 3', 'w2le' -> 'bytes 3-4', 'b0' -> 'byte 1' (counted from 1, like J1939 documents)."""
    m = _WHERE.match(where)
    if not m:
        return where
    if m.group(1) is not None:
        i = int(m.group(1))
        return f"bytes {i + 1}-{i + 2}" + (" (high byte first)" if m.group(2) == "be" else "")
    i = int(m.group(3))
    return f"byte {i + 1}" + (f" bit {int(m.group(4)) + 1}" if m.group(4) is not None else "")


def extract(where: str, data: bytes) -> int | None:
    """The raw value of a field from a payload (None when the payload is too short)."""
    m = _WHERE.match(where)
    if not m:
        return None
    if m.group(1) is not None:
        i = int(m.group(1))
        if len(data) < i + 2:
            return None
        return data[i] | data[i + 1] << 8 if m.group(2) == "le" else data[i] << 8 | data[i + 1]
    i = int(m.group(3))
    if len(data) <= i:
        return None
    return data[i] >> int(m.group(4)) & 1 if m.group(4) is not None else data[i]


def split_key(key: str) -> tuple[str, str]:
    """'PGN FF04 from 27 b1.2' -> ('PGN FF04 from 27', 'b1.2')."""
    label, _, where = key.rpartition(" ")
    return label, where


class _Stream:
    __slots__ = ("key", "label", "samples", "latest", "latest_t", "count", "first_t", "last_kept", "moves")

    def __init__(self, key: tuple, label: str):
        self.key, self.label = key, label
        self.samples: deque = deque(maxlen=MAX_SAMPLES)     # (t, bytes)
        self.latest, self.latest_t = b"", 0.0
        self.count, self.first_t, self.last_kept = 0, 0.0, -1e18
        self.moves: dict = {}       # (byte, bit or None) -> deque of the times it changed (cheap 'moving now')

    def note_moves(self, old: bytes, new: bytes, t: float):
        for i in range(min(len(old), len(new))):
            x = old[i] ^ new[i]
            if not x:
                continue
            self.moves.setdefault((i, None), deque(maxlen=MOVES_KEPT)).append(t)
            for bit in range(8):
                if x >> bit & 1:
                    self.moves.setdefault((i, bit), deque(maxlen=MOVES_KEPT)).append(t)


def stream_key(can_id: int, extended: bool) -> tuple:
    if extended:
        j = parse_id(can_id)
        return ("j1939", j.pgn, j.sa)
    return ("11-bit", can_id, None)


def stream_label(key: tuple) -> str:
    kind, ident, sa = key
    return f"PGN {ident:04X} from {sa:02X}" if kind == "j1939" else f"ID {ident:03X}"


class FieldWatch:
    """The live view of every field on one bus."""

    def __init__(self, window: float = WINDOW, clock=time.time):
        self.window, self.clock = window, clock
        self._lock = threading.Lock()
        self._streams: dict[tuple, _Stream] = {}
        self._keys: dict[tuple, tuple] = {}             # (can_id, extended) -> stream key
        self._by_label: dict[str, tuple] = {}
        self._fields: dict[str, FieldInfo] = {}
        self._report: changes.Report | None = None
        self._judged = 0.0                               # when ``refresh`` last finished (0 = never)
        self._busy = False
        self.frames = 0

    # --- frames in (the receive thread) ---
    def feed(self, frame):
        if getattr(frame, "error", None) or getattr(frame, "tx", False):
            return
        ident = (frame.can_id, bool(frame.extended))
        key = self._keys.get(ident)
        if key is None:
            key = self._keys[ident] = stream_key(frame.can_id, frame.extended)
        t, data = frame.timestamp, bytes(frame.data)
        with self._lock:
            s = self._streams.get(key)
            if s is None:
                s = self._streams[key] = _Stream(key, stream_label(key))
                self._by_label[s.label] = key
                s.first_t = t
            s.count += 1
            self.frames += 1
            if data != s.latest and s.latest:
                s.note_moves(s.latest, data, t)
            if data != s.latest or t - s.last_kept >= KEEP_EVERY:
                s.samples.append((t, data))
                s.last_kept = t
            s.latest, s.latest_t = data, t

    def clear(self):
        with self._lock:
            self._streams.clear()
            self._by_label.clear()
            self._fields, self._report, self._judged = {}, None, 0.0
            self.frames = 0

    # --- judging the fields (the heavy part) ---
    def refresh(self, now: float | None = None) -> None:
        """Split every stream into fields again and judge each from the whole window."""
        now = self.clock() if now is None else now
        cut = now - self.window
        with self._lock:
            for s in self._streams.values():
                while s.samples and s.samples[0][0] < cut:
                    s.samples.popleft()
            snap = [(s.key, list(s.samples), s.count) for s in self._streams.values() if s.samples]
        start = min((smp[0][0] for _k, smp, _n in snap), default=now)
        phase = [changes.Phase("window", start, now + 1)]
        streams, fields = [], {}
        for (kind, ident, sa), samples, count in snap:
            st = changes._stream(kind, ident, sa, [samples], phase, count, start)
            streams.append(st)
            for f in st.fields:
                key = f"{st.label} {f.where}"
                fields[key] = FieldInfo(key, (kind, ident, sa), st.label, st.name, f.where, f.kind, f.known, f.scale)
        report = changes.Report(phase, streams, sum(len(smp) for _k, smp, _n in snap))
        with self._lock:
            self._fields, self._report, self._judged = fields, report, self.clock()

    def refresh_soon(self, every: float = REFRESH_EVERY) -> bool:
        """``refresh`` on a background thread when the last one is older than ``every`` seconds
        (a UI calls this as often as it likes). True when one was started."""
        if self._busy or (self._judged and self.clock() - self._judged < every):
            return False
        self._busy = True

        def work():
            try:
                self.refresh()
            finally:
                self._busy = False
        threading.Thread(target=work, name="fieldwatch", daemon=True).start()
        return True

    @property
    def judged(self) -> float:
        return self._judged

    # --- reading it (any thread) ---
    def fields(self) -> list[FieldInfo]:
        with self._lock:
            return list(self._fields.values())

    def info(self, key: str) -> FieldInfo | None:
        with self._lock:
            return self._fields.get(key)

    def report(self) -> changes.Report | None:
        return self._report

    def streams(self) -> list[tuple]:
        """[(stream key, frames, frames a second over the window)]."""
        now = self.clock()
        with self._lock:
            out = []
            for s in self._streams.values():
                span = max(1e-6, min(self.window, (s.latest_t or now) - s.first_t))
                n = sum(1 for t, _d in s.samples if t >= s.latest_t - span)
                out.append((s.key, s.count, n / span if s.count > 1 else 0.0))
            return out

    def _stream_of(self, key: str):
        label, where = split_key(key)
        sk = self._by_label.get(label)
        return (self._streams.get(sk) if sk else None), where

    def value(self, key: str) -> int | None:
        """The field's raw value in the latest frame of its stream."""
        with self._lock:
            s, where = self._stream_of(key)
            return extract(where, s.latest) if s is not None and s.latest else None

    def age(self, key: str) -> float | None:
        """Seconds since its stream was last heard (None if never)."""
        with self._lock:
            s, _w = self._stream_of(key)
            return None if s is None else self.clock() - s.latest_t

    def series(self, key: str, since: float | None = None) -> list[tuple[float, int]]:
        """[(time, raw value)] over the window (or since ``since``)."""
        with self._lock:
            s, where = self._stream_of(key)
            samples = list(s.samples) if s is not None else []
        out = []
        for t, d in samples:
            if since is not None and t < since:
                continue
            v = extract(where, d)
            if v is not None:
                out.append((t, v))
        return out

    def transitions(self, key: str, since: float | None = None, intervals=None) -> int:
        """How many times the value changed (since ``since``, or only inside ``intervals``:
        [(start, end)])."""
        n, last = 0, None
        for t, v in self.series(key):
            if last is not None and v != last:
                if since is not None and t < since:
                    pass
                elif intervals is not None and not any(a <= t <= b for a, b in intervals):
                    pass
                else:
                    n += 1
            last = v
        return n

    def held_transitions(self, key: str, since: float, hold: float = 0.3) -> int:
        """Changes since ``since`` after which the new value stayed at least ``hold`` seconds: a
        person working a switch holds each position; a contact that bounces doesn't."""
        changes_at, last = [], None
        for t, v in self.series(key):
            if last is not None and v != last and t >= since:
                changes_at.append(t)
            last = v
        ends = changes_at[1:] + [self.clock()]
        return sum(1 for a, b in zip(changes_at, ends) if b - a >= hold)

    def _move_lists(self, key: str) -> list:
        s, where = self._stream_of(key)
        m = _WHERE.match(where)
        if s is None or not m:
            return []
        if m.group(1) is not None:
            i = int(m.group(1))
            return [s.moves.get((i, None)), s.moves.get((i + 1, None))]
        bit = None if m.group(4) is None else int(m.group(4))
        return [s.moves.get((int(m.group(3)), bit))]

    def last_change(self, key: str) -> float | None:
        """When the field last changed (None if not since it was first heard). Cheap: for every row
        on a screen, every frame."""
        with self._lock:
            times = [q[-1] for q in self._move_lists(key) if q]
        return max(times) if times else None

    def recent_changes(self, key: str, seconds: float = 10.0) -> int:
        """Changes in the last ``seconds`` (counted from a short memory: at most MOVES_KEPT)."""
        cut = self.clock() - seconds
        with self._lock:
            lists = [list(q) for q in self._move_lists(key) if q]
        return len({t for q in lists for t in q if t >= cut})

    def stream_field(self, key: str):
        """(changes.Stream, changes.Field) for a field, from the last ``refresh`` (or (None, None))."""
        label, where = split_key(key)
        r = self._report
        for st in (r.streams if r is not None else []):
            if st.label == label:
                return st, next((f for f in st.fields if f.where == where), None)
        return None, None

    def numeric_byte(self, label: str, index: int) -> bool:
        """Does byte ``index`` of the stream move like a number (a reading that wobbles or creeps
        between neighbouring values), rather than like a set of flags?"""
        with self._lock:
            sk = self._by_label.get(label)
            s = self._streams.get(sk) if sk else None
            samples = list(s.samples) if s is not None else []
        vals = [d[index] for _t, d in samples if len(d) > index]
        if len(set(vals)) < 3:
            return False
        steps = [abs(b - a) for a, b in zip(vals, vals[1:]) if b != a]
        return bool(steps) and sum(1 for d in steps if d <= NUMERIC_STEP) / len(steps) >= 0.8

    def plausible(self, key: str) -> bool:
        """Could this field really be a control's own signal? Not when it's a bit of a byte that
        moves like a number (the lowest digits of a voltage or a temperature), a byte that's half
        of a 16-bit number, a counter or noise. Fields not judged yet are given the benefit of the
        doubt."""
        label, where = split_key(key)
        m = _WHERE.match(where)
        if not m:
            return False
        with self._lock:
            mine = self._fields.get(key)
            words = [f.where for f in self._fields.values() if f.label == label and f.where.startswith("w")]
        if mine is not None and mine.kind in (changes.COUNTER, changes.NOISE):
            return False
        if m.group(3) is not None:
            i = int(m.group(3))
            if any(int(w[1:-2]) in (i, i - 1) for w in words):
                return False                        # half of a 16-bit number: the number is the field
            if m.group(4) is not None and self.numeric_byte(label, i):
                return False
        return True
