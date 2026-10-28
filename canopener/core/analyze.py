"""'Find what changed': compare two stretches of bus traffic and rank the bytes / bits that differ.

Tag "pedal up", press the pedal, tag "pedal down" (or mark two moments), and the data that moved
with the pedal comes out on top. Works on any list of ``CanFrame`` (a live session's buffer, a
loaded log or capture), 29-bit and 11-bit IDs alike. It only suggests where to look: naming the
data is up to you (``core.labeling``).

Scoring, per CAN ID and position: how far the average moved between the windows compared with how
much the value wanders inside them. Rolling counters and checksums wander a lot in both windows,
so they score low; a byte or bit that sits at one value before and another after scores highest.
"""

import bisect
from dataclasses import dataclass
from statistics import fmean, pstdev

from ..j1939.ids import parse_id

MARK_SECONDS = 2.0      # "Mark A / B now" uses the last this-many seconds of traffic
WINDOW_MAX = 10.0       # a window from a tag runs to the next tag, at most this long
CLEAR = 50.0            # score for "one steady value before, another steady value after"
HIDDEN_BIT = 20.0       # a bit that flipped inside a byte whose other bits wander
MIN_SCORE = 2.0


@dataclass(frozen=True)
class Candidate:
    can_id: int
    extended: bool
    start: int              # byte
    length: int = 1         # bytes (1 or 2)
    big_endian: bool = False    # a 2-byte value with its high byte first
    bit: int = 0
    bits: int = 0           # 1 = a single bit (a switch)
    score: float = 0.0
    before: str = ""        # what it looked like in window A / B, e.g. "00" or "12..1F"
    after: str = ""

    @property
    def pgn(self) -> int | None:
        return parse_id(self.can_id).pgn if self.extended else None

    @property
    def sa(self) -> int | None:
        return parse_id(self.can_id).sa if self.extended else None

    @property
    def where(self) -> str:
        if self.bits:
            return f"byte {self.start} bit {self.bit}"
        if self.length == 2:
            return f"bytes {self.start}-{self.start + 1}" + (" (high first)" if self.big_endian else "")
        return f"byte {self.start}"

    @property
    def id_str(self) -> str:
        return f"{self.can_id:08X}" if self.extended else f"{self.can_id:03X}"

    def signal_draft(self) -> dict:
        """Starting values for the label dialog."""
        d = {"pgn": self.pgn, "sa": self.sa} if self.extended else {"id": self.can_id}
        d["start"] = self.start
        if self.bits:
            d.update(bit=self.bit, bits=self.bits)
        else:
            d["length"] = self.length
            if self.big_endian:
                d["order"] = "big"
        return d


def in_window(frames: list, t0: float, t1: float) -> list:
    """Frames with t0 <= timestamp <= t1 (``frames`` in time order, as a session keeps them)."""
    times = [f.timestamp for f in frames]
    return frames[bisect.bisect_left(times, t0):bisect.bisect_right(times, t1)]


def tag_windows(tagged: list[tuple[str, float]], end: float | None = None) -> list[tuple[str, float, float]]:
    """(title, t) tags -> (title, start, end) windows: each runs to the next tag, at most
    ``WINDOW_MAX`` seconds (the last one to ``end`` if given)."""
    tagged = sorted(tagged, key=lambda x: x[1])
    out = []
    for i, (title, t) in enumerate(tagged):
        nxt = tagged[i + 1][1] if i + 1 < len(tagged) else (end if end is not None else t + WINDOW_MAX)
        out.append((title, t, min(nxt, t + WINDOW_MAX)))
    return out


def _payloads(frames) -> dict[tuple[int, bool], list[bytes]]:
    out: dict[tuple[int, bool], list[bytes]] = {}
    for f in frames:
        if f.error or f.tx:
            continue
        out.setdefault((f.can_id, f.extended), []).append(bytes(f.data))
    return out


def _span(vals: list[int], width: int) -> str:
    lo, hi = min(vals), max(vals)
    w = width * 2
    return f"{lo:0{w}X}" if lo == hi else f"{lo:0{w}X}..{hi:0{w}X}"


