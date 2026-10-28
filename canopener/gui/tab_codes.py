"""Fault Codes tab: read DM1/DM2/... and inspect each code. CANopen errors share the table: EMCY
errors a node broadcasts are Active, its error history (object 1003) is Stored."""

import datetime
import html
import time

from ..core import canopen, j1939_clear, scan, units
from ..core.canopen import CanopenCode
from ..core.scan import DtcRow, ScanResult
from ..j1939.addresses import sa_name
from ..j1939.constants import STATUS_ORDER, pgn_label
from ..j1939.signals import all_signals
from ..j1939.spn import PROPRIETARY_SPN_RANGE
from .flow import FlowLayout
from .qt import Qt, QtGui, QtWidgets
from .widgets import item, setup_table

COLS = ["Status", "Source", "SPN / code", "Description", "FMI / reg.", "Meaning", "Count", "Lamps", "Last seen"]
STATUS_COLORS = {"Active": "#ffd6d6", "Pending": "#ffe8cc", "Emis. active": "#ffd6d6",
                 "Stored": "#fff3bf", "Emis. stored": "#fff3bf", "Cleared": "#e9ecef"}


def _clock(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S") if t > 1e9 else f"{t:.1f} s"


class CodesTab(QtWidgets.QWidget):
    title = "Fault Codes"

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.rows: list[DtcRow] = []
        self.co_rows: list[CanopenCode] = []
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
        self.clear_btn = QtWidgets.QToolButton()
        self.clear_btn.setText("Clear Codes…")
        self.clear_btn.setMinimumHeight(44)
        self.clear_btn.setToolTip("Clear J1939 fault codes (DM11 active / DM3 stored) on every module, or only "
                                  "the one picked under ECU, or a CANopen node's error history. Asks first.")
        self.clear_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(self.clear_btn)
        for which, label in ((j1939_clear.ACTIVE, "Clear active codes (DM11)…"),
                             (j1939_clear.STORED, "Clear stored codes (DM3)…"),
                             (j1939_clear.BOTH, "Clear active and stored codes…")):
            menu.addAction(label, lambda w=which: self.clear_codes(w))
        menu.addSeparator()
        menu.addAction("Clear CANopen error history…", self.clear_canopen)
        self.clear_btn.setMenu(menu)
        bar.addWidget(self.clear_btn)
        self.buttons.append(self.clear_btn)
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
    def clear_codes(self, which: str):
        """Clear codes on every module, or the one picked under ECU; asks first (default No)."""
        ctx = self.ctx
        s = ctx.session
        if not ctx.live or ctx.busy:
            return
        if s.listen_only:
            QtWidgets.QMessageBox.information(self, "Clear codes", "Listen-only is on: clearing codes has to "
                                              "transmit. Turn listen-only off in Connection first.")
            return
        da = self.ecu.currentData()
        if da is not None and da > 0xFF:
            QtWidgets.QMessageBox.information(self, "Clear codes", "That's a CANopen node: use Clear CANopen "
                                              "error history… instead (or pick All ECUs).")
            return
        da = j1939_clear.GLOBAL_SA if da is None else da
        r = QtWidgets.QMessageBox.warning(self, "Clear codes", j1939_clear.warning(which, da),
                                          QtWidgets.QMessageBox.StandardButton.Yes
                                          | QtWidgets.QMessageBox.StandardButton.No,
                                          QtWidgets.QMessageBox.StandardButton.No)
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        pgns = {j1939_clear.ACTIVE: scan.ACTIVE, j1939_clear.STORED: scan.STORED}.get(which, scan.ACTIVE + scan.STORED)

        def work():
            result = j1939_clear.clear_codes(s, which, da)
            time.sleep(1.2)                 # the next DM1 broadcast shows what's still active
            return result, scan.run_scan(s, pgns, title="Codes after clearing")

        def done(value):
            result, after = value
            ctx.result_ready.emit(after)
            ctx.status_message.emit("Clear codes: " + "; ".join(result.lines()[:-1]))
            box = QtWidgets.QMessageBox.information if result.ok else QtWidgets.QMessageBox.warning
            box(self, "Clear codes", "\n".join(result.lines()))
        ctx.run_task("Clearing codes…", work, done)

    def clear_canopen(self):
        """Clear the error history (object 1003) of the node picked under ECU, or of every node
        with stored errors; asks first (default No), then reads the history again."""
        ctx = self.ctx
        s = ctx.session
        if not ctx.live or ctx.busy:
            return
        if s.listen_only:
            QtWidgets.QMessageBox.information(self, "Clear CANopen errors", "Listen-only is on: clearing has to "
                                              "transmit. Turn listen-only off in Connection first.")
            return
        pick = self.ecu.currentData()
        if pick is not None and canopen.is_node(pick):
            nodes = [pick - canopen.NODE_SRC]
        else:
            nodes = sorted({c.node for c in canopen.codes(s) if c.status == "Stored"})
        if not nodes:
            QtWidgets.QMessageBox.information(self, "Clear CANopen errors", "No CANopen error history read yet: "
                                              "press Read Stored first, or pick a node under ECU.")
            return
        text = "\n\n".join(canopen.warning_clear(n) for n in nodes[:1])
        if len(nodes) > 1:
            text += f"\n\n(The same for {len(nodes)} nodes: " + ", ".join(f"{n:02X}" for n in nodes) + ".)"
        r = QtWidgets.QMessageBox.warning(self, "Clear CANopen errors", text,
                                          QtWidgets.QMessageBox.StandardButton.Yes
                                          | QtWidgets.QMessageBox.StandardButton.No,
                                          QtWidgets.QMessageBox.StandardButton.No)
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        def work():
            client = canopen.CanopenClient(s)
            done = []
            try:
                for n in nodes:
                    try:
                        client.clear_error_history(n)
                        done.append(f"node {n:02X}: cleared")
                    except canopen.SdoError as e:
                        done.append(f"node {n:02X}: {e}")
            finally:
                client.close()
            return done, scan.run_scan(s, scan.STORED, 0.5, title="Codes after clearing")

        def finished(value):
            lines, after = value
            ctx.result_ready.emit(after)
            QtWidgets.QMessageBox.information(self, "Clear CANopen errors", "\n".join(lines))
        ctx.run_task("Clearing CANopen error history…", work, finished)

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
        co = list(res.canopen) if res else []
        if self.history.isChecked():
            have = {(r.pgn, r.sa, r.spn, r.fmi) for r in rows}
            extra = [r for r in self._session_rows() if (r.pgn, r.sa, r.spn, r.fmi) not in have]
            rows += extra
            quiet = [q for q in quiet if not any(r.pgn == q.pgn and r.sa == q.sa for r in extra)]
            rows.sort(key=lambda r: (STATUS_ORDER.get(r.status, 9), r.sa, r.spn, r.fmi))
            if self.ctx.session is not None:
                have_co = {(c.status, c.node, c.code) for c in co}
                co += [c for c in canopen.codes(self.ctx.session, history=True)
                       if c.status != "Stored" and (c.status, c.node, c.code) not in have_co]
        self._sync_ecu_choices({r.sa for r in rows} | {q.sa for q in quiet} | {c.src for c in co})
        only = self.ecu.currentData()
        if only is not None:
            rows = [r for r in rows if r.sa == only]
            quiet = [q for q in quiet if q.sa == only]
            co = [c for c in co if c.src == only]
        prev = self.selected()
        prev_key = self._key(prev) if prev else None
        self.rows, self.co_rows = rows, co
        self._items = list(rows) + list(co)

        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for r in rows:
            i = t.rowCount()
            t.insertRow(i)
            self._fill(i, [r.status, sa_name(r.sa), r.spn_label, r.spn_text, r.fmi, r.fmi_text, r.oc_text,
                           str(r.lamps), _clock(r.last)], r.status)
        for c in co:
            i = t.rowCount()
            t.insertRow(i)
            reg = "" if c.register is None else f"{c.register:02X}"
            meaning = c.register_text.split(" ", 1)[1].strip("()") if c.register is not None else ""
            self._fill(i, [c.status, canopen.source_name(c.src), f"EMCY {c.code:04X}", c.text, reg, meaning,
                           str(c.seen) if c.last else "", "", _clock(c.last) if c.last else ""], c.status)
        for q in quiet:
            i = t.rowCount()
            t.insertRow(i)
            for col, v in enumerate([q.status, sa_name(q.sa), "-", "No codes reported", "", "", "",
                                     str(q.lamps), ""]):
                it = item(v)
                it.setForeground(QtGui.QColor("gray"))
                t.setItem(i, col, it)
        if res is None:
            self.summary.setText("Connect, then read codes." if not self.ctx.session else "No scan yet.")
        else:
            n_act = sum(1 for r in rows if r.status in ("Active", "Emis. active")) + \
                sum(1 for c in co if c.status == "Active")
            self.summary.setText(f"{res.title}: {len(rows) + len(co)} code(s), {n_act} active"
                                 + ("  (listen-only)" if res.listen_only and not res.offline else ""))
        if not rows and not quiet and not co and res is not None:
            t.insertRow(0)
            t.setItem(0, 3, item("No codes received" if res.j1939_skipped else "No DM responses received"))
        for i, r in enumerate(self._items):            # keep the user's selection across refreshes
            if self._key(r) == prev_key:
                t.selectRow(i)
                break
        self.show_detail()

    def _fill(self, i: int, vals: list, status: str):
        bg = QtGui.QColor(STATUS_COLORS.get(status, "#ffffff"))
        for c, v in enumerate(vals):
            it = item(v, c in (2, 4, 6), data=i if c == 0 else None)
            it.setBackground(bg)
            it.setForeground(QtGui.QColor("#212529"))
            self.table.setItem(i, c, it)

    @staticmethod
    def _key(r):
        if isinstance(r, CanopenCode):
            return ("co", r.status, r.node, r.code)
        return (r.pgn, r.sa, r.spn, r.fmi)

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
            self.ecu.addItem(canopen.source_name(sa), sa)
        self.ecu.setCurrentIndex(max(self.ecu.findData(current), 0))
        self.ecu.blockSignals(False)

    def selected(self) -> DtcRow | CanopenCode | None:
        sel = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not sel:
            return None
        it = self.table.item(sel[0].row(), 0)
        idx = it.data(Qt.ItemDataRole.UserRole) if it else None
        items = getattr(self, "_items", self.rows)
        return items[idx] if idx is not None and idx < len(items) else None

    def _signal_for(self, spn: int):
        return next((s for s in all_signals() if s.spn == spn), None)

    def show_detail(self):
        r = self.selected()
        if r is None:
            self.live_btn.setEnabled(False)
            self.detail.setHtml("<p style='color:gray'>Select a code to see details.</p>")
            return
        if isinstance(r, CanopenCode):
            self._canopen_detail(r)
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

    def _canopen_detail(self, c: CanopenCode):
        e = html.escape
        what = {"Active": "Active: the node reported it (EMCY) and hasn't reported it cleared",
                "Stored": "Stored: in the node's error history (object 1003)",
                "Cleared": "Seen this session (EMCY), cleared since"}[c.status]
        info = [("Status", what), ("Source", canopen.source_name(c.src)), ("Error code", f"{c.code:04X}")]
        if c.register is not None:
            info.append(("Error register", c.register_text))
        if c.extra.strip(b"\0"):
            info.append(("Manufacturer data", c.extra_text + "  (meaning is the controller maker's)"))
        if c.last:
            info.append(("First / last seen", f"{_clock(c.first)} / {_clock(c.last)}  ({c.seen} message(s))"))
        if c.code >= 0xFF00 or (c.code & 0xF000) == 0xF000:
            info.append(("Note", "Device-specific code: look it up in the controller maker's list, or name it "
                                 "in a profile (\"emcy\") so it's described next time."))
        parts = [f"<h3>CANopen EMCY {c.code:04X} &mdash; {e(c.text)}</h3><table cellspacing=4>"]
        parts += [f"<tr><td><b>{e(k)}</b></td><td>{e(v)}</td></tr>" for k, v in info]
        parts.append("</table>")
        self.live_btn.setEnabled(False)
        self.live_btn.setText("No live parameter for this code")
        self.detail.setHtml("".join(parts))

    def _open_live(self):
        r = self.selected()
        if r and not isinstance(r, CanopenCode):
            self.ctx.show_signal.emit(r.spn)

    def refresh(self):
        if self.history.isChecked() and self.ctx.live:
            with self.ctx.session.lock:
                n_co = len(self.ctx.session.canopen.emcy_log)
            n = (len(self._session_rows()), n_co)
            if n != self._last_hist_n:
                self._last_hist_n = n
                self.rebuild()

