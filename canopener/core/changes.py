"""What changed? Compare what a bus said in different states of a machine, with no decoder.

Most CAN traffic is plain: a message repeats, and each value sits in the same bytes every time.
So the quickest way to find out what an unknown message carries is to change one thing on the
machine and see which bytes follow it. This module does that comparison:

* the capture is cut into **phases** (the stretches between timestamps: "key on", "idling",
  "lifting" ...), and every message stream (a J1939 PGN from one source, or an 11-bit ID) into
  **fields**: 16-bit values where two neighbouring bytes behave like one number, single bytes
  otherwise, and single bits where a byte only ever holds a few values;
* each field is described per phase (lowest, highest, average, most common value) and by how it
  behaves (constant, a rolling counter, noise such as a checksum, a switch, a value);
* ``changes`` ranks the fields that differ most between two phases, compared with how much they
  wander inside one. Counters and noise are left out; streams that start, stop or change their
  rate are listed too.

Where a decoder already names a field (the standard J1939 tables, the active profile), the name is
shown beside it, which is also how the method is checked. Nothing here needs one, and nothing
here transmits: it works on a saved capture or a list of frames.

``analyze`` returns a ``Report``; ``Report.to_dict()`` is JSON for other tools and
``Report.lines()`` the text a person reads. Pure: no Qt, no Pygame, no I/O besides
``analyze_capture`` reading the capture it is given.
"""

import os
from collections import Counter
from dataclasses import dataclass, field
from statistics import pstdev

from ..can.logfile import read_log
from ..canopen import cob
from ..j1939.ids import parse_id
from ..j1939.signals import all_signals, id_signals

MAX_SAMPLES = 4000              # payloads kept per stream and phase (the rest are thinned evenly)
WORD_ROUGHNESS = 0.12           # two bytes are one number when it moves this smoothly (step / range)
NOISE_ROUGHNESS = 0.22          # a byte this jumpy with many values: a checksum, or random
COUNTER_SHARE = 0.9             # this share of steps equal: a rolling counter
SWITCH_VALUES = 4               # a byte with no more values than this is looked at bit by bit
SERIES_STEP = 0.5               # seconds per slot when a value is followed over time
FOLLOWS = 0.97                  # correlation above which one value is said to follow another
MIN_SCORE = 3.0                 # changes scoring less than this aren't listed
BEFORE = "Before the first timestamp"

CONSTANT, COUNTER, NOISE, SWITCH, VALUE = "constant", "counter", "noise", "switch", "value"


@dataclass
class Phase:
    label: str
    start: float
    end: float


@dataclass
class PhaseStats:
    n: int = 0
    low: float = 0.0
    high: float = 0.0
    mean: float = 0.0
    spread: float = 0.0         # standard deviation
    mode: int = 0               # the most common raw value

    def to_dict(self) -> dict:
        return {"n": self.n, "low": self.low, "high": self.high, "mean": round(self.mean, 3),
                "spread": round(self.spread, 3), "mode": self.mode}


@dataclass
class Field:
    """One value inside a stream. ``where`` is 'b3' (a byte), 'w2le' / 'w2be' (16 bits starting at
    byte 2, low or high byte first) or 'b3.5' (bit 5 of byte 3; bit 0 is the lowest)."""

    where: str
    kind: str                   # constant / counter / noise / switch / value
    by_phase: list              # PhaseStats per phase (n = 0 where the stream was silent)
    known: str = ""             # a decoder's name for these bytes, when one covers them
    values: int = 0             # how many different raw values over the whole capture
    series: dict = field(default_factory=dict, repr=False)     # value fields: time slot -> mean (for ``relations``)
    scale: tuple = ()           # (scale, offset, unit) of the decoder's signal, when ``known`` is a number

    def to_dict(self) -> dict:
        return {"where": self.where, "kind": self.kind, "known": self.known, "values": self.values,
                "by_phase": [p.to_dict() for p in self.by_phase]}


