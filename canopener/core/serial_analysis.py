"""Working out an unknown serial protocol from what was heard on the line.

The method is the same as ``core.changes`` for CAN: record both directions of a link while doing
one thing at a time (with timestamps), then let the data say what it can:

* **messages**: chunks joined back into messages by the quiet gaps between them, and by turns
  (one side answering the other closes the message before it);
* **kinds**: messages grouped by line, length and first byte, with what each byte position does
  (constant, a counter, a switch, a value, noise such as a checksum: ``changes._byte_kind``);
* **checksums**: common sums, XORs and CRCs tested against every message, at the end, covering
  the bytes before them from offset 0 to 3; a rule that fits nearly every message is reported;
* **exchanges**: which message on one line is answered by which on the other, and how fast;
  the line that asks is usually the console (the master);
* **text**: runs of printable ASCII, since a console's menu screens are often plain text (and
  text that only appears with the top bit cleared hints at 7 data bits plus parity);
* **phases**: with timestamps, the kinds that start or stop, and the byte positions whose usual
  value moves, from one timestamp to the next;
* **framing hints**: a wrong baud rate reads as bytes made mostly of 0x00 / 0x80 / 0xF0 / 0xFF.

Everything here reads; nothing transmits. ``analyze`` returns a ``Report``: ``lines()`` is the
text a person reads, ``to_dict()`` JSON for other tools. No Qt, no Pygame, standard library only.
"""

import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median

from ..serialline.framing import Framing
from ..serialline.log import read_serial_log
from ..serialline.port import Chunk
from .capture import SERIAL_LOG, Capture
from .changes import Phase, _byte_kind, phases_from_markers

SERIAL_ANALYSIS_TXT = "serial_analysis.txt"     # written beside serial.log when a capture is saved
DEFAULT_GAP = 0.010         # seconds of quiet that end a message (USB adapters batch bytes)
EXCHANGE_WINDOW = 0.5       # a reply starts within this long after the message it answers
MIN_KIND = 3                # messages of a kind before its byte positions are described
MIN_CHECKSUM = 5            # messages a checksum rule needs to be tested against
CHECKSUM_SHARE = 0.9        # and the share of them it has to fit
TEXT_MIN = 4                # printable characters in a row to count as text
SHOWN = 20                  # rows per section in the text report
JUNK = {0x00, 0x80, 0xC0, 0xE0, 0xF0, 0xF8, 0xFC, 0xFE, 0xFF}     # what a too-slow baud reads as


@dataclass
class Message:
    line: str
    t: float                # arrival of its first chunk
    t_end: float            # arrival of its last chunk
    data: bytes
    chunks: int = 1

    def kind(self, head: int = 1) -> tuple:
        return (self.line, len(self.data), self.data[:head].hex().upper())


def kind_name(kind: tuple) -> str:
    line, length, head = kind
    return f"{line} len {length} starts {head or '-'}"


# ----------------------------------------------------------------------
# messages
# ----------------------------------------------------------------------
def default_gap(framing: Framing | None = None) -> float:
    """The quiet that ends a message: the default, or more for a slow line (3 characters)."""
    return max(DEFAULT_GAP, 3 * framing.char_time) if framing else DEFAULT_GAP


def split_messages(chunks: list[Chunk], gap: float = DEFAULT_GAP, turns: bool = True) -> list[Message]:
    """Join chunks into messages. A message on a line ends after ``gap`` seconds of quiet on it,
    or (``turns``) when another line speaks: a half-duplex reply closes the request before it."""
    open_: dict[str, Message] = {}
    out: list[Message] = []
    last_line = None
    for c in sorted(chunks, key=lambda c: c.t):
        m = open_.get(c.line)
        if m is not None and (c.t - m.t_end > gap or (turns and last_line != c.line)):
            out.append(m)
            m = None
        if m is None:
            open_[c.line] = Message(c.line, c.t, c.t, c.data)
        else:
            m.data += c.data
            m.t_end = c.t
            m.chunks += 1
        last_line = c.line
    out.extend(open_.values())
    return sorted(out, key=lambda m: m.t)


