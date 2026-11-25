"""Values other profiles already know: undecoded traffic matched against every profile on the unit.

A profile is someone's knowledge of a machine: a built-in one, a plugin's, or one the Profile
builder made on this or another unit. Machines of one family share their messages (the same
CANopen PDOs on the same COB-IDs, the same proprietary PGNs), so a stream nothing decodes here is
often decoded by a profile made for a sister machine. This finds those, and checks each borrowed
value against the data actually on the bus before offering it:

* the field must fit the frames (a 6-byte PDO can't hold a value in bytes 6-7),
* an enum must cover the raw values seen, and a value with a normal range (``low`` / ``high``)
  must mostly sit in or near it,
* a value that never moves is consistent but tells little; a value that moves and stays sane is
  good evidence; one bad value costs more than one good value earns.

``suggest(samples, candidates, active)`` returns ``Borrowed`` groups, one per stream, best first:
the profile entries to add (``entries``, in profile-document form with an ``"observed"`` note of
where they came from), and ``hints()`` maps field keys (``core.fieldwatch``'s ``"ID 1A2 b3"``) to
the borrowed name, for the guided builder's offers.

Pure, standard library, bounded work (at most ``MAX_SAMPLES`` payloads per stream), so it runs on
the handheld. Nothing is sent to the machine. No Qt, no Pygame.
"""

from dataclasses import dataclass, field

from ..j1939 import signals as sigtable
from . import profiles

MAX_SAMPLES = 400           # payloads looked at per stream (spread over the window)
RANGE_SLACK = 0.1           # how far outside low..high (as a share of the range) still counts as in it
GOOD_SHARE = 0.9            # share of samples that must be sane for a value to count as consistent
MIN_SCORE = 1.0             # a stream needs at least this much evidence to be offered


@dataclass
class Check:
    """One borrowed value, judged against the bus."""

    signal: sigtable.Signal
    ok: bool
    moved: bool
    note: str = ""

    @property
    def weight(self) -> float:
        if not self.ok:
            return -2.0
        if self.signal.bits == 1:
            return 0.3 if self.moved else 0.1           # a bit fits anything: weak evidence
        return 1.0 if self.moved else 0.4


@dataclass
class Borrowed:
    """The values one profile knows for one undecoded stream on this bus."""

    stream: tuple                   # ("11-bit", can_id, None) or ("j1939", pgn, sa)
    profile_id: str
    profile_name: str
    checks: list = field(default_factory=list)
    coverage: int = 0               # how many of this bus's streams that profile decodes
    nodes: int = 0                  # its detect nodes / addresses heard here

    @property
    def score(self) -> float:
        return sum(c.weight for c in self.checks)

    @property
    def label(self) -> str:
        kind, ident, sa = self.stream
        return f"PGN {ident:04X} from {sa:02X}" if kind == "j1939" else f"ID {ident:03X}"

    @property
    def good(self) -> list:
        return [c for c in self.checks if c.ok]

    def describe(self) -> str:
        bad = len(self.checks) - len(self.good)
        tail = f", {bad} left out (didn't fit the data)" if bad else ""
        return f"{self.label}: {len(self.good)} value(s) from {self.profile_name}{tail}"

    def entries(self) -> list[dict]:
        """Profile signal entries for the values that fit, noting where each came from."""
        out = []
        for c in self.good:
            d = signal_doc(c.signal)
            d["observed"] = {"sure": "borrowed", "from": self.profile_name,
                             "evidence": [f"Same message in {self.profile_name}; "
                                          + ("it moved and stayed in range here" if c.moved
                                             else "it fits the data here (didn't move)")]}
            out.append(d)
        return out


