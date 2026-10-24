"""Live Data tab: decoded parameters with min/max and a rolling chart.

The screen only: values, min/max, history and the picked signals live in ``core.live.LiveData``
(J1939 broadcast signals) or ``core.live.ObdLiveData`` (OBD-II PIDs), which the on-device UI
shares. The OBD-II tab embeds a second instance of this tab over the OBD-II model.
"""

import time

from ..core import units
from ..core.live import LiveData
from .qt import Qt, QtCore, QtGui, QtWidgets
from .widgets import StripChart, item, setup_table

COLS = ["Plot", "Signal", "Group", "Value", "Unit", "Min", "Max", "Source", "PGN", "SPN", "Age"]
C_PLOT, C_NAME, C_GROUP, C_VALUE, C_UNIT, C_MIN, C_MAX, C_SRC, C_PGN, C_SPN, C_AGE = range(len(COLS))
ALL_GROUPS = "All groups"
STALE_AFTER = 3.0


class LiveTab(QtWidgets.QWidget):
    title = "Live Data"

    def __init__(self, ctx, parent=None, model: LiveData | None = None, settings_prefix: str = "live",
                 empty_text: str = "No standard live parameters seen yet. Key on / engine running?",
                 filter_hint: str = "Filter signals (name, SPN, PGN)..."):
        super().__init__(parent)
        self.ctx = ctx
        self.model = model if model is not None else LiveData()
        self.prefix = settings_prefix
        self.empty_text = empty_text
        # The model's own containers (cleared in place), under the names the tab always used.
        self.rows, self.order, self.plotted = self.model.rows, self.model.order, self.model.selected
        lay = QtWidgets.QVBoxLayout(self)

        bar = self.controls = QtWidgets.QHBoxLayout()
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText(filter_hint)
        self.filter.textChanged.connect(self._apply_filter)
        bar.addWidget(self.filter, 2)
        self.group = QtWidgets.QComboBox()
        self.group.addItem(ALL_GROUPS)
        self.group.setMinimumWidth(150)
        self.group.setToolTip("Show one group of parameters (groups come from the machine profile)")
        self.group.currentIndexChanged.connect(lambda _: self._apply_filter())
        bar.addWidget(self.group)
        reset = QtWidgets.QPushButton("Reset min/max")
        reset.clicked.connect(self._reset_minmax)
        bar.addWidget(reset)
        # Two view toggles (remembered between sessions): list only the ticked signals, and
        # hide the chart to give the list the whole tab on a small screen.
        settings = QtCore.QSettings()
        self.only_checked = QtWidgets.QPushButton()
        self.only_checked.setCheckable(True)
        self.only_checked.setToolTip("List only the signals ticked in the Plot column")
        self.only_checked.toggled.connect(self._only_checked_toggled)
        bar.addWidget(self.only_checked)
        self.hide_plots = QtWidgets.QPushButton()
        self.hide_plots.setCheckable(True)
        self.hide_plots.setToolTip("Hide the chart so the list fills the tab (ticked signals keep recording)")
        self.hide_plots.toggled.connect(self._hide_plots_toggled)
        bar.addWidget(self.hide_plots)
        lay.addLayout(bar)

        split = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        self.table = QtWidgets.QTableWidget()
        # The signal name gets a fixed, readable width and Group takes up the slack: with the Log
        # panel open on a 1280-wide laptop the table has ~850 px, and a stretching name column
        # used to be squeezed to "En...".
        cols = list(COLS)
        cols[C_SRC], (cols[C_PGN], cols[C_SPN]) = self.model.source_header, self.model.id_headers
        setup_table(self.table, cols, stretch=(C_GROUP,))
        for c, w in ((C_PLOT, 44), (C_NAME, 210), (C_VALUE, 90), (C_UNIT, 55), (C_MIN, 70), (C_MAX, 70),
                     (C_SRC, 110), (C_PGN, 55), (C_SPN, 55), (C_AGE, 50)):
            self.table.setColumnWidth(c, w)
        self.table.setColumnHidden(C_SPN, not cols[C_SPN])
        self.table.itemChanged.connect(self._item_changed)
        split.addWidget(self.table)
        # The chart and its own controls, in one pane that "Hide plots" hides.
        self.chart_pane = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(self.chart_pane)
        cl.setContentsMargins(0, 0, 0, 0)
        cbar = QtWidgets.QHBoxLayout()
        cbar.addWidget(QtWidgets.QLabel("Chart window"))
        self.window = QtWidgets.QComboBox()
        for label, secs in (("30 s", 30), ("1 min", 60), ("2 min", 120), ("5 min", 300), ("10 min", 600)):
            self.window.addItem(label, secs)
        self.window.setCurrentIndex(1)
        self.window.currentIndexChanged.connect(lambda _: self.chart.set_window(self.window.currentData()))
        cbar.addWidget(self.window)
        self.pause = QtWidgets.QPushButton("Pause chart")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(lambda on: self.chart.set_paused(on))
        cbar.addWidget(self.pause)
        clear = QtWidgets.QPushButton("Clear plots")
        clear.setToolTip("Untick every signal")
        clear.clicked.connect(self._clear_plots)
        cbar.addWidget(clear)
        cbar.addStretch(1)
        cl.addLayout(cbar)
        self.chart = StripChart()
        cl.addWidget(self.chart, 1)
        split.addWidget(self.chart_pane)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay.addWidget(split)
        self.empty = QtWidgets.QLabel(empty_text)
        self.empty.setStyleSheet("color: gray")
        lay.addWidget(self.empty)

        self.only_checked.setChecked(settings.value(f"{self.prefix}/only_checked", "false") in (True, "true"))
        self.hide_plots.setChecked(settings.value(f"{self.prefix}/hide_plots", "false") in (True, "true"))
        self._only_checked_toggled(self.only_checked.isChecked(), save=False)
        self._hide_plots_toggled(self.hide_plots.isChecked(), save=False)

        ctx.session_changed.connect(self._session_changed)
        ctx.units_changed.connect(lambda _: self._units_changed())
        if type(self.model) is LiveData:       # J1939: signals come from the profile; codes link by SPN
            ctx.profile_changed.connect(lambda: self._session_changed(ctx.session))
            ctx.show_signal.connect(self.focus_spn)

    def _only_checked_toggled(self, on: bool, save: bool = True):
        self.only_checked.setText("Show unticked" if on else "Hide unticked")
        if save:
            QtCore.QSettings().setValue(f"{self.prefix}/only_checked", on)
        self._apply_filter()
        self.refresh(force=True)

    def _hide_plots_toggled(self, on: bool, save: bool = True):
        self.hide_plots.setText("Show plots" if on else "Hide plots")
        self.chart_pane.setVisible(not on)
        if save:
            QtCore.QSettings().setValue(f"{self.prefix}/hide_plots", on)

    def _session_changed(self, session):
        self.model.clear()
        self.table.setRowCount(0)
        self.chart.follow_data = bool(session and session.offline)
        if session is not None and session.offline:
            self._load_history(session)
        self._sync_chart()
        self.sample()
        self.refresh()

    def _load_history(self, session):
        """For a decoded log, rebuild each signal's full history from the raw frames."""
        self.model.load_history(session)
        self._rebuild_table()

    def _units_changed(self):
        self._sync_chart()
        self.refresh(force=True)

    # ------------------------------------------------------------------
    def sample(self):
        """Pull the latest value of every signal from the session (called ~5x/s, any tab)."""
        s = self.ctx.session
        if s is not None and self.model.sample(s):
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
            t.setItem(i, C_PLOT, chk)
            name = item(r.sig.name)
            if r.sig.low is not None or r.sig.high is not None:
                name.setToolTip(f"Normal range: {_num(r.sig.low, r.sig.decimals)} to "
                                f"{_num(r.sig.high, r.sig.decimals)} {r.sig.unit}")
            t.setItem(i, C_NAME, name)
            t.setItem(i, C_GROUP, item(r.sig.group))
            t.setItem(i, C_SRC, item(self.model.source_label(r.sa)))
            ref, spn = self.model.ids(r)
            t.setItem(i, C_PGN, item(ref, True))
            t.setItem(i, C_SPN, item(spn, True))
            for c in (C_VALUE, C_UNIT, C_MIN, C_MAX, C_AGE):
                t.setItem(i, c, item("", c != C_UNIT))
        t.blockSignals(False)
        self._sync_groups()
        self._apply_filter()
        self.refresh(force=True)

    def refresh(self, force: bool = False):
        if not force and not self.isVisible():
            return
        if not self.order:
            self.empty.setText(self.empty_text)
        elif self.only_checked.isChecked() and not self.plotted:
            self.empty.setText("Nothing is ticked. Tick signals in the Plot column, or press Show unticked.")
        self.empty.setVisible(not self.order or (self.only_checked.isChecked() and not self.plotted))
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
            nd = units.display_decimals(sig.unit, unit, sig.decimals)
            age = now - r.t
            stale = not offline and age > STALE_AFTER
            if sig.enum:
                cells = {C_VALUE: sig.text(r.value) or _num(r.value, 0), C_UNIT: "",
                         C_MIN: sig.text(r.lo) or _num(r.lo, 0), C_MAX: sig.text(r.hi) or _num(r.hi, 0)}
            else:
                cells = {C_VALUE: _num(val, nd), C_UNIT: unit, C_MIN: _num(lo, nd), C_MAX: _num(hi, nd)}
            cells[C_AGE] = "" if offline else f"{age:.1f}s"
            for c, text in cells.items():
                it = t.item(i, c)
                if it is not None and it.text() != text:
                    it.setText(text)
            it = t.item(i, C_VALUE)
            if it is not None:
                if stale:
                    it.setForeground(QtGui.QColor("gray"))
                elif sig.out_of_range(r.value):
                    it.setForeground(QtGui.QColor("#e03131"))
                else:
                    it.setForeground(self.palette().color(QtGui.QPalette.ColorRole.Text))
        t.blockSignals(False)
        c = self.ctx.capture
        self.chart.markers = [(m.t, c.title(i)) for i, m in c.tagged()] if c is not None else []
        self.chart.update()

    # ------------------------------------------------------------------
    def _sync_groups(self):
        groups = self.model.groups()
        have = [self.group.itemText(i) for i in range(1, self.group.count())]
        if groups == have:
            return
        current = self.group.currentText()
        self.group.blockSignals(True)
        self.group.clear()
        self.group.addItem(ALL_GROUPS)
        self.group.addItems(groups)
        self.group.setCurrentIndex(max(self.group.findText(current), 0))
        self.group.blockSignals(False)

    def _apply_filter(self):
        q = self.filter.text()
        group = self.group.currentText()
        group = None if group == ALL_GROUPS else group
        only = self.only_checked.isChecked()
        for i, key in enumerate(self.order):
            self.table.setRowHidden(i, not self.model.matches(key, q, group, only))

    def _item_changed(self, it):
        if it.column() != C_PLOT:
            return
        key = it.data(Qt.ItemDataRole.UserRole)
        self.model.select(key, it.checkState() == Qt.CheckState.Checked)
        self._sync_chart()
        if self.only_checked.isChecked():
            self._apply_filter()           # an unticked row leaves the list
            self.refresh(force=True)

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
            nd = units.display_decimals(r.sig.unit, unit, r.sig.decimals)
            series.append((f"{key}", r.sig.name, unit, nd, pts))
        self.chart.set_series(series)

    def _reset_minmax(self):
        self.model.reset_minmax()
        self.refresh(force=True)

    def _clear_plots(self):
        self.plotted.clear()
        self._rebuild_table()
        self._sync_chart()

    def focus_keys(self, keys: list) -> bool:
        """Tick (plot) these rows and select the first: a fault code's sensor values."""
        keys = [k for k in keys if k in self.rows]
        if not keys:
            return False
        self.filter.clear()
        self.group.setCurrentIndex(0)
        for k in keys:
            self.model.select(k)
        self._rebuild_table()
        self._sync_chart()
        i = self.order.index(keys[0])
        self.table.selectRow(i)
        self.table.scrollToItem(self.table.item(i, C_NAME))
        return True

    def focus_spn(self, spn: int):
        """Select and plot the row for an SPN (from the Fault Codes tab)."""
        self.sample()
        key = self.model.find_spn(spn)
        if key is not None:
            self.filter.clear()
            self.group.setCurrentIndex(0)
            if key not in self.plotted:
                self.model.select(key)
                self._rebuild_table()
                self._sync_chart()
            i = self.order.index(key)
            self.table.selectRow(i)
            self.table.scrollToItem(self.table.item(i, C_NAME))
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