# ----------------------------------------------------------------------
# checksums
# ----------------------------------------------------------------------
def _crc(width, poly, init, refin, refout, xorout):
    top, mask = 1 << (width - 1), (1 << width) - 1

    def reflect(v, n):
        return int(f"{v:0{n}b}"[::-1], 2)

    def fn(data: bytes) -> int:
        crc = init
        for b in data:
            if refin:
                b = reflect(b, 8)
            crc ^= b << (width - 8)
            for _ in range(8):
                crc = ((crc << 1) ^ poly) & mask if crc & top else (crc << 1) & mask
        if refout:
            crc = reflect(crc, width)
        return crc ^ xorout
    return fn


# name -> (bytes, function of the covered bytes)
CHECKSUMS = {
    "sum8": (1, lambda d: sum(d) & 0xFF),
    "sum8, two's complement (all bytes add to 0)": (1, lambda d: -sum(d) & 0xFF),
    "sum8, inverted (all bytes add to FF)": (1, lambda d: ~sum(d) & 0xFF),
    "xor8": (1, lambda d: _xor(d)),
    "xor8, inverted": (1, lambda d: _xor(d) ^ 0xFF),
    "CRC-8": (1, _crc(8, 0x07, 0x00, False, False, 0x00)),
    "CRC-8/MAXIM": (1, _crc(8, 0x31, 0x00, True, True, 0x00)),
    "CRC-8/SAE-J1850": (1, _crc(8, 0x1D, 0xFF, False, False, 0xFF)),
    "sum16": (2, lambda d: sum(d) & 0xFFFF),
    "CRC-16/ARC": (2, _crc(16, 0x8005, 0x0000, True, True, 0x0000)),
    "CRC-16/MODBUS": (2, _crc(16, 0x8005, 0xFFFF, True, True, 0x0000)),
    "CRC-16/CCITT-FALSE": (2, _crc(16, 0x1021, 0xFFFF, False, False, 0x0000)),
    "CRC-16/XMODEM": (2, _crc(16, 0x1021, 0x0000, False, False, 0x0000)),
    "CRC-16/KERMIT": (2, _crc(16, 0x1021, 0x0000, True, True, 0x0000)),
}


def _xor(d: bytes) -> int:
    x = 0
    for b in d:
        x ^= b
    return x


@dataclass
class ChecksumFit:
    name: str
    skip: int               # covered bytes start here
    order: str              # "", "big-endian" or "little-endian" (2-byte checks)
    fits: int
    tested: int
    lines: list = field(default_factory=list)

    @property
    def share(self) -> float:
        return self.fits / self.tested if self.tested else 0.0

    def text(self) -> str:
        where = f"over bytes {self.skip}.." if self.skip else "over every byte before it"
        order = f", {self.order}" if self.order else ""
        return (f"{self.name} at the end{order}, {where}: fits {self.fits} of {self.tested} "
                f"messages ({self.share:.0%}) on {', '.join(self.lines)}")


def find_checksums(messages: list[Message], max_skip: int = 3) -> list[ChecksumFit]:
    """Checksum rules that fit at least ``CHECKSUM_SHARE`` of the messages they could apply to.
    Messages are only tested where at least two bytes are covered (one byte "sums" to itself)."""
    found = []
    for name, (width, fn) in CHECKSUMS.items():
        orders = ("big-endian", "little-endian") if width == 2 else ("",)
        for skip in range(max_skip + 1):
            for order in orders:
                fits = tested = 0
                lines: set = set()
                for m in messages:
                    body = m.data[skip:len(m.data) - width]
                    if len(body) < 2:
                        continue
                    tested += 1
                    tail = m.data[-width:]
                    got = int.from_bytes(tail, "little" if order == "little-endian" else "big")
                    if fn(body) == got:
                        fits += 1
                        lines.add(m.line)
                if tested >= MIN_CHECKSUM and fits / tested >= CHECKSUM_SHARE:
                    found.append(ChecksumFit(name, skip, order, fits, tested, sorted(lines)))
    found.sort(key=lambda f: (-f.share, -f.tested, f.skip))
    return found


