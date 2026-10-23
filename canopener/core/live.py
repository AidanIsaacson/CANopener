"""Live data model shared by every front-end: the latest value, min / max and history of each
decoded signal on the bus, and which signals the user picked.

The desktop Live Data tab is a view over this; the on-device Live Data page (Pygame, see
docs/ARCHITECTURE.md) can use the same model: call ``sample(session)`` each frame, then draw
``rows`` filtered with ``matches()``. ``selected`` is the "picked signals" list (plotted on the
desktop, the large-value grid on the device).
"""

from collections import deque

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

    def find_spn(self, spn: int):
        """The first row for an SPN (fault code -> live value), or None."""
        return next((k for k in self.order if self.rows[k].sig.spn == spn), None)