def _counting(vals: list[int]) -> int:
    """The step if ``vals`` only ever stays put or goes up by one same step (mod 256), else 0."""
    steps = {(b - a) & 0xFF for a, b in zip(vals, vals[1:])} - {0}
    return steps.pop() if len(steps) == 1 and next(iter(steps)) < 0x80 else 0


def _is_counter(va: list[int], vb: list[int]) -> bool:
    """A rolling counter / uptime tick: counts by the same step in both windows. It always
    "changes" between any two moments, so it's never what you're looking for."""
    step = _counting(va)
    return bool(step) and _counting(vb) == step


def _separation(a: list[int], b: list[int]) -> float:
    if len(set(a)) == 1 and len(set(b)) == 1:
        return CLEAR if a[0] != b[0] else 0.0
    sep = abs(fmean(b) - fmean(a)) / (pstdev(a) + pstdev(b) + 0.5)
    if max(a) < min(b) or max(b) < min(a):      # the ranges don't even overlap
        sep *= 2
    return min(sep, CLEAR - 1)


def find_changes(frames, window_a: tuple[float, float], window_b: tuple[float, float],
                 limit: int = 40, min_frames: int = 2) -> list[Candidate]:
    """Candidates that differ between the two windows, most convincing first."""
    frames = list(frames)
    pa = _payloads(in_window(frames, *window_a))
    pb = _payloads(in_window(frames, *window_b))
    out: list[Candidate] = []
    for key in pa.keys() & pb.keys():
        a, b = pa[key], pb[key]
        if len(a) < min_frames or len(b) < min_frames:
            continue
        n = min(min(len(d) for d in a), min(len(d) for d in b))
        cid, ext = key
        for i in range(n):
            va, vb = [d[i] for d in a], [d[i] for d in b]
            if _is_counter(va, vb):
                continue
            steady = len(set(va)) == 1 and len(set(vb)) == 1
            flipped = [k for k in range(8) if steady and (va[0] ^ vb[0]) >> k & 1]
            if steady and len(flipped) == 1:          # one switch changed: offer the bit, not the byte
                k = flipped[0]
                out.append(Candidate(cid, ext, i, bit=k, bits=1, score=CLEAR,
                                     before=str(va[0] >> k & 1), after=str(vb[0] >> k & 1)))
                continue
            s = _separation(va, vb)
            if s >= MIN_SCORE:
                out.append(Candidate(cid, ext, i, score=s, before=_span(va, 1), after=_span(vb, 1)))
            elif not steady:            # the byte as a whole didn't move, but a switch may hide in it
                for k in range(8):
                    ba, bb = {v >> k & 1 for v in va}, {v >> k & 1 for v in vb}
                    if len(ba) == 1 and len(bb) == 1 and ba != bb:
                        out.append(Candidate(cid, ext, i, bit=k, bits=1, score=HIDDEN_BIT,
                                             before=str(min(ba)), after=str(min(bb))))
            # 16-bit value when both bytes move: a low byte that wraps makes each byte look
            # noisy on its own while the pair moves smoothly. Both byte orders are tried (J1939
            # and CANopen are low byte first; some OEM data is high byte first): the better wins.
            if i + 1 < n and not _is_counter([d[i + 1] for d in a], [d[i + 1] for d in b]) \
                    and len({d[i + 1] for d in a + b}) > 1 and len(set(va + vb)) > 1:
                best = None
                for big in (False, True):
                    wa = [(d[i] << 8 | d[i + 1]) if big else (d[i] | d[i + 1] << 8) for d in a]
                    wb = [(d[i] << 8 | d[i + 1]) if big else (d[i] | d[i + 1] << 8) for d in b]
                    s2 = _separation(wa, wb)
                    if s2 >= MIN_SCORE and (best is None or s2 > best.score - 0.5):
                        best = Candidate(cid, ext, i, length=2, big_endian=big, score=s2 + 0.5,
                                         before=_span(wa, 2), after=_span(wb, 2))
                if best is not None:
                    out.append(best)
    out.sort(key=lambda c: (-c.score, not c.extended, c.can_id, c.start, c.bit))
    return out[:limit]