# ----------------------------------------------------------------------
# kinds, exchanges, text
# ----------------------------------------------------------------------
@dataclass
class Kind:
    key: tuple
    count: int
    first: float
    example: bytes
    positions: list = field(default_factory=list)       # [(index, behaviour, distinct values)]

    @property
    def name(self) -> str:
        return kind_name(self.key)


def kinds(messages: list[Message], head: int = 1) -> list[Kind]:
    groups: dict[tuple, list[Message]] = defaultdict(list)
    for m in messages:
        groups[m.kind(head)].append(m)
    out = []
    for key, ms in groups.items():
        k = Kind(key, len(ms), ms[0].t, ms[0].data)
        if len(ms) >= MIN_KIND:
            for i in range(key[1]):
                seq = [m.data[i] for m in ms]
                k.positions.append((i, _byte_kind(seq), len(set(seq))))
        out.append(k)
    out.sort(key=lambda k: (-k.count, k.key))
    return out


@dataclass
class Exchange:
    ask: bytes
    ask_line: str
    count: int
    replies: Counter
    latency: float          # median seconds from the end of the ask to the reply


def exchanges(messages: list[Message], window: float = EXCHANGE_WINDOW) -> list[Exchange]:
    """Each message and the first one on another line that starts within ``window`` after it."""
    pairs: dict[tuple, list] = defaultdict(list)
    for i, m in enumerate(messages):
        for n in messages[i + 1:]:
            if n.t - m.t_end > window:
                break
            if n.line != m.line:
                pairs[(m.line, m.data)].append((n.line, n.data, max(0.0, n.t - m.t_end)))
                break
    out = []
    for (line, ask), replies in pairs.items():
        out.append(Exchange(ask, line, len(replies),
                            Counter(f"{rl}: {rd.hex(' ').upper()}" for rl, rd, _ in replies),
                            median(lat for _, _, lat in replies)))
    out.sort(key=lambda e: -e.count)
    return out


def master_line(found: list[Exchange]) -> str | None:
    """The line that asks most often: usually the console."""
    asks = Counter()
    for e in found:
        asks[e.ask_line] += e.count
    return asks.most_common(1)[0][0] if asks else None


def text_runs(messages: list[Message], min_len: int = TEXT_MIN, mask7: bool = False) -> Counter:
    """Printable ASCII runs (``mask7``: with each byte's top bit cleared first) -> times seen."""
    found = Counter()
    for m in messages:
        run = []
        for b in m.data:
            b = b & 0x7F if mask7 else b
            if 0x20 <= b < 0x7F:
                run.append(chr(b))
                continue
            if len(run) >= min_len:
                found["".join(run).strip()] += 1
            run = []
        if len(run) >= min_len:
            found["".join(run).strip()] += 1
    found.pop("", None)
    return found


def framing_hint(chunks: list[Chunk]) -> str:
    """A warning when the bytes look like a wrong baud rate, else ""."""
    data = b"".join(c.data for c in chunks)
    if len(data) < 32:
        return ""
    junk = sum(1 for b in data if b in JUNK) / len(data)
    if junk > 0.7:
        return (f"{junk:.0%} of the bytes are 00 / 80 / F0 / FF and the like: that's how a line "
                "read at the wrong baud rate looks (usually too fast for it). Check the bit width "
                "on the logic analyzer (canopener-cli serial baud).")
    return ""


