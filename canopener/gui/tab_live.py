"""Live Data tab: decoded broadcast parameters with min/max and a rolling chart."""

import time
from collections import deque

from ..core import units
from ..j1939.addresses import sa_name
from ..j1939.ids import parse_id
from ..j1939.signals import Signal, all_signals
from .qt import Qt, QtGui, QtWidgets
from .widgets import StripChart, item, setup_table

COLS = ["Plot", "Signal", "Value", "Unit", "Min", "Max", "Source", "PGN", "SPN", "Age"]
HISTORY_SECONDS = 600
STALE_AFTER = 3.0


class _Row:
    def __init__(self, sig: Signal, sa: int):
        self.sig, self.sa = sig, sa
        self.value = None
        self.t = 0.0
        self.lo = self.hi = None
        self.points: deque = deque(maxlen=HISTORY_SECONDS * 10)

    @property
    def key(self):
        return (self.sig.key, self.sa)


class LiveTab(QtWidgets.QWidget):
    title = "Live Data"

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.rows: dict[tuple, _Row] = {}
        self.order: list[tuple] = []
        self.plotted: list[tuple] = []
        lay = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("Filter signals (name, SPN, PGN)...")
        self.filter.textChanged.connect(self._apply_filter)
        bar.addWidget(self.filter, 2)
        reset = QtWidgets.QPushButton("Reset min/max")
        reset.clicked.connect(self._reset_minmax)
        bar.addWidget(reset)
        bar.addWidget(QtWidgets.QLabel("Chart window"))
        self.window = QtWidgets.QComboBox()
        for label, secs in (("30 s", 30), ("1 min", 60), ("2 min", 120), ("5 min", 300), ("10 min", 600)):
            self.window.addItem(label, secs)
        self.window.setCurrentIndex(1)
        self.window.currentIndexChanged.connect(lambda _: self.chart.set_window(self.window.currentData()))
        bar.addWidget(self.window)
        self.pause = QtWidgets.QPushButton("Pause chart")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(lambda on: self.chart.set_paused(on))
        bar.addWidget(self.pause)
        clear = QtWidgets.QPushButton("Clear plots")
        clear.clicked.connect(self._clear_plots)
        bar.addWidget(clear)
        lay.addLayout(bar)

        split = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        self.table = QtWidgets.QTableWidget()
        setup_table(self.table, COLS, stretch=(1,))
        for c, w in ((0, 44), (2, 100), (3, 60), (4, 80), (5, 80), (6, 170), (7, 60), (8, 60), (9, 55)):
            self.table.setColumnWidth(c, w)
        self.table.itemChanged.connect(self._item_changed)
        split.addWidget(self.table)
        self.chart = StripChart()
        split.addWidget(self.chart)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay.addWidget(split)
        self.empty = QtWidgets.QLabel("No standard live parameters seen yet. Key on / engine running?")
        self.empty.setStyleSheet("color: gray")
        lay.addWidget(self.empty)

        ctx.session_changed.connect(self._session_changed)
        ctx.units_changed.connect(lambda _: self._units_changed())
        ctx.profile_changed.connect(lambda: self._session_changed(ctx.session))
        ctx.show_signal.connect(self.focus_spn)

    def _session_changed(self, session):
        self.rows.clear()
        self.order.clear()
        self.plotted.clear()
        self.table.setRowCount(0)
        self.chart.follow_data = bool(session and session.offline)
        if session is not None and session.offline:
            self._load_history(session)
        self._sync_chart()
        self.sample()
        self.refresh()

    def _load_history(self, session):
        """For a decoded log, rebuild each signal's full history from the raw frames."""
        by_pgn = {}
        for sig in all_signals():
            by_pgn.setdefault(sig.pgn, []).append(sig)
        with session.lock:
            frames = list(session.raw)
        for fr in frames:
            if not fr.extended or fr.error:
                continue
            jid = parse_id(fr.can_id)
            for sig in by_pgn.get(jid.pgn, ()):
                if sig.sa is not None and jid.sa != sig.sa:
                    continue
                v = sig.decode(fr.data)
                key = (sig.key, jid.sa)
                row = self.rows.get(key)
                if row is None:
                    if v is None:
                        continue
                    row = self.rows[key] = _Row(sig, jid.sa)
                    self.order.append(key)
                if v is not None:
                    row.lo = v if row.lo is None else min(row.lo, v)
                    row.hi = v if row.hi is None else max(row.hi, v)
                    row.points.append((fr.timestamp, v))
                row.t, row.value = fr.timestamp, v
        self._rebuild_table()

    def _units_changed(self):
        self._sync_chart()
        self.refresh(force=True)

    # ------------------------------------------------------------------
    def sample(self):
        """Pull the latest value of every signal from the session (called ~5x/s, any tab)."""
        s = self.ctx.session
        if s is None:
            return
        added = False
        for sig in all_signals():
            for sa, t, d in s.latest_for(sig.pgn):
                if sig.sa is not None and sa != sig.sa:
                    continue
                key = (sig.key, sa)
                row = self.rows.get(key)
                v = sig.decode(d)
                if row is None:
                    if v is None:
                        continue        # don't list parameters this machine doesn't support
                    row = self.rows[key] = _Row(sig, sa)
                    self.order.append(key)
                    added = True
                if t != row.t:
                    row.t, row.value = t, v
                    if v is not None:
                        row.lo = v if row.lo is None else min(row.lo, v)
                        row.hi = v if row.hi is None else max(row.hi, v)
                        row.points.append((t, v))
        if added:
            self._rebuild_table()

    def _rebuild_table(self):
        t = self.table
        t.blockSignals(True)
        t.setRowCount(len(self.order))
        for i, key in enumerate(self.order):
            r = self.rows[key]
            chk = QtWidgets.QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if key in self.plotted else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, key)
            t.setItem(i, 0, chk)
            t.setItem(i, 1, item(r.sig.name))
            t.setItem(i, 6, item(sa_name(r.sa)))
            t.setItem(i, 7, item(f"{r.sig.pgn:04X}", True))
            t.setItem(i, 8, item(r.sig.spn if r.sig.spn is not None else "", True))
            for c in (2, 3, 4, 5, 9):
                t.setItem(i, c, item("", c != 3))
        t.blockSignals(False)
        self._apply_filter()
        self.refresh(force=True)

    def refresh(self, force: bool = False):
        if not force and not self.isVisible():
            return
        self.empty.setVisible(not self.order)
        now = time.time()
        offline = self.ctx.session is not None and self.ctx.session.offline
        sysu = self.ctx.units
        t = self.table
        t.blockSignals(True)
        for i, key in enumerate(self.order):
            r = self.rows[key]
            sig = r.sig
            val, unit = units.convert(r.value, sig.unit, sysu)
            lo = units.convert(r.lo, sig.unit, sysu)[0]
            hi = units.convert(r.hi, sig.unit, sysu)[0]
            nd = sig.decimals + (1 if unit == "psi" and sig.decimals == 0 else 0)
            age = now - r.t
            stale = not offline and age > STALE_AFTER
            cells = {2: _num(val, nd), 3: unit, 4: _num(lo, nd), 5: _num(hi, nd),
                     9: "" if offline else f"{age:.1f}s"}
            for c, text in cells.items():
                it = t.item(i, c)
                if it is not None and it.text() != text:
                    it.setText(text)
            it = t.item(i, 2)
            if it is not None:
                it.setForeground(QtGui.QColor("gray") if stale else self.palette().color(QtGui.QPalette.ColorRole.Text))
        t.blockSignals(False)
        c = self.ctx.capture
        self.chart.markers = [(m.t, c.title(i)) for i, m in c.tagged()] if c is not None else []
        self.chart.update()

    # ------------------------------------------------------------------
    def _apply_filter(self):
        q = self.filter.text().strip().lower()
        for i, key in enumerate(self.order):
            r = self.rows[key]
            hay = f"{r.sig.name} {r.sig.spn or ''} {r.sig.pgn:04x} {sa_name(r.sa)}".lower()
            self.table.setRowHidden(i, bool(q) and q not in hay)

    def _item_changed(self, it):
        if it.column() != 0:
            return
        key = it.data(Qt.ItemDataRole.UserRole)
        on = it.checkState() == Qt.CheckState.Checked
        if on and key not in self.plotted:
            self.plotted.append(key)
        elif not on and key in self.plotted:
            self.plotted.remove(key)
        self._sync_chart()

    def _sync_chart(self):
        sysu = self.ctx.units
        series = []
        for key in self.plotted:
            r = self.rows.get(key)
            if r is None:
                continue
            unit = units.display_unit(r.sig.unit, sysu)
            pts = r.points
            if unit != r.sig.unit:
                pts = _ConvertedPoints(r.points, r.sig.unit, sysu)
            nd = r.sig.decimals + (1 if unit == "psi" and r.sig.decimals == 0 else 0)
            series.append((f"{key}", r.sig.name, unit, nd, pts))
        self.chart.set_series(series)

    def _reset_minmax(self):
        for r in self.rows.values():
            r.lo = r.hi = r.value
        self.refresh(force=True)

    def _clear_plots(self):
        self.plotted.clear()
        self._rebuild_table()
        self._sync_chart()

    def focus_spn(self, spn: int):
        """Select and plot the row for an SPN (from the Fault Codes tab)."""
        self.sample()
        for i, key in enumerate(self.order):
            if self.rows[key].sig.spn == spn:
                self.filter.clear()
                if key not in self.plotted:
                    self.plotted.append(key)
                    self._rebuild_table()
                    self._sync_chart()
                self.table.selectRow(i)
                self.table.scrollToItem(self.table.item(i, 1))
                return True
        self.ctx.status_message.emit(f"SPN {spn} is not being broadcast right now")
        return False


def _num(v, decimals: int) -> str:
    return "n/a" if v is None else f"{v:.{decimals}f}"


class _ConvertedPoints:
    """Read-only view of a (t, value) deque in another unit system, for the chart."""

    def __init__(self, points, unit, system):
        self.points, self.unit, self.system = points, unit, system

    def __iter__(self):
        for t, v in list(self.points):
            yield t, units.convert(v, self.unit, self.system)[0]