@dataclass
class Stream:
    kind: str                   # "j1939" / "11-bit"
    ident: int                  # the PGN, or the 11-bit ID
    sa: int | None
    label: str                  # "PGN FEF1 from 00", "ID 181"
    name: str                   # what a decoder calls the message, "" when unknown
    frames: int
    dlc: int
    rate_by_phase: list         # frames per second in each phase
    fields: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.ident, "sa": self.sa, "label": self.label, "name": self.name,
                "frames": self.frames, "dlc": self.dlc, "rate_by_phase": [round(r, 2) for r in self.rate_by_phase],
                "fields": [f.to_dict() for f in self.fields if f.kind != CONSTANT]}


@dataclass
class Change:
    stream: Stream
    field: Field | None         # None: the stream itself started, stopped or changed rate
    a: int                      # phase indexes
    b: int
    score: float
    text: str

    def to_dict(self) -> dict:
        return {"stream": self.stream.label, "name": self.stream.name, "field": self.field.where if self.field else "",
                "known": self.field.known if self.field else "", "from_phase": self.a, "to_phase": self.b,
                "score": round(self.score, 1), "text": self.text}


@dataclass
class Relation:
    """``field`` moves with ``other``: other's raw number is about ``slope`` x field's + ``offset``."""

    stream: Stream
    field: Field
    other_stream: Stream
    other: Field
    r: float
    slope: float
    offset: float

    def formula(self) -> tuple[float, float, str]:
        """(k, c, unit): the other value, in its own units when a decoder scales it, = k x raw + c."""
        k, c, unit = self.slope, self.offset, ""
        if self.other.scale:
            scale, offset, unit = self.other.scale
            k, c = k * scale, c * scale + offset
        return k, c, unit

    def text(self) -> str:
        who = self.other.known or f"{self.other_stream.label} {self.other.where}"
        k, c, unit = self.formula()
        what = f"{who} ({unit})" if unit else (who if self.other.scale else f"{who}'s raw number")
        tail = "" if abs(c) < 0.005 * max(1.0, abs(k)) else f" {'+' if c >= 0 else '-'} {abs(c):.4g}"
        return f"{self.stream.label} {self.field.where} follows {who}: {what} = {k:.4g} x this{tail}"

    def to_dict(self) -> dict:
        return {"stream": self.stream.label, "field": self.field.where, "follows_stream": self.other_stream.label,
                "follows_field": self.other.where, "follows_known": self.other.known, "r": round(self.r, 4),
                "slope": self.slope, "offset": self.offset,
                "formula": dict(zip(("scale", "offset", "unit"), self.formula()))}


def _fit(a: dict[int, float], b: dict[int, float]):
    """(r, slope, offset) with b = slope * a + offset over the time slots both have, or None if
    they don't move together."""
    keys = a.keys() & b.keys()
    if len(keys) < 20:
        return None
    xs, ys = [a[k] for k in keys], [b[k] for k in keys]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx < len(xs) or syy < len(ys):              # one of them barely moved
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    r = sxy / (sxx * syy) ** 0.5
    if abs(r) < FOLLOWS:
        return None
    slope = sxy / sxx
    return r, slope, my - slope * mx