# ----------------------------------------------------------------------
# phases
# ----------------------------------------------------------------------
@dataclass
class PhaseKinds:
    phase: Phase
    counts: Counter                             # kind -> messages in this phase
    new: list = field(default_factory=list)     # kinds first heard in this phase
    gone: list = field(default_factory=list)    # kinds heard before, not in this phase
    moved: list = field(default_factory=list)   # (kind, byte index, usual value before, now)


def by_phase(messages: list[Message], phases: list[Phase], head: int = 1) -> list[PhaseKinds]:
    out: list[PhaseKinds] = []
    seen: set = set()
    usual_before: dict = {}
    for ph in phases:
        ms = [m for m in messages if ph.start <= m.t < ph.end or (ph is phases[-1] and m.t == ph.end)]
        counts = Counter(m.kind(head) for m in ms)
        pk = PhaseKinds(ph, counts)
        pk.new = sorted(k for k in counts if k not in seen)
        pk.gone = sorted(k for k in seen if k not in counts)
        seen |= set(counts)
        usual = {}
        for key in counts:
            group = [m.data for m in ms if m.kind(head) == key]
            for i in range(key[1]):
                usual[(key, i)] = Counter(d[i] for d in group).most_common(1)[0][0]
        for (key, i), v in sorted(usual.items()):
            before = usual_before.get((key, i))
            if before is not None and before != v:
                pk.moved.append((key, i, before, v))
        usual_before.update(usual)
        out.append(pk)
    return out


# ----------------------------------------------------------------------
# the report
# ----------------------------------------------------------------------
@dataclass
class Report:
    lines_meta: list
    chunks: int
    bytes_by_line: Counter
    messages: list
    kinds: list
    checksums: list
    exchanges: list
    master: str | None
    text: Counter
    text7: Counter
    phases: list
    hint: str
    gap: float

    def lines(self) -> list[str]:
        L = ["Serial lines"]
        meta = {d.get("line"): d for d in self.lines_meta}
        for line in sorted(set(self.bytes_by_line) | set(meta)):
            d = meta.get(line, {})
            what = ", ".join(x for x in (d.get("label", ""), d.get("port", ""), d.get("framing", "")) if x)
            n_msgs = sum(1 for m in self.messages if m.line == line)
            L.append(f"  {line}: {self.bytes_by_line.get(line, 0):,} bytes, {n_msgs:,} messages"
                     + (f" ({what})" if what else ""))
        L.append(f"  {self.chunks:,} chunks, joined into messages at {self.gap * 1000:g} ms of quiet")
        if self.hint:
            L += ["", "Framing", "  " + self.hint]
        if self.master:
            L += ["", f"Line {self.master} asks most often: probably the console (the master)."]

        L += ["", "Message kinds (line, length, first byte), most common first"]
        for k in self.kinds[:SHOWN]:
            L.append(f"  {k.name}: {k.count}x, e.g. {k.example.hex(' ').upper()}")
            if k.positions:
                L.append("    " + "  ".join(f"[{i}] {how}" + (f" ({n})" if how != "constant" else "")
                                            for i, how, n in k.positions))
        if len(self.kinds) > SHOWN:
            L.append(f"  ... {len(self.kinds) - SHOWN} more kinds")

        L += ["", "Checksums that fit"]
        L += [f"  {c.text()}" for c in self.checksums[:5]] or ["  None of the usual sums, XORs or CRCs fit."]

        L += ["", "Exchanges (a message and the reply on the other line), most common first"]
        for e in self.exchanges[:SHOWN]:
            reply, n = e.replies.most_common(1)[0]
            more = f" (+{len(e.replies) - 1} other replies)" if len(e.replies) > 1 else ""
            L.append(f"  {e.ask_line}: {e.ask.hex(' ').upper()}  ->  {reply}  {n}/{e.count}x, "
                     f"{e.latency * 1000:.0f} ms{more}")
        if not self.exchanges:
            L.append("  None: nothing on one line was answered on another within "
                     f"{EXCHANGE_WINDOW * 1000:g} ms.")

        L += ["", "Text"]
        if self.text:
            L += [f"  {n}x  {s!r}" for s, n in self.text.most_common(SHOWN)]
        if self.text7 and sum(self.text7.values()) > 2 * max(1, sum(self.text.values())):
            L.append("  With the top bit cleared there's much more text: the line may be 7 data bits "
                     "plus parity (try 7E1 / 7O1).")
            L += [f"  {n}x  {s!r}" for s, n in self.text7.most_common(SHOWN)]
        if not self.text and not self.text7:
            L.append("  No runs of printable characters.")

        if self.phases and len(self.phases) > 1:
            L += ["", "By timestamp"]
            for pk in self.phases:
                L.append(f"  {pk.phase.label}: {sum(pk.counts.values())} messages")
                L += [f"    new: {kind_name(k)}" for k in pk.new[:SHOWN]]
                L += [f"    stopped: {kind_name(k)}" for k in pk.gone[:SHOWN]]
                L += [f"    {kind_name(k)} byte {i}: {a:02X} -> {b:02X}" for k, i, a, b in pk.moved[:SHOWN]]
        return L

    def to_dict(self) -> dict:
        return {
            "lines": self.lines_meta, "chunks": self.chunks, "bytes": dict(self.bytes_by_line),
            "messages": len(self.messages), "gap_s": self.gap, "master": self.master, "hint": self.hint,
            "kinds": [{"name": k.name, "count": k.count, "example": k.example.hex(),
                       "positions": [{"index": i, "kind": how, "values": n} for i, how, n in k.positions]}
                      for k in self.kinds],
            "checksums": [c.text() for c in self.checksums],
            "exchanges": [{"line": e.ask_line, "ask": e.ask.hex(), "count": e.count,
                           "replies": dict(e.replies), "latency_s": e.latency} for e in self.exchanges],
            "text": dict(self.text.most_common(200)),
            "phases": [{"label": pk.phase.label, "messages": sum(pk.counts.values()),
                        "new": [kind_name(k) for k in pk.new], "stopped": [kind_name(k) for k in pk.gone],
                        "moved": [{"kind": kind_name(k), "byte": i, "from": a, "to": b} for k, i, a, b in pk.moved]}
                       for pk in self.phases],
        }


