"""Fault Codes tab: read DM1/DM2/... and inspect each code."""

import datetime
import html

from ..core import units
from ..core.scan import DtcRow, ScanResult
from ..j1939.addresses import sa_name
from ..j1939.constants import STATUS_ORDER, pgn_label
from ..j1939.signals import all_signals
from ..j1939.spn import PROPRIETARY_SPN_RANGE
from .flow import FlowLayout
from .qt import Qt, QtGui, QtWidgets
from .widgets import item, setup_table

COLS = ["Status", "Source", "SPN", "Description", "FMI", "FMI meaning", "Count", "Lamps", "Last seen"]
STATUS_COLORS = {"Active": "#ffd6d6", "Pending": "#ffe8cc", "Emis. active": "#ffd6d6",
                 "Stored": "#fff3bf", "Emis. stored": "#fff3bf"}


def _clock(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S") if t > 1e9 else f"{t:.1f} s"


class CodesTab(QtWidgets.QWidget):
    title = "Fault Codes"

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.rows: list[DtcRow] = []
        self._last_hist_n = None
        lay = QtWidgets.QVBoxLayout(self)

        # A flow layout so this control row wraps onto a second line on a narrow screen instead
        # of pinning the window's minimum width wide (see gui/flow.py).
        bar = FlowLayout(spacing=8)
        self.buttons = []
        for preset, label in (("active", "Read Active"), ("stored", "Read Stored"),
                              ("emissions", "Pending / Emissions"), ("full", "Full Scan")):
            b = QtWidgets.QPushButton(label)
            b.setProperty("big", True)
            b.setMinimumHeight(44)
            b.clicked.connect(lambda _=False, p=preset: ctx.run_preset(p))
            bar.addWidget(b)
            self.buttons.append(b)
        self.history = QtWidgets.QCheckBox("Include every code seen this session")
        self.history.setToolTip("Catches intermittent faults: any DTC broadcast in DM1 since you "
                                "connected stays listed, even if it has since cleared.")
        self.history.toggled.connect(self.rebuild)
        bar.addWidget(self.history)
        bar.addWidget(QtWidgets.QLabel("ECU"))
        self.ecu = QtWidgets.QComboBox()
        self.ecu.setMinimumWidth(190)
        self.ecu.addItem("All ECUs", None)
        self.ecu.currentIndexChanged.connect(lambda _: self.rebuild())
        bar.addWidget(self.ecu)
        self.summary = QtWidgets.QLabel("")
        bar.addWidget(self.summary)
        lay.addLayout(bar)

        split = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        self.table = QtWidgets.QTableWidget()
        setup_table(self.table, COLS)
        self.table.itemSelectionChanged.connect(self.show_detail)
        split.addWidget(self.table)

        detail = QtWidgets.QWidget()
        dl = QtWidgets.QVBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        self.detail = QtWidgets.QTextBrowser()
        dl.addWidget(self.detail)
        row = QtWidgets.QHBoxLayout()
        self.live_btn = QtWidgets.QPushButton("Show this parameter in Live Data")
        self.live_btn.setEnabled(False)
        self.live_btn.clicked.connect(self._open_live)
        row.addWidget(self.live_btn)
        row.addStretch(1)
        dl.addLayout(row)
        split.addWidget(detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay.addWidget(split)

        ctx.result_ready.connect(lambda r: self.rebuild())
        ctx.busy_changed.connect(self._busy)
        ctx.session_changed.connect(self._session_changed)
        ctx.units_changed.connect(lambda _: self.show_detail())
        ctx.profile_changed.connect(self.rebuild)
        self._session_changed(None)

    def _session_changed(self, session):
        enabled = self.ctx.live and not self.ctx.busy
        for b in self.buttons:
            b.setEnabled(enabled)
        self.rebuild()

    def _busy(self, busy):
        for b in self.buttons:
            b.setEnabled(not busy and self.ctx.live)
        if busy:
            self.summary.setText("Reading...")

    # ------------------------------------------------------------------
    def _session_rows(self) -> list[DtcRow]:
        s = self.ctx.session
        if s is None:
            return []
        with s.lock:
            hist = list(s.dtc_hist.values())
        return [DtcRow(h.pgn, h.sa, h.spn, h.fmi, h.oc, h.cm, h.alt_spn, h.lamps, h.first, h.last, h.seen)
                for h in hist]

    def rebuild(self):
        res: ScanResult | None = self.ctx.result
        rows = list(res.rows) if res else []
        quiet = list(res.quiet) if res else []
        if self.history.isChecked():
            have = {(r.pgn, r.sa, r.spn, r.fmi) for r in rows}
            extra = [r for r in self._session_rows() if (r.pgn, r.sa, r.spn, r.fmi) not in have]
            rows += extra
            quiet = [q for q in quiet if not any(r.pgn == q.pgn and r.sa == q.sa for r in extra)]
            rows.sort(key=lambda r: (STATUS_ORDER.get(r.status, 9), r.sa, r.spn, r.fmi))
        self._sync_ecu_choices({r.sa for r in rows} | {q.sa for q in quiet})
        only = self.ecu.currentData()
        if only is not None:
            rows = [r for r in rows if r.sa == only]
            quiet = [q for q in quiet if q.sa == only]
        prev = self.selected()
        prev_key = (prev.pgn, prev.sa, prev.spn, prev.fmi) if prev else None
        self.rows = rows

        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for r in rows:
            i = t.rowCount()
            t.insertRow(i)
            vals = [r.status, sa_name(r.sa), r.spn_label, r.spn_text, r.fmi, r.fmi_text, r.oc_text,
                    str(r.lamps), _clock(r.last)]
            bg = QtGui.QColor(STATUS_COLORS.get(r.status, "#ffffff"))
            for c, v in enumerate(vals):
                it = item(v, c in (2, 4, 6), data=i if c == 0 else None)
                it.setBackground(bg)
                it.setForeground(QtGui.QColor("#212529"))
                t.setItem(i, c, it)
        for q in quiet:
            i = t.rowCount()
            t.insertRow(i)
            for c, v in enumerate([q.status, sa_name(q.sa), "-", "No codes reported", "", "", "",
                                   str(q.lamps), ""]):
                it = item(v)
                it.setForeground(QtGui.QColor("gray"))
                t.setItem(i, c, it)
        if res is None:
            self.summary.setText("Connect, then read codes." if not self.ctx.session else "No scan yet.")
        else:
            n_act = sum(1 for r in rows if r.status in ("Active", "Emis. active"))
            self.summary.setText(f"{res.title}: {len(rows)} code(s), {n_act} active"
                                 + ("  (listen-only)" if res.listen_only and not res.offline else ""))
        if not rows and not quiet and res is not None:
            t.insertRow(0)
            t.setItem(0, 3, item("No DM responses received"))
        for i, r in enumerate(rows):            # keep the user's selection across refreshes
            if (r.pgn, r.sa, r.spn, r.fmi) == prev_key:
                t.selectRow(i)
                break
        self.show_detail()

    def _sync_ecu_choices(self, sas: set[int]):
        """Offer every ECU that reported (or reported nothing), keeping the current choice."""
        have = {self.ecu.itemData(i) for i in range(1, self.ecu.count())}
        current = self.ecu.currentData()
        if current is not None:
            sas = sas | {current}
        if sas == have:
            return
        self.ecu.blockSignals(True)
        while self.ecu.count() > 1:
            self.ecu.removeItem(1)
        for sa in sorted(sas):
            self.ecu.addItem(sa_name(sa), sa)
        self.ecu.setCurrentIndex(max(self.ecu.findData(current), 0))
        self.ecu.blockSignals(False)

    def selected(self) -> DtcRow | None:
        sel = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not sel:
            return None
        it = self.table.item(sel[0].row(), 0)
        idx = it.data(Qt.ItemDataRole.UserRole) if it else None
        return self.rows[idx] if idx is not None and idx < len(self.rows) else None

    def _signal_for(self, spn: int):
        return next((s for s in all_signals() if s.spn == spn), None)

    def show_detail(self):
        r = self.selected()
        if r is None:
            self.live_btn.setEnabled(False)
            self.detail.setHtml("<p style='color:gray'>Select a code to see details.</p>")
            return
        e = html.escape
        parts = [f"<h3>SPN {r.spn} / FMI {r.fmi} &mdash; {e(r.spn_text)}</h3>",
                 f"<p><b>{e(r.fmi_text)}</b></p><table cellspacing=4>"]
        info = [
            ("Status", f"{r.status} ({pgn_label(r.pgn).split(' ')[0]})"),
            ("Source", sa_name(r.sa)),
            ("Occurrence count", r.oc_text),
            ("Lamps", str(r.lamps)),
            ("First / last seen", f"{_clock(r.first)} / {_clock(r.last)}  ({r.seen} message(s))"),
        ]
        if r.alt_spn is not None:
            info.append(("Old SPN format", f"CM bit set - ECU may use the pre-2000 byte order; "
                                           f"alternate SPN {r.alt_spn}"))
        if r.spn in PROPRIETARY_SPN_RANGE:
            info.append(("Note", "Manufacturer-specific SPN: look it up in the OEM fault list, or add "
                                 "it to a machine profile so it's named next time."))
        parts += [f"<tr><td><b>{e(k)}</b></td><td>{e(v)}</td></tr>" for k, v in info]
        parts.append("</table>")

        res = self.ctx.result
        if res:
            ff = [f for sa, (frames, _) in res.freeze_frames().items() if sa == r.sa for f in frames
                  if f.dtc.spn == r.spn and f.dtc.fmi == r.fmi]
            for f in ff:
                sys_ = self.ctx.units
                parts.append(
                    "<p><b>Freeze frame</b> (conditions when the code set): "
                    f"{units.fmt(f.rpm, 'rpm', 0)}, load {units.fmt(f.load_pct, '%', 0)}, "
                    f"coolant {units.fmt(f.coolant_c, '°C', 0, sys_)}, boost {units.fmt(f.boost_kpa, 'kPa', 0, sys_)}, "
                    f"speed {units.fmt(f.speed_kmh, 'km/h', 1, sys_)}</p>")
        sig = self._signal_for(r.spn)
        self.live_btn.setEnabled(sig is not None and self.ctx.session is not None)
        self.live_btn.setText(f"Show {sig.name} in Live Data" if sig else "No live parameter for this SPN")
        self.detail.setHtml("".join(parts))

    def _open_live(self):
        r = self.selected()
        if r:
            self.ctx.show_signal.emit(r.spn)

    def refresh(self):
        if self.history.isChecked() and self.ctx.live:
            n = len(self._session_rows())
            if n != self._last_hist_n:
                self._last_hist_n = n
                self.rebuild()