def signal_doc(s: sigtable.Signal) -> dict:
    """A Signal as a profile document entry (what ``profiles.parse_profile`` reads back)."""
    d: dict = {"name": s.name}
    if s.can_id is not None:
        d["id"] = f"0x{s.can_id:03X}"
    else:
        d["pgn"] = f"0x{s.pgn:04X}"
        if s.sa is not None:
            d["sa"] = [f"0x{a:02X}" for a in s.sa] if isinstance(s.sa, tuple) else f"0x{s.sa:02X}"
    d["start"] = s.start
    if s.is_text:
        d.update(type="text", length=s.length)
    elif s.bits:
        d.update(bit=s.bit, bits=s.bits)
    else:
        d["length"] = s.length
        if s.big_endian:
            d["order"] = "big"
    if not s.is_text:
        if s.signed:
            d["signed"] = True
        if s.scale != 1:
            d["scale"] = s.scale
        if s.offset:
            d["offset"] = s.offset
        if s.unit:
            d["unit"] = s.unit
        d["decimals"] = s.decimals
        if s.enum:
            d["enum"] = {str(k): v for k, v in s.enum}
        if s.low is not None:
            d["low"] = s.low
        if s.high is not None:
            d["high"] = s.high
    if s.group:
        d["group"] = s.group
    if not s.na_check:
        d["na_check"] = False
    return d


def field_key(stream: tuple, s: sigtable.Signal) -> str | None:
    """The field-watch key of a signal's place ("ID 1A2 b3", "PGN FF04 from 27 w2le"), or None
    when it isn't a byte, a 16-bit number or a single bit (those are the only fields watched)."""
    kind, ident, sa = stream
    label = f"PGN {ident:04X} from {sa:02X}" if kind == "j1939" else f"ID {ident:03X}"
    if s.is_text:
        return None
    if s.bits == 1:
        return f"{label} b{s.start}.{s.bit}"
    if s.bits == 8 and s.bit == 0 or not s.bits and s.length == 1:
        return f"{label} b{s.start}"
    if not s.bits and s.length == 2:
        return f"{label} w{s.start}{'be' if s.big_endian else 'le'}"
    return None


def _raw(s: sigtable.Signal, data: bytes) -> int | None:
    """The raw field value (before scale), or None when it doesn't fit the frame."""
    width = s.width_bits
    first = s.start * 8 + s.bit
    if s.is_text or first + width > len(data) * 8:
        return None
    if s.big_endian and not s.bits:
        v = int.from_bytes(data[s.start:s.start + s.length], "big")
    else:
        v = int.from_bytes(data, "little") >> first & ((1 << width) - 1)
    if s.signed and v >> (width - 1) & 1:
        v -= 1 << width
    return v


def judge(s: sigtable.Signal, payloads: list) -> Check:
    """Does a borrowed signal fit what's on the bus?"""
    raws = [r for r in (_raw(s, d) for d in payloads) if r is not None]
    if not raws:
        return Check(s, False, False, "doesn't fit the frames here (too short)")
    moved = len(set(raws)) > 1
    if s.is_text:
        return Check(s, True, moved)
    width = s.width_bits
    all_ones = (1 << width) - 1 if not s.signed else -1
    sane = [r for r in raws if not (s.na_check and r == all_ones)]    # "not available" is neutral
    if not sane:
        return Check(s, True, False, "not available here")
    if s.enum:
        keys = {k for k, _v in s.enum}
        hit = sum(1 for r in sane if r in keys) / len(sane)
        if hit < GOOD_SHARE:
            return Check(s, False, moved, f"values its list doesn't name ({100 - round(hit * 100)}%)")
        return Check(s, True, moved)
    if s.low is not None and s.high is not None and s.high > s.low:
        slack = (s.high - s.low) * RANGE_SLACK
        vals = [r * s.scale + s.offset for r in sane]
        hit = sum(1 for v in vals if s.low - slack <= v <= s.high + slack) / len(vals)
        if hit < GOOD_SHARE:
            return Check(s, False, moved, f"out of its normal range {round((1 - hit) * 100)}% of the time")
    return Check(s, True, moved)


def _spread(seq: list, n: int) -> list:
    if len(seq) <= n:
        return list(seq)
    step = len(seq) / n
    return [seq[int(i * step)] for i in range(n)]


def decoded_here(stream: tuple, active: profiles.Profile | None) -> bool:
    """Is the stream already decoded (by the profile in use, or the standard J1939 tables)?"""
    kind, ident, sa = stream
    if kind == "11-bit":
        if active is not None and any(s.can_id == ident for s in active.signals):
            return True
        return False
    own = active.signals if active is not None else []
    for s in list(sigtable.SIGNALS) + list(own):
        if s.can_id is None and s.pgn == ident and s.accepts(sa):
            return True
    return False


def _signals_for(p: profiles.Profile, stream: tuple) -> list:
    kind, ident, sa = stream
    if kind == "11-bit":
        return [s for s in p.signals if s.can_id == ident]
    return [s for s in p.signals if s.can_id is None and s.pgn == ident and s.accepts(sa)]


