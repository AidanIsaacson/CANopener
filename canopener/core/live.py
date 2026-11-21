"""Live data model shared by every front-end: the latest value, min / max and history of each
decoded signal on the bus, and which signals the user picked.

``LiveData`` holds J1939 broadcast signals; ``ObdLiveData`` holds OBD-II PIDs, decoded from the
requests and answers on the bus (``obd.ObdSniffer``). Both give the same rows, so one view shows
either.

The desktop Live Data tab is a view over this; the on-device Live Data page (Pygame, see
docs/ARCHITECTURE.md) can use the same model: call ``sample(session)`` each frame, then draw
``rows`` filtered with ``matches()``. ``selected`` is the "picked signals" list (plotted on the
desktop, the large-value grid on the device).

``sections`` groups rows for a view: by the controller that sends them (``GROUP_SOURCE``) or by
the profile's group (``GROUP_KIND``), and ``Collapsed`` remembers which sections are folded, so
both front-ends group and collapse the same way.
"""

from collections import deque
from dataclasses import dataclass, field

from .. import obd
from ..j1939.ids import parse_id
from ..j1939.signals import Signal, all_signals, id_signals
from .canopen import id_src, source_name

HISTORY_SECONDS = 600
RECENT_TEXTS = 8                # a text signal keeps the last few different messages it showed
SAMPLES_PER_SECOND = 10         # history depth: HISTORY_SECONDS at up to this rate


class LiveRow:
    def __init__(self, sig: Signal, sa: int):
        self.sig, self.sa = sig, sa
        self.value = None
        self.t = 0.0
        self.lo = self.hi = None
        self.points: deque = deque(maxlen=HISTORY_SECONDS * SAMPLES_PER_SECOND)
        self.recent: deque = deque(maxlen=RECENT_TEXTS)    # text signals: different messages, oldest first

    @property
    def key(self):
        return (self.sig.key, self.sa)

    def _update(self, t: float, v):
        if getattr(self.sig, "is_text", False):
            if v is None:               # a blank frame between blinking messages: keep the last one
                return
            self.t, self.value = t, v
            if v in self.recent:
                self.recent.remove(v)
            self.recent.append(v)       # no min / max / graph for text
            return
        self.t, self.value = t, v
        if v is not None:
            self.lo = v if self.lo is None else min(self.lo, v)
            self.hi = v if self.hi is None else max(self.hi, v)
            self.points.append((t, v))


class LiveData:
    source_header = "Source"
    id_headers = ("PGN / ID", "SPN")

    def __init__(self):
        self.rows: dict[tuple, LiveRow] = {}
        self.order: list[tuple] = []          # first-seen order
        self.selected: list[tuple] = []       # picked by the user, in the order picked

    def clear(self):
        """Forget everything (in place, so views holding these containers stay valid)."""
        self.rows.clear()
        self.order.clear()
        self.selected.clear()

    def _row(self, sig: Signal, sa: int, v) -> LiveRow | None:
        key = (sig.key, sa)
        row = self.rows.get(key)
        if row is None and v is not None:       # don't list parameters this machine doesn't support
            row = self.rows[key] = LiveRow(sig, sa)
            self.order.append(key)
        return row

    def sample(self, session) -> bool:
        """Pull the latest value of every signal from a live session. Returns True if new rows
        appeared (so a view knows to rebuild its list). Cheap enough to call 5-10 times a second."""
        n = len(self.order)
        by_pgn: dict[int, list] = {}            # one pass over the session, not one per signal
        with session.lock:
            for (pgn, sa), (t, d) in session.latest.items():
                by_pgn.setdefault(pgn, []).append((sa, t, d))
            std = {cid: session.latest_std[cid] for cid in id_signals() if cid in session.latest_std}
        for sig in all_signals():
            if sig.can_id is not None:
                continue
            for sa, t, d in by_pgn.get(sig.pgn, ()):
                if not sig.accepts(sa):
                    continue
                v = sig.decode(d)
                row = self._row(sig, sa, v)
                if row is not None and t != row.t:
                    row._update(t, v)
        for cid, (t, d) in std.items():         # 11-bit IDs (CANopen PDOs, plain CAN)
            for sig in id_signals()[cid]:
                v = sig.decode(d)
                row = self._row(sig, id_src(cid), v)
                if row is not None and t != row.t:
                    row._update(t, v)
        return len(self.order) != n

    def load_history(self, session):
        """For a decoded log: rebuild each signal's full history from the raw frames."""
        by_pgn: dict[int, list[Signal]] = {}
        for sig in all_signals():
            by_pgn.setdefault(sig.pgn, []).append(sig)
        with session.lock:
            frames = list(session.raw)
        by_id = id_signals()
        for fr in frames:
            if fr.error:
                continue
            if not fr.extended:
                for sig in by_id.get(fr.can_id, ()):
                    v = sig.decode(fr.data)
                    row = self._row(sig, id_src(fr.can_id), v)
                    if row is not None:
                        row._update(fr.timestamp, v)
                continue
            jid = parse_id(fr.can_id)
            for sig in by_pgn.get(jid.pgn, ()):
                if not sig.accepts(jid.sa):
                    continue
                v = sig.decode(fr.data)
                row = self._row(sig, jid.sa, v)
                if row is not None:
                    row._update(fr.timestamp, v)

    def reset_minmax(self):
        for r in self.rows.values():
            if isinstance(r.value, str):
                r.recent.clear()
                r.recent.append(r.value)
            else:
                r.lo = r.hi = r.value

    def select(self, key, on: bool = True):
        if on and key not in self.selected and key in self.rows:
            self.selected.append(key)
        elif not on and key in self.selected:
            self.selected.remove(key)

    def groups(self) -> list[str]:
        return sorted({self.rows[k].sig.group for k in self.order if self.rows[k].sig.group})

    def matches(self, key, query: str = "", group: str | None = None, only_selected: bool = False) -> bool:
        """Whether a row passes the view's filters: text (name, SPN, PGN, source, group), one
        group (None = all), and 'only the selected signals'."""
        r = self.rows[key]
        if only_selected and key not in self.selected:
            return False
        if group is not None and r.sig.group != group:
            return False
        q = query.strip().lower()
        if q:
            hay = f"{r.sig.name} {r.sig.spn or ''} {r.sig.ref} {source_name(r.sa)} {r.sig.group}".lower()
            return q in hay
        return True

    def source_label(self, sa: int) -> str:
        """A J1939 address, or a CANopen node / 11-bit ID (``core.canopen.source_name``)."""
        return source_name(sa)

    def ids(self, row: "LiveRow") -> tuple[str, str]:
        """The two reference columns for a row (PGN, or 'ID 181' for 11-bit; and the SPN)."""
        return row.sig.ref, "" if row.sig.spn is None else str(row.sig.spn)

    def find_spn(self, spn: int):
        """The first row for an SPN (fault code -> live value), or None."""
        return next((k for k in self.order if self.rows[k].sig.spn == spn), None)


