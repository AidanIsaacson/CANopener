"""Live data model shared by every front-end: the latest value, min / max and history of each
decoded signal on the bus, and which signals the user picked.

``LiveData`` holds J1939 broadcast signals; ``ObdLiveData`` holds OBD-II PIDs, decoded from the
requests and answers on the bus (``obd.ObdSniffer``). Both give the same rows, so one view shows
either.

The desktop Live Data tab is a view over this; the on-device Live Data page (Pygame, see
docs/ARCHITECTURE.md) can use the same model: call ``sample(session)`` each frame, then draw
``rows`` filtered with ``matches()``. ``selected`` is the "picked signals" list (plotted on the
desktop, the large-value grid on the device).
"""

from collections import deque
from dataclasses import dataclass

from .. import obd
from ..j1939.addresses import sa_name
from ..j1939.ids import parse_id
from ..j1939.signals import Signal, all_signals

HISTORY_SECONDS = 600
SAMPLES_PER_SECOND = 10         # history depth: HISTORY_SECONDS at up to this rate


class LiveRow:
    def __init__(self, sig: Signal, sa: int):
        self.sig, self.sa = sig, sa
        self.value = None
        self.t = 0.0
        self.lo = self.hi = None
        self.points: deque = deque(maxlen=HISTORY_SECONDS * SAMPLES_PER_SECOND)

    @property
    def key(self):
        return (self.sig.key, self.sa)

    def _update(self, t: float, v):
        self.t, self.value = t, v
        if v is not None:
            self.lo = v if self.lo is None else min(self.lo, v)
            self.hi = v if self.hi is None else max(self.hi, v)
            self.points.append((t, v))


class LiveData:
    source_header = "Source"
    id_headers = ("PGN", "SPN")

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
        for sig in all_signals():
            for sa, t, d in by_pgn.get(sig.pgn, ()):
                if not sig.accepts(sa):
                    continue
                v = sig.decode(d)
                row = self._row(sig, sa, v)
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
        for fr in frames:
            if not fr.extended or fr.error:
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
            hay = f"{r.sig.name} {r.sig.spn or ''} {r.sig.pgn:04x} {sa_name(r.sa)} {r.sig.group}".lower()
            return q in hay
        return True

    def source_label(self, sa: int) -> str:
        return sa_name(sa)

    def ids(self, row: "LiveRow") -> tuple[str, str]:
        """The two reference columns for a row (PGN and SPN for J1939)."""
        return f"{row.sig.pgn:04X}", "" if row.sig.spn is None else str(row.sig.spn)

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