@dataclass
class Report:
    phases: list
    streams: list
    frames: int = 0

    def changes(self, a: int, b: int, limit: int = 40) -> list[Change]:
        """What differs between phase ``a`` and phase ``b``, most marked first."""
        out = []
        for s in self.streams:
            ra, rb = s.rate_by_phase[a], s.rate_by_phase[b]
            if is_housekeeping(s.kind, s.ident, s.sa):
                continue                            # asked-for answers come and go with the asking
            if (ra == 0) != (rb == 0):
                out.append(Change(s, None, a, b, 35.0, "started" if ra == 0 else "stopped"))
            elif ra and rb and (ra / rb > 2 or rb / ra > 2):
                out.append(Change(s, None, a, b, 8.0, f"rate {ra:.1f} -> {rb:.1f} per second"))
            for f in s.fields:
                c = _field_change(s, f, a, b)
                if c is not None:
                    out.append(c)
        out = _merge_bits(out, a, b)
        out.sort(key=lambda c: (-c.score, c.stream.label, c.field.where if c.field else ""))
        return out[:limit]

    def relations(self) -> list["Relation"]:
        """Unnamed values that move with a named one (or with each other): the quickest clue to
        what an unknown value is and how it's scaled."""
        fields = [(s, f) for s in self.streams for f in s.fields if f.kind == VALUE and len(f.series) >= 20]
        out = []
        for s, f in fields:
            if f.known:
                continue
            best = None
            for s2, g in fields:
                if g is f or (not g.known and (s2.label, g.where) <= (s.label, f.where)):
                    continue                        # named ones first; unnamed pairs once
                fit = _fit(f.series, g.series)
                if fit and (best is None or (bool(g.known), abs(fit[0])) > (bool(best[1].known), abs(best[2][0]))):
                    best = (s2, g, fit)
            if best:
                out.append(Relation(s, f, best[0], best[1], *best[2]))
        out.sort(key=lambda r: (not r.other.known, -abs(r.r)))
        return out

    def to_dict(self) -> dict:
        pairs = [(i, i + 1) for i in range(len(self.phases) - 1)]
        return {"frames": self.frames,
                "phases": [{"label": p.label, "start": p.start, "end": p.end} for p in self.phases],
                "streams": [s.to_dict() for s in self.streams],
                "changes": [c.to_dict() for a, b in pairs for c in self.changes(a, b)],
                "relations": [r.to_dict() for r in self.relations()]}

    def lines(self, limit: int = 15) -> list[str]:
        out = [f"{self.frames} frames, {len(self.streams)} message streams, {len(self.phases)} phase(s)."]
        kinds = Counter(f.kind for s in self.streams for f in s.fields)
        order = (VALUE, SWITCH, COUNTER, NOISE, CONSTANT)
        out.append("Fields: " + ", ".join(f"{kinds[k]} {k}" for k in order if kinds[k]))
        for i in range(len(self.phases) - 1):
            a, b = self.phases[i], self.phases[i + 1]
            out += ["", f"== {a.label}  ->  {b.label} =="]
            found = self.changes(i, i + 1, limit)
            if not found:
                out.append("  nothing changed that stands out")
            for c in found:
                where = f" {c.field.where}" if c.field else ""
                name = c.field.known if c.field and c.field.known else c.stream.name
                out.append(f"  {c.stream.label + where:<26} {c.text:<34} {name}")
        related = self.relations()
        if related:
            out += ["", "== Values that move together =="] + [f"  {r.text()}" for r in related[:limit]]
        return out


# ----------------------------------------------------------------------
# phases
# ----------------------------------------------------------------------
def phases_from_markers(markers: list[tuple[float, str]], start: float, end: float) -> list[Phase]:
    """[(time, label)] -> the stretch before the first timestamp, then one phase per timestamp."""
    marks = sorted((t, label) for t, label in markers if start <= t <= end)
    if not marks:
        return [Phase("The whole capture", start, end)]
    out = [Phase(BEFORE, start, marks[0][0])] if marks[0][0] - start > 0.5 else []
    for i, (t, label) in enumerate(marks):
        out.append(Phase(label, t, marks[i + 1][0] if i + 1 < len(marks) else end))
    return out


def even_phases(start: float, end: float, n: int) -> list[Phase]:
    """``n`` equal slices, for a capture without timestamps."""
    step = (end - start) / n if n and end > start else 0.0
    return [Phase(f"Part {i + 1} of {n}", start + i * step, start + (i + 1) * step if i + 1 < n else end)
            for i in range(n)]


# ----------------------------------------------------------------------
# the analysis
# ----------------------------------------------------------------------
def _stats(values: list[int]) -> PhaseStats:
    if not values:
        return PhaseStats()
    return PhaseStats(len(values), min(values), max(values), sum(values) / len(values),
                      pstdev(values) if len(values) > 1 else 0.0, Counter(values).most_common(1)[0][0])