# ----------------------------------------------------------------------
# OBD-II
# ----------------------------------------------------------------------
NOT_LIVE_PIDS = (0x01, 0x02, 0x41)      # monitor status / freeze DTC: readiness, not live values
_DECIMALS = {"V": 3, "rpm": 0, "km": 0, "s": 0, "min": 0, "": 2, "N·m": 0, "ppm": 0}


@dataclass(eq=False)
class ObdSignal:
    """One value of an OBD-II PID, shaped like a J1939 ``Signal`` for the live views."""

    pid: int
    name: str
    unit: str
    group: str
    labels: list            # text values seen (text PIDs plot as an index into this)
    spn = None
    pgn = None
    low = None
    high = None

    @property
    def key(self) -> str:
        return f"OBD:{self.pid:02X}:{self.name}"

    @property
    def decimals(self) -> int:
        return _DECIMALS.get(self.unit, 1)

    @property
    def enum(self) -> bool:
        return bool(self.labels)

    def text(self, value) -> str | None:
        if value is None or not self.labels:
            return None
        i = int(value)
        return self.labels[i] if 0 <= i < len(self.labels) else None

    def out_of_range(self, value) -> bool:
        return False

    def accepts(self, sa: int) -> bool:
        return True


class ObdLiveData(LiveData):
    """OBD-II PIDs as live rows, keyed (signal key, module response ID). Decoded passively from the
    frames on the bus, so it shows whatever is polled (by this app or another scan tool), and a
    recorded log replays into the same rows."""

    source_header = "Module"
    id_headers = ("PID", "")

    def __init__(self):
        super().__init__()
        self.sniffer = obd.ObdSniffer()
        self._sigs: dict[tuple[int, str], ObdSignal] = {}
        self._session = None
        self._seq = 0

    def clear(self):
        super().clear()
        self.sniffer = obd.ObdSniffer()
        self._sigs.clear()
        self._session, self._seq = None, 0

    def _signal(self, pid: int, name: str, unit: str) -> ObdSignal:
        sig = self._sigs.get((pid, name))
        if sig is None:
            sig = self._sigs[(pid, name)] = ObdSignal(pid, name, unit, obd.pid_group(pid), [])
        return sig

    def _feed(self, frames) -> bool:
        n = len(self.order)
        for fr in frames:
            if fr.error:
                continue
            for ecu, r in self.sniffer.feed(fr.can_id, fr.extended, bytes(fr.data), fr.timestamp):
                if r.mode != obd.MODE_CURRENT_DATA or not r.ok or r.pid in NOT_LIVE_PIDS:
                    continue
                for name, value, unit in r.values:
                    sig = self._signal(r.pid, name, unit)
                    if isinstance(value, str):
                        if value not in sig.labels:
                            sig.labels.append(value)
                        value = float(sig.labels.index(value))
                    row = self._row(sig, ecu, value)
                    if row is not None:
                        row._update(fr.timestamp, value)
        return len(self.order) != n

    def sample(self, session) -> bool:
        """Decode the frames that arrived since the last call. True if new rows appeared."""
        if session is not self._session:
            self.clear()
            self._session = session
        self._seq, frames = session.frames_since(self._seq)
        return self._feed(frames)

    def load_history(self, session):
        """For a decoded log: every OBD-II answer in it, with its time."""
        self.clear()
        self._session = session
        with session.lock:
            frames = list(session.raw)
            self._seq = session.frame_count
        self._feed(frames)

    def source_label(self, ecu: int) -> str:
        return self.sniffer.label(ecu)

    def ids(self, row: LiveRow) -> tuple[str, str]:
        return f"{row.sig.pid:02X}", ""

    def matches(self, key, query: str = "", group: str | None = None, only_selected: bool = False) -> bool:
        r = self.rows[key]
        if only_selected and key not in self.selected:
            return False
        if group is not None and r.sig.group != group:
            return False
        q = query.strip().lower()
        return not q or q in f"{r.sig.name} {r.sig.pid:02x} {self.source_label(r.sa)} {r.sig.group}".lower()

    def find_pids(self, pids) -> list[tuple]:
        """Rows for these PIDs (a trouble code's sensors), in the order given."""
        return [k for pid in pids for k in self.order if self.rows[k].sig.pid == pid]

    def polled(self) -> list[tuple[int, int]]:
        """(module, PID) of every row, in list order, once each: what to poll."""
        out = []
        for k in self.order:
            r = self.rows[k]
            if (r.sa, r.sig.pid) not in out:
                out.append((r.sa, r.sig.pid))
        return out


