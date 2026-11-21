"""Live Data tab: decoded parameters with min/max and a rolling chart.

The screen only: values, min/max, history and the picked signals live in ``core.live.LiveData``
(J1939 broadcast signals) or ``core.live.ObdLiveData`` (OBD-II PIDs), which the on-device UI
shares. The OBD-II tab embeds a second instance of this tab over the OBD-II model.

**Group by** puts the rows under a heading per controller (the source address / module) or per
profile group (``core.live.sections``); clicking a heading folds it away (``core.live.Collapsed``).
Both, and which headings are folded, are remembered.
"""

import json
import time

from ..core import units
from ..core.live import GROUP_KIND, GROUP_NONE, GROUP_SOURCE, Collapsed, LiveData, grouping_label, sections
from . import themes
from .flow import FlowLayout
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

        # wraps onto a second line on a narrow screen instead of pinning the window wide (gui/flow.py);
        # the OBD-II tab adds its own buttons to it
        bar = self.controls = FlowLayout(spacing=6)
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText(filter_hint)
        self.filter.setMinimumWidth(220)
        self.filter.textChanged.connect(self._apply_filter)
        bar.addWidget(self.filter)
        self.group = QtWidgets.QComboBox()
        self.group.addItem(ALL_GROUPS)
        self.group.setMinimumWidth(150)
        self.group.setToolTip("Show one group of parameters (groups come from the machine profile)")
        self.group.currentIndexChanged.connect(lambda _: self._apply_filter())
        bar.addWidget(self.group)
        self.group_by = QtWidgets.QComboBox()
        for by in (GROUP_NONE, GROUP_SOURCE, GROUP_KIND):
            self.group_by.addItem(grouping_label(by, self.model), by)
        self.group_by.setToolTip("Put the values under a heading per controller (who sends them) or per group; "
                                 "click a heading to fold it away")
        bar.addWidget(self.group_by)
        self.fold_all = QtWidgets.QPushButton("Collapse all")
        self.fold_all.setToolTip("Fold every heading away, or open them all again")
        self.fold_all.clicked.connect(self._fold_all)
        bar.addWidget(self.fold_all)
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
        # Starting widths until the first rows arrive; then the columns fit their contents
        # (setup_table's rule) and stay where the user drags them.
        cols = list(COLS)
        cols[C_SRC], (cols[C_PGN], cols[C_SPN]) = self.model.source_header, self.model.id_headers
        setup_table(self.table, cols)
        for c, w in ((C_PLOT, 44), (C_NAME, 210), (C_VALUE, 90), (C_UNIT, 55), (C_MIN, 70), (C_MAX, 70),
                     (C_SRC, 110), (C_PGN, 55), (C_SPN, 55), (C_AGE, 50)):
            self.table.setColumnWidth(c, w)
        self.table.setColumnHidden(C_SPN, not cols[C_SPN])
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellClicked.connect(self._cell_clicked)
        split.addWidget(self.table)
        # What each table row is: a value (row_of: key -> row) or a section heading (headers).
        self.row_of: dict = {}
        self.headers: dict = {}
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

        try:
            self.collapsed = Collapsed(json.loads(settings.value(f"{self.prefix}/collapsed", "{}") or "{}"))
        except (TypeError, ValueError):
            self.collapsed = Collapsed()
        saved_by = settings.value(f"{self.prefix}/group_by", GROUP_NONE)
        self.group_by.setCurrentIndex(max(self.group_by.findData(saved_by), 0))
        self.fold_all.setVisible(self.grouping != GROUP_NONE)
        self.group_by.currentIndexChanged.connect(lambda _: self._grouping_changed())
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

    @property
    def grouping(self) -> str:
        return self.group_by.currentData() or GROUP_NONE

    def _grouping_changed(self):
        QtCore.QSettings().setValue(f"{self.prefix}/group_by", self.grouping)
        self.fold_all.setVisible(self.grouping != GROUP_NONE)
        self._rebuild_table()

    def _save_collapsed(self):
        QtCore.QSettings().setValue(f"{self.prefix}/collapsed", json.dumps(self.collapsed.to_dict()))

    def toggle_section(self, sid: str):
        """Fold or unfold one heading (what clicking it does)."""
        self.collapsed.toggle(self.grouping, sid)
        self._save_collapsed()
        self._apply_filter()
        self.refresh(force=True)

    def _fold_all(self):
        ids = [sec.id for sec in self.headers.values()]
        fold = not all(self.collapsed.is_folded(self.grouping, i) for i in ids)
        self.collapsed.fold_all(self.grouping, ids, fold)
        self._save_collapsed()
        self._apply_filter()
        self.refresh(force=True)

    def _cell_clicked(self, row: int, _col: int):
        sec = self.headers.get(row)
        if sec is not None:
            self.toggle_section(sec.id)

    def _hide_plots_toggled(self, on: bool, save: bool = True):
        self.hide_plots.setText("Show plots" if on else "Hide plots")
        self.chart_pane.setVisible(not on)
        if save:
            QtCore.QSettings().setValue(f"{self.prefix}/hide_plots", on)

    def _session_changed(self, session):
        self.model.clear()
        self.table.setRowCount(0)
        self.row_of, self.headers = {}, {}
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
        t.clearSpans()
        display: list = []              # (section or None, key or None) per table row
        if self.grouping == GROUP_NONE:
            display = [(None, k) for k in self.order]
        else:
            for sec in sections(self.model, self.grouping):
                display.append((sec, None))
                display += [(sec, k) for k in sec.keys]
        self.row_of, self.headers = {}, {}
        t.setRowCount(len(display))
        bg, fg = themes.tint("muted")
        bold = QtGui.QFont(t.font())
        bold.setBold(True)
        for i, (sec, key) in enumerate(display):
            if key is None:             # a section heading, across the whole row
                self.headers[i] = sec
                head = QtWidgets.QTableWidgetItem(sec.title)
                head.setFlags(Qt.ItemFlag.ItemIsEnabled)
                head.setFont(bold)
                head.setBackground(bg)
                head.setForeground(fg)
                head.setToolTip("Click to fold or unfold")
                t.setItem(i, 0, head)
                for c in range(1, t.columnCount()):
                    t.setItem(i, c, None)
                t.setSpan(i, 0, 1, t.columnCount())
                continue
            self.row_of[key] = i
            r = self.rows[key]
            chk = QtWidgets.QTableWidgetItem()
            if getattr(r.sig, "is_text", False):        # a message, not a number: nothing to plot
                chk.setFlags(Qt.ItemFlag.ItemIsEnabled)
                chk.setToolTip("Text: shows the latest message (hover the value for recent ones)")
            else:
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
        for key, i in self.row_of.items():
            r = self.rows.get(key)
            if r is None or t.isRowHidden(i):
                continue
            sig = r.sig
            val, unit = units.convert(r.value, sig.unit, sysu)
            lo = units.convert(r.lo, sig.unit, sysu)[0]
            hi = units.convert(r.hi, sig.unit, sysu)[0]
            nd = units.display_decimals(sig.unit, unit, sig.decimals)
            age = now - r.t
            stale = not offline and age > STALE_AFTER
            if getattr(sig, "is_text", False):
                cells = {C_VALUE: r.value or "", C_UNIT: "", C_MIN: "", C_MAX: ""}
            elif sig.enum:
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
            if it is not None and getattr(sig, "is_text", False):
                tip = "Recent messages, newest last:\n" + "\n".join(f"  {m}" for m in r.recent) if r.recent else ""
                if it.toolTip() != tip:
                    it.setToolTip(tip)
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
        shown = {key: self.model.matches(key, q, group, only) for key in self.row_of}
        for i, sec in self.headers.items():
            folded = self.collapsed.is_folded(self.grouping, sec.id)
            n = sum(1 for k in sec.keys if shown.get(k))
            self.table.setRowHidden(i, n == 0)
            for k in sec.keys:
                self.table.setRowHidden(self.row_of[k], folded or not shown.get(k))
            text = f"{'▸' if folded else '▾'}  {sec.title}   ·   {n} value{'s' if n != 1 else ''}"
            it = self.table.item(i, 0)
            if it is not None and it.text() != text:
                it.setText(text)
        if not self.headers:
            for key, i in self.row_of.items():
                self.table.setRowHidden(i, not shown[key])

    def _item_changed(self, it):
        if it.column() != C_PLOT:
            return
        key = it.data(Qt.ItemDataRole.UserRole)
        if key is None:                 # a heading
            return
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
            if r is None or getattr(r.sig, "is_text", False):
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
        self._show_row(keys[0])
        return True

    def _show_row(self, key):
        """Select a value's row, unfolding its heading first."""
        for sec in self.headers.values():
            if key in sec.keys and self.collapsed.is_folded(self.grouping, sec.id):
                self.collapsed.set(self.grouping, sec.id, False)
                self._save_collapsed()
                self._apply_filter()
        i = self.row_of[key]
        self.table.selectRow(i)
        self.table.scrollToItem(self.table.item(i, C_NAME))

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
            self._show_row(key)
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