def _roughness(seq: list[int]) -> float:
    """Mean step between neighbours over the range: near 0 for a smooth value, about 1/3 for noise."""
    span = max(seq) - min(seq)
    if span == 0 or len(seq) < 2:
        return 0.0
    return sum(abs(seq[i + 1] - seq[i]) for i in range(len(seq) - 1)) / (len(seq) - 1) / span


def _carries(word: list[int], high: list[int]) -> bool:
    """Do two bytes behave as one number? When the high byte steps, a real 16-bit value has only
    moved a little (the low byte wrapped); two separate values jump by about 256 a step."""
    steps = [abs(word[i + 1] - word[i]) for i in range(len(word) - 1) if high[i + 1] != high[i]]
    return len(steps) >= 3 and sum(1 for d in steps if d < 128) / len(steps) >= 0.8


def _is_counter(seq: list[int], modulus: int) -> bool:
    if len(seq) < 8:
        return False
    steps = Counter((seq[i + 1] - seq[i]) % modulus for i in range(len(seq) - 1))
    step, count = steps.most_common(1)[0]
    return step != 0 and count / (len(seq) - 1) >= COUNTER_SHARE


def _byte_kind(seq: list[int]) -> str:
    distinct = len(set(seq))
    if distinct <= 1:
        return CONSTANT
    if _is_counter(seq, 256) or (distinct <= 16 and (_is_counter([v & 0x0F for v in seq], 16)
                                                     or _is_counter([v >> 4 for v in seq], 16))):
        return COUNTER
    if distinct <= SWITCH_VALUES:
        # three or four neighbouring numbers (40, 41, 42) are a slow value, not flags
        return VALUE if distinct >= 3 and max(seq) - min(seq) == distinct - 1 else SWITCH
    if distinct > 32 and _roughness(seq) > NOISE_ROUGHNESS:
        return NOISE
    return VALUE


def _thin(items: list, limit: int = MAX_SAMPLES) -> list:
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def is_housekeeping(kind: str, ident: int, sa) -> bool:
    """Messages that carry the conversation, not the machine's values: a scan tool's own frames,
    J1939 requests / acknowledgements / transport / address claims and the codes and info they
    fetch, CANopen network management, SDO and heartbeats, OBD-II requests and answers."""
    from .identity import OBD_IDS, STRUCTURAL_KINDS, STRUCTURAL_PGNS
    if kind == "j1939":
        return ident in STRUCTURAL_PGNS or (sa is not None and sa >= 0xF8)
    return ident in OBD_IDS or cob.classify(ident)[0] in STRUCTURAL_KINDS


def _known_names(kind: str, ident: int, sa) -> tuple[dict, dict]:
    """{(first byte, bytes long): a decoder's name} for whole-byte signals ((byte, 0) for bit fields),
    and {the same key: (scale, offset, unit)} for the ones that are numbers."""
    sigs = [s for s in all_signals() if s.can_id is None and s.pgn == ident and (sa is None or s.accepts(sa))] \
        if kind == "j1939" else list(id_signals().get(ident, ()))
    out, scales = {}, {}
    for s in sigs:
        key = (s.start, 0 if s.bits else s.length)
        out[key] = (out[key] + " / " if key in out else "") + s.name
        if not s.bits and not s.is_text and not s.enum:
            scales[key] = (s.scale, s.offset, s.unit)
    return out, scales


def _series(times: list[float], values: list[int], t0: float) -> dict[int, float]:
    """A value over time, averaged in SERIES_STEP slots (slot number -> mean)."""
    sums: dict[int, list] = {}
    for t, v in zip(times, values):
        slot = sums.setdefault(int((t - t0) / SERIES_STEP), [0, 0])
        slot[0] += v
        slot[1] += 1
    return {k: a / n for k, (a, n) in sums.items()}