# ----------------------------------------------------------------------
# Sections: rows grouped by who sends them, or by the profile's groups
# ----------------------------------------------------------------------
GROUP_NONE, GROUP_SOURCE, GROUP_KIND = "none", "source", "group"
GROUPINGS = (GROUP_NONE, GROUP_SOURCE, GROUP_KIND)
OTHER = "Other"                 # rows whose signal has no group


@dataclass
class Section:
    id: str                     # stable across samples and restarts: "source:0", "group:Engine"
    title: str
    keys: list = field(default_factory=list)     # member row keys, in the model's order


def grouping_label(by: str, model=None) -> str:
    """What a grouping is called on screen ("By source", "By module" for OBD-II, ...)."""
    if by == GROUP_SOURCE:
        return f"By {(getattr(model, 'source_header', '') or 'Source').lower()}"
    return {GROUP_KIND: "By group", GROUP_NONE: "No grouping"}.get(by, by)


def section_of(model, key, by: str) -> tuple[str, str, tuple]:
    """(id, title, sort key) of the section a row belongs to under ``by``."""
    r = model.rows[key]
    if by == GROUP_SOURCE:
        return f"source:{r.sa}", model.source_label(r.sa), (0, r.sa if isinstance(r.sa, int) else 0, str(r.sa))
    g = getattr(r.sig, "group", "") or ""
    return f"group:{g or OTHER}", g or OTHER, (1 if not g else 0, 0, g.lower())


def sections(model, by: str, keys=None) -> list[Section]:
    """The rows (``keys``, default every row in first-seen order) in sections. Sources sort by
    their number (a J1939 engine at 00 first), groups by name with the ungrouped last. Rows keep
    the model's order inside a section. ``GROUP_NONE``: one section holding everything."""
    keys = list(model.order if keys is None else keys)
    if by not in (GROUP_SOURCE, GROUP_KIND):
        return [Section("all", "All", keys)]
    found: dict[str, tuple] = {}
    for k in keys:
        sid, title, order = section_of(model, k, by)
        if sid not in found:
            found[sid] = (order, Section(sid, title))
        found[sid][1].keys.append(k)
    return [sec for _, sec in sorted(found.values(), key=lambda x: x[0])]


class Collapsed:
    """Which sections are folded, per grouping. A plain dict of lists, so a front-end can keep it
    in its settings as it is (``to_dict`` / ``Collapsed(saved)``)."""

    def __init__(self, saved: dict | None = None):
        self._by = {k: set(v) for k, v in (saved or {}).items() if isinstance(v, (list, tuple, set))}

    def is_folded(self, by: str, sid: str) -> bool:
        return sid in self._by.get(by, ())

    def toggle(self, by: str, sid: str) -> bool:
        """Fold or unfold one section; returns True when it's now folded."""
        folded = self._by.setdefault(by, set())
        if sid in folded:
            folded.discard(sid)
            return False
        folded.add(sid)
        return True

    def set(self, by: str, sid: str, folded: bool):
        if folded != self.is_folded(by, sid):
            self.toggle(by, sid)

    def fold_all(self, by: str, ids, folded: bool = True):
        if folded:
            self._by.setdefault(by, set()).update(ids)
        else:
            self._by[by] = set()

    def to_dict(self) -> dict:
        return {k: sorted(v) for k, v in self._by.items() if v}