def analyze(chunks: list[Chunk], lines_meta: list | None = None, markers: list[tuple[float, str]] | None = None,
            gap: float | None = None, head: int = 1) -> Report:
    """The whole analysis of one recording. ``markers``: [(time, label)] from a capture."""
    lines_meta = lines_meta or []
    if gap is None:
        framings = [Framing.parse(d["framing"]) for d in lines_meta if d.get("framing")]
        gap = default_gap(min(framings, key=lambda f: f.baud) if framings else None)
    msgs = split_messages(chunks, gap)
    phases = []
    if msgs and markers:
        phases = by_phase(msgs, phases_from_markers(markers, msgs[0].t, msgs[-1].t_end), head)
    found = exchanges(msgs)
    by_line = Counter()
    for c in chunks:
        by_line[c.line] += len(c.data)
    return Report(lines_meta, len(chunks), by_line, msgs, kinds(msgs, head), find_checksums(msgs), found,
                  master_line(found), text_runs(msgs), text_runs(msgs, mask7=True), phases,
                  framing_hint(chunks), gap)


def analyze_capture(path: str, gap: float | None = None, head: int = 1) -> Report:
    """A capture folder (its serial.log and timestamps) or a serial.log on its own."""
    folder = path if os.path.isdir(path) else None
    log = os.path.join(path, SERIAL_LOG) if folder else path
    lines_meta, chunks = read_serial_log(log)
    markers = None
    if folder and os.path.exists(os.path.join(folder, "capture.json")):
        c = Capture.load(folder)
        markers = [(m.t, c.title(i)) for i, m in enumerate(c.markers) if m.tagged]
    return analyze(chunks, lines_meta, markers, gap, head)