def _stream(kind: str, ident: int, sa, per_phase: list, phases: list[Phase], total: int, t0: float = 0.0) -> Stream:
    from ..j1939.addresses import sa_name
    from ..j1939.constants import pgn_label
    if kind == "j1939":
        label, name = f"PGN {ident:04X} from {sa:02X}", pgn_label(ident)
        src = sa_name(sa)
        name = f"{name} ({src})" if name else src
    else:
        what, _node = cob.classify(ident)
        label, name = f"ID {ident:03X}", cob.KIND_LABELS.get(what, "") if what else ""
    rates = [len(p) / (ph.end - ph.start) if ph.end > ph.start else 0.0 for p, ph in zip(per_phase, phases)]
    sampled = [_thin(p) for p in per_phase]
    lengths = Counter(len(d) for p in sampled for _t, d in p)
    dlc = lengths.most_common(1)[0][0] if lengths else 0
    s = Stream(kind, ident, sa, label, name, total, dlc, rates)
    if is_housekeeping(kind, ident, sa):
        return s                                    # requests, transport, network management: no values inside
    known, scales = _known_names(kind, ident, sa)

    times = [t for p in sampled for t, d in p if len(d) >= dlc]

    def column(get) -> tuple[list[int], list[list[int]]]:
        cols = [[get(d) for _t, d in p if len(d) >= dlc] for p in sampled]
        return [v for c in cols for v in c], cols

    kinds, columns = {}, {}
    for i in range(dlc):
        seq, cols = column(lambda d, i=i: d[i])
        kinds[i], columns[i] = (_byte_kind(seq) if seq else CONSTANT), (seq, cols)
    used = set()
    words = []
    for i in range(dlc - 1):                    # two bytes that move as one number
        if CONSTANT in (kinds[i], kinds[i + 1]) or COUNTER in (kinds[i], kinds[i + 1]):
            continue
        le, le_cols = column(lambda d, i=i: d[i] | (d[i + 1] << 8))
        be, be_cols = column(lambda d, i=i: (d[i] << 8) | d[i + 1])
        r_le, r_be = _roughness(le), _roughness(be)
        little = r_le <= r_be
        word, high = (le, columns[i + 1][0]) if little else (be, columns[i][0])
        named = known.get((i, 2))
        apart = known.get((i, 1)) or known.get((i + 1, 1))      # a decoder says they're two values
        if named or (not apart and min(r_le, r_be) < WORD_ROUGHNESS and _carries(word, high)):
            words.append((min(r_le, r_be), i, "le" if little else "be", (le, le_cols) if little else (be, be_cols)))
    for _rough, i, order, (seq, cols) in sorted(words):
        if i in used or i + 1 in used:
            continue
        used |= {i, i + 1}
        s.fields.append(Field(f"w{i}{order}", VALUE, [_stats(c) for c in cols], known.get((i, 2), ""), len(set(seq)),
                              _series(times, seq, t0), scales.get((i, 2), ())))
    for i in range(dlc):
        if i in used:
            continue
        seq, cols = columns[i]
        f = Field(f"b{i}", kinds[i], [_stats(c) for c in cols], known.get((i, 1), ""), len(set(seq)),
                  _series(times, seq, t0) if kinds[i] == VALUE else {}, scales.get((i, 1), ()))
        s.fields.append(f)
        if kinds[i] == SWITCH or (kinds[i] == VALUE and known.get((i, 0))):
            varying = 0
            for v in set(seq):
                varying |= v ^ seq[0]
            for bit in range(8):
                if varying >> bit & 1:
                    bcols = [[v >> bit & 1 for v in c] for c in cols]
                    s.fields.append(Field(f"b{i}.{bit}", SWITCH, [_stats(c) for c in bcols], known.get((i, 0), ""), 2))
    s.fields.sort(key=lambda f: (int("".join(ch for ch in f.where.split(".")[0] if ch.isdigit())), f.where))
    return s


def _merge_bits(found: list[Change], a: int, b: int) -> list[Change]:
    """Three or more bits of one byte changing together read better as the byte: '40 -> 03'."""
    groups: dict[tuple, list[Change]] = {}
    for c in found:
        if c.field is not None and "." in c.field.where:
            groups.setdefault((id(c.stream), c.field.where.split(".")[0]), []).append(c)
    drop = set()
    extra = []
    for (_sid, where), bits in groups.items():
        if len(bits) < 3:
            continue
        s = bits[0].stream
        byte = next((f for f in s.fields if f.where == where), None)
        if byte is None:
            continue
        drop |= {id(c) for c in bits}
        pa, pb = byte.by_phase[a], byte.by_phase[b]
        extra.append(Change(s, byte, a, b, max(c.score for c in bits),
                            f"{pa.mode:02X} -> {pb.mode:02X}  ({len(bits)} bits)"))
    return [c for c in found if id(c) not in drop] + extra