def _related(active: profiles.Profile | None, candidates: list) -> set:
    """The profile in use and everything it builds on: their values aren't 'borrowed'."""
    if active is None:
        return set()
    by_id = {p.id: p for p in candidates}
    out, pid = set(), active.id
    while pid and pid not in out:
        out.add(pid)
        p = by_id.get(pid)
        pid = p.extends if p is not None else ""
    return out


def suggest(samples: dict, candidates: list, active: profiles.Profile | None = None,
            nodes_seen=(), addresses_seen=()) -> list[Borrowed]:
    """``samples``: stream key -> [payload bytes] (oldest first). One ``Borrowed`` per undecoded
    stream that some other profile decodes consistently, the best profile for it; best first.
    Generic profiles and the profile in use (and what it extends) are never borrowed from."""
    skip = _related(active, candidates) | set(profiles.GENERIC.values())
    pool = [p for p in candidates if p.id not in skip and p.signals]
    streams = {k: _spread(list(v), MAX_SAMPLES) for k, v in samples.items() if v}
    open_streams = [k for k in streams if not decoded_here(k, active)]
    nodes_seen, addresses_seen = set(nodes_seen), set(addresses_seen)
    passing: list[Borrowed] = []
    for p in pool:
        nodes = len(p.detect_nodes & nodes_seen) + len(p.detect_addresses & addresses_seen)
        for k in open_streams:
            sigs = _signals_for(p, k)
            if not sigs:
                continue
            b = Borrowed(k, p.id, p.name, [judge(s, streams[k]) for s in sigs], 0, nodes)
            if b.score >= MIN_SCORE and len(b.good) * 4 >= len(b.checks) * 3:
                passing.append(b)               # else too little evidence, or a quarter didn't fit
    # A profile that fits many of this bus's streams knows this machine's family: it beats one
    # that happens to fit a single stream better.
    fit: dict[str, int] = {}
    for b in passing:
        fit[b.profile_id] = fit.get(b.profile_id, 0) + 1
    found: dict[tuple, Borrowed] = {}
    for b in passing:
        b.coverage = fit[b.profile_id]
        best = found.get(b.stream)
        if best is None or (b.nodes, b.coverage, b.score) > (best.nodes, best.coverage, best.score):
            found[b.stream] = b
    return sorted(found.values(), key=lambda b: (-b.nodes, -b.coverage, -b.score, b.label))


def hints(borrowed: list) -> dict:
    """Field-watch key -> (borrowed name, profile name), for every value that fits."""
    out = {}
    for b in borrowed:
        for c in b.good:
            key = field_key(b.stream, c.signal)
            if key:
                out.setdefault(key, (c.signal.name, b.profile_name))
    return out


def from_watch(watch, candidates: list, active: profiles.Profile | None = None) -> list[Borrowed]:
    """``suggest`` over what a ``core.fieldwatch.FieldWatch`` has heard."""
    samples = watch.payloads()
    nodes = {ident & 0x7F for kind, ident, _sa in samples if kind == "11-bit" and _sends(ident)}
    addresses = {sa for kind, _i, sa in samples if kind == "j1939"}
    return suggest(samples, candidates, active, nodes, addresses)


def _sends(can_id: int) -> bool:
    """A CANopen COB-ID its node sends itself: TPDO1-4, EMCY, heartbeat (not RPDOs, SDO requests)."""
    fn = can_id & 0x780
    return bool(can_id & 0x7F) and fn in (0x080, 0x180, 0x280, 0x380, 0x480, 0x700)


def from_frames(frames, candidates: list, active: profiles.Profile | None = None) -> list[Borrowed]:
    """``suggest`` over a recorded log (``CanFrame`` list)."""
    from .fieldwatch import stream_key
    samples: dict = {}
    for f in frames:
        if getattr(f, "error", None):
            continue
        samples.setdefault(stream_key(f.can_id, f.extended), []).append(bytes(f.data))
    nodes = {ident & 0x7F for kind, ident, _sa in samples if kind == "11-bit" and _sends(ident)}
    addresses = {sa for kind, _i, sa in samples if kind == "j1939"}
    return suggest(samples, candidates, active, nodes, addresses)