def _field_change(s: Stream, f: Field, a: int, b: int) -> Change | None:
    if f.kind in (CONSTANT, COUNTER, NOISE):
        return None
    pa, pb = f.by_phase[a], f.by_phase[b]
    if not pa.n or not pb.n:
        return None
    if "." in f.where:                              # one bit: how often it was set
        if abs(pa.mean - pb.mean) < 0.5:
            return None
        return Change(s, f, a, b, 10 + 20 * abs(pa.mean - pb.mean),
                      f"{'on' if pa.mean >= 0.5 else 'off'} -> {'on' if pb.mean >= 0.5 else 'off'}")
    if f.kind == SWITCH:
        if pa.mode == pb.mode or any(x.where.startswith(f.where + ".") for x in s.fields):
            return None                             # its bits tell it better
        return Change(s, f, a, b, 12.0, f"{pa.mode:02X} -> {pb.mode:02X}")
    diff = abs(pa.mean - pb.mean)
    if diff < 1 or diff / max(pa.spread, pb.spread, 0.5) < MIN_SCORE:
        return None                                 # within what it wanders anyway
    span = max(p.high for p in f.by_phase if p.n) - min(p.low for p in f.by_phase if p.n)
    score = 10 + 20 * min(1.0, diff / span) if span else 10.0      # same scale as a bit: 10 to 30
    return Change(s, f, a, b, score, f"{pa.mean:.0f} -> {pb.mean:.0f}  (was {pa.low:.0f}-{pa.high:.0f}, "
                                     f"now {pb.low:.0f}-{pb.high:.0f})")


def analyze(frames: list, phases: list[Phase]) -> Report:
    """``frames``: CanFrame-like objects (can_id, extended, data, timestamp, error) in time order."""
    n = len(phases)
    streams: dict[tuple, list[list[bytes]]] = {}
    totals: Counter = Counter()
    count = 0
    for fr in frames:
        if getattr(fr, "error", None):
            continue
        t = fr.timestamp
        idx = next((i for i, p in enumerate(phases) if p.start <= t < p.end or (i == n - 1 and t == p.end)), None)
        if idx is None:
            continue
        count += 1
        if fr.extended:
            j = parse_id(fr.can_id)
            key = ("j1939", j.pgn, j.sa)
        else:
            key = ("11-bit", fr.can_id, None)
        streams.setdefault(key, [[] for _ in range(n)])[idx].append((t, bytes(fr.data)))
        totals[key] += 1
    t0 = phases[0].start if phases else 0.0
    out = [_stream(kind, ident, sa, per_phase, phases, totals[(kind, ident, sa)], t0)
           for (kind, ident, sa), per_phase in streams.items()]
    out.sort(key=lambda s: (s.kind, s.ident, s.sa or 0))
    return Report(phases, out, count)


def analyze_capture(folder_or_log: str, parts: int = 0) -> Report:
    """A saved capture folder (its timestamps are the phases) or a bare log file. ``parts`` cuts a
    capture without timestamps into that many equal slices instead of one phase."""
    markers = []
    path = folder_or_log
    if os.path.isdir(folder_or_log):
        from .capture import Capture
        c = Capture.load(folder_or_log)
        path = c.log_path
        markers = [(m.t, c.title(i)) for i, m in c.tagged()]
    frames = [f for f in read_log(path) if not f.error]
    if not frames:
        return Report([], [], 0)
    start, end = frames[0].timestamp, frames[-1].timestamp
    phases = phases_from_markers(markers, start, end) if markers or not parts else even_phases(start, end, parts)
    return analyze(frames, phases)
