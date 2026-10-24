"""OBD-II (SAE J1979) tab: a generic scan tool for cars and light trucks.

Pages: trouble codes from every module (with Clear codes), readiness monitors, streaming live data,
freeze frame, Mode 06 on-board test results, vehicle info, and Mode 08 on-board tests. Shown for
the OBD-II vehicle profile. Everything talks through ``core.obd.ObdClient`` on worker threads; the
two commands that change the vehicle (clear codes, start an on-board test) ask first.

OBD-II needs to send requests, so nothing here works in listen-only.
"""

import threading
import time

from .. import obd
from ..core import units
from ..core.obd import CLEAR_WARNING, ObdClient
from .qt import Qt, QtCore, QtGui, QtWidgets, Signal
from .widgets import item, setup_table

BAD = QtGui.QColor("#ffc9c9")
WARN = QtGui.QColor("#ffec99")
GOOD = QtGui.QColor("#d3f9d8")


def _fmt(value, unit: str, system: str) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    decimals = 0 if unit in ("rpm", "km", "s", "min", "") and float(value).is_integer() else \
        3 if unit in ("V", "") else 1
    return units.fmt(value, unit, decimals, system=system) if unit else f"{value:.{decimals}f}"


class ObdTab(QtWidgets.QWidget):
    title = "OBD-II (Auto)"
    _done = Signal(object)          # (callback, result) from a worker thread -> run on the GUI thread
    _live_value = Signal(object)    # (ecu, ObdResponse) from the live-data thread

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.client: ObdClient | None = None
        self._jobs = 0
        self._streaming = False
        self._live_rows: dict[tuple[int, int, str], int] = {}     # (ecu, pid, value name) -> row
        self._live_minmax: dict[tuple[int, int, str], tuple[float, float]] = {}
        self._last: dict = {}
        lay = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.read_btn = QtWidgets.QPushButton("Read Vehicle (OBD-II)")
        self.read_btn.setMinimumHeight(44)
        self.read_btn.setToolTip("Read everything: codes, readiness, live data, freeze frame, test results, info")
        self.read_btn.clicked.connect(self._read)
        bar.addWidget(self.read_btn)
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: gray")
        bar.addWidget(self.status, 1)
        self.vin_label = QtWidgets.QLabel("")
        self.vin_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bar.addWidget(self.vin_label)
        lay.addLayout(bar)

        self.mil_label = QtWidgets.QLabel("")
        self.mil_label.setWordWrap(True)
        lay.addWidget(self.mil_label)

        self.pages = QtWidgets.QTabWidget()
        self.pages.addTab(self._codes_page(), "Trouble codes")
        self.pages.addTab(self._readiness_page(), "Readiness")
        self.pages.addTab(self._live_page(), "Live data")
        self.pages.addTab(self._freeze_page(), "Freeze frame")
        self.pages.addTab(self._results_page(), "Test results (Mode 06)")
        self.pages.addTab(self._info_page(), "Vehicle info")
        self.pages.addTab(self._tests_page(), "On-board tests")
        lay.addWidget(self.pages, 1)

        self.note = QtWidgets.QLabel("Pick the OBD-II simulator (or a 500k SocketCAN car bus) and connect, "
                                     "then Read. Needs to send requests, so not available in listen-only.")
        self.note.setStyleSheet("color: gray")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        self._done.connect(lambda cb_res: cb_res[0](cb_res[1]))
        self._live_value.connect(self._show_live_value)
        self._rate_timer = QtCore.QTimer(self)
        self._rate_timer.timeout.connect(self.refresh_rate)
        self._rate_timer.start(1000)
        ctx.session_changed.connect(self._session_changed)
        ctx.busy_changed.connect(lambda _: self._update_enabled())
        ctx.units_changed.connect(lambda _: self._reapply_units())
        self._update_enabled()

    # ------------------------------------------------------------------
    # pages
    # ------------------------------------------------------------------
    def _codes_page(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.codes = QtWidgets.QTableWidget()
        setup_table(self.codes, ["Module", "Type", "Code", "Description"], stretch=(3,))
        lay.addWidget(self.codes, 1)
        row = QtWidgets.QHBoxLayout()
        self.codes_btn = QtWidgets.QPushButton("Read codes")
        self.codes_btn.clicked.connect(self._read_codes)
        row.addWidget(self.codes_btn)
        row.addStretch(1)
        self.clear_btn = QtWidgets.QPushButton("Clear codes (Mode 04)…")
        self.clear_btn.setStyleSheet("color: #c92a2a; font-weight: bold;")
        self.clear_btn.setToolTip("Erase codes, freeze frame and test results on every module; turns the MIL off")
        self.clear_btn.clicked.connect(self._clear_codes)
        row.addWidget(self.clear_btn)
        lay.addLayout(row)
        # Kept for anything that wants a one-line summary of the codes.
        self.dtc_label = QtWidgets.QLabel("")
        self.dtc_label.setWordWrap(True)
        lay.addWidget(self.dtc_label)
        return w

    def _readiness_page(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.ready_btn = QtWidgets.QPushButton("Read readiness")
        self.ready_btn.clicked.connect(self._read_readiness)
        row.addWidget(self.ready_btn)
        self.this_cycle = QtWidgets.QCheckBox("This drive cycle only (PID 41)")
        row.addWidget(self.this_cycle)
        row.addStretch(1)
        lay.addLayout(row)
        self.ready_summary = QtWidgets.QLabel("")
        self.ready_summary.setStyleSheet("font-size: 13pt; font-weight: bold;")
        lay.addWidget(self.ready_summary)
        self.readiness = QtWidgets.QTableWidget()
        setup_table(self.readiness, ["Module", "Monitor", "Status"], stretch=(1,))
        lay.addWidget(self.readiness, 1)
        return w

    def _live_page(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.live_btn = QtWidgets.QPushButton("Read live data")
        self.live_btn.setToolTip("Find every supported PID and read each once")
        self.live_btn.clicked.connect(self._read_live)
        row.addWidget(self.live_btn)
        self.stream_btn = QtWidgets.QPushButton("▶ Stream")
        self.stream_btn.setToolTip("Keep polling the ticked parameters")
        self.stream_btn.clicked.connect(self._toggle_stream)
        row.addWidget(self.stream_btn)
        self.reset_minmax = QtWidgets.QPushButton("Reset min/max")
        self.reset_minmax.clicked.connect(self._reset_minmax)
        row.addWidget(self.reset_minmax)
        row.addStretch(1)
        self.rate = QtWidgets.QLabel("")
        self.rate.setStyleSheet("color: gray")
        row.addWidget(self.rate)
        lay.addLayout(row)
        self.table = QtWidgets.QTableWidget()
        setup_table(self.table, ["Watch", "Parameter", "Value", "Min", "Max", "Module"], stretch=(1,))
        lay.addWidget(self.table, 1)
        return w

    def _freeze_page(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.freeze_btn = QtWidgets.QPushButton("Read freeze frame")
        self.freeze_btn.clicked.connect(self._read_freeze)
        row.addWidget(self.freeze_btn)
        row.addWidget(QtWidgets.QLabel("Values stored when the code that caused it was set."))
        row.addStretch(1)
        lay.addLayout(row)
        self.freeze = QtWidgets.QTableWidget()
        setup_table(self.freeze, ["Module", "Parameter", "Value"], stretch=(1,))
        lay.addWidget(self.freeze, 1)
        return w

    def _results_page(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.results_btn = QtWidgets.QPushButton("Read test results")
        self.results_btn.clicked.connect(self._read_results)
        row.addWidget(self.results_btn)
        row.addWidget(QtWidgets.QLabel("Each monitor's last test value and its limits: spot a part that "
                                       "passes, but only just."))
        row.addStretch(1)
        lay.addLayout(row)
        self.results = QtWidgets.QTableWidget()
        setup_table(self.results, ["Module", "Monitor", "Test", "Value", "Min", "Max", "Result"], stretch=(1, 2))
        lay.addWidget(self.results, 1)
        return w

    def _info_page(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.info_btn = QtWidgets.QPushButton("Read vehicle info")
        self.info_btn.clicked.connect(self._read_info)
        lay.addWidget(self.info_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.info = QtWidgets.QTableWidget()
        setup_table(self.info, ["Module", "Item", "Value"], stretch=(2,))
        lay.addWidget(self.info, 1)
        return w

    def _tests_page(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        note = QtWidgets.QLabel(
            "Mode 08 asks a module to run one of its on-board tests. The standard one is the EVAP "
            "leak test (TID 01): the module seals the fuel-vapour system and checks it holds. Other "
            "test IDs are defined by the manufacturer. The vehicle's conditions (fuel level, "
            "temperature, engine state) must suit the test, or the module declines.")
        note.setWordWrap(True)
        lay.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        self.tests_btn = QtWidgets.QPushButton("Find supported tests")
        self.tests_btn.clicked.connect(self._read_tests)
        row.addWidget(self.tests_btn)
        self.test_pick = QtWidgets.QComboBox()
        self.test_pick.setMinimumWidth(280)
        row.addWidget(self.test_pick)
        self.run_test_btn = QtWidgets.QPushButton("Run test…")
        self.run_test_btn.clicked.connect(self._run_test)
        row.addWidget(self.run_test_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.test_log = QtWidgets.QListWidget()
        lay.addWidget(self.test_log, 1)
        return w

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _session_changed(self, session):
        self._stop_stream()
        if self.client is not None:
            self.client.close()
        self.client = ObdClient(session) if session is not None and not session.offline else None
        self._update_enabled()

    @property
    def can_talk(self) -> bool:
        return self.client is not None and self.ctx.live and not self.ctx.session.listen_only

    def _update_enabled(self):
        ok = self.can_talk and not self._jobs
        for b in (self.read_btn, self.codes_btn, self.clear_btn, self.ready_btn, self.live_btn, self.freeze_btn,
                  self.results_btn, self.info_btn, self.tests_btn, self.run_test_btn):
            b.setEnabled(ok)
        self.stream_btn.setEnabled(self.can_talk and (self._streaming or self.table.rowCount() > 0))
        self.run_test_btn.setEnabled(ok and self.test_pick.count() > 0)

    def _job(self, what: str, fn, done):
        """Run ``fn(client)`` on a worker thread, then ``done(result)`` on the GUI thread."""
        if not self.can_talk:
            return
        client = self.client
        self._jobs += 1
        self.status.setText(f"{what}…")
        self._update_enabled()

        def work():
            try:
                result = fn(client)
            except Exception as e:          # noqa: BLE001 - report any failure in the UI
                result = e
            self._done.emit((lambda r: self._finish(r, done), result))

        threading.Thread(target=work, name="obd", daemon=True).start()

    def _finish(self, result, done):
        self._jobs -= 1
        self.status.setText("")
        self._update_enabled()
        if isinstance(result, Exception):
            QtWidgets.QMessageBox.warning(self, "OBD-II", f"Read failed: {result}")
            return
        done(result)

    def _label(self, ecu: int) -> str:
        return self.client.ecu_label(ecu) if self.client is not None else f"{ecu:X}"

    @staticmethod
    def _fill(table: QtWidgets.QTableWidget, rows: list[list], colors: dict[int, QtGui.QColor] | None = None):
        table.setRowCount(len(rows))
        for r, cells in enumerate(rows):
            for c, text in enumerate(cells):
                it = item(str(text))
                if colors and r in colors:
                    it.setBackground(colors[r])
                table.setItem(r, c, it)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def _read(self):
        """Everything, in the order a technician wants it."""
        def everything(c: ObdClient):
            out = {"info": c.read_info_by_ecu()}        # first: it names the modules
            out["dtcs"] = c.read_dtcs_by_ecu()
            out["ready"] = c.read_readiness()
            out["live"] = self._probe_live(c)
            out["freeze"] = c.read_freeze_frame()
            out["results"] = c.read_test_results()
            return out

        def show(r):
            self._show_info(r["info"])
            self._show_codes(r["dtcs"])
            self._show_readiness(r["ready"])
            self._show_live(r["live"])
            self._show_freeze(r["freeze"])
            self._show_results(r["results"])
        self._job("Reading the vehicle", everything, show)

    def _read_codes(self):
        self._job("Reading codes", lambda c: c.read_dtcs_by_ecu(), self._show_codes)

    def _read_readiness(self):
        cycle = self.this_cycle.isChecked()
        self._job("Reading readiness", lambda c: c.read_readiness(cycle), self._show_readiness)

    def _read_live(self):
        self._job("Finding live data", self._probe_live, self._show_live)

    def _read_freeze(self):
        self._job("Reading freeze frame", lambda c: c.read_freeze_frame(), self._show_freeze)

    def _read_results(self):
        self._job("Reading test results", lambda c: c.read_test_results(), self._show_results)

    def _read_info(self):
        self._job("Reading vehicle info", lambda c: c.read_info_by_ecu(), self._show_info)

    def _read_tests(self):
        self._job("Finding on-board tests", lambda c: c.control_tests(), self._show_tests)

    @staticmethod
    def _probe_live(c: ObdClient) -> list[tuple[int, obd.ObdResponse]]:
        out = []
        for ecu, pid in c.live_pids():
            r = c.read_pid(ecu, pid)
            if r is not None and r.values:
                out.append((ecu, r))
        return out

    # ------------------------------------------------------------------
    # display
    # ------------------------------------------------------------------
    def _show_codes(self, by_ecu: dict):
        self._last["dtcs"] = by_ecu
        rows, colors, summary = [], {}, []
        for ecu, kinds in by_ecu.items():
            for kind in ("stored", "pending", "permanent"):
                for code in kinds.get(kind, []):
                    if kind == "stored":
                        colors[len(rows)] = BAD
                    rows.append([self._label(ecu), kind, code, obd.describe_dtc(code)])
                    summary.append(f"{code} ({kind})")
        if not rows:
            rows = [["", "", "", "No codes stored, pending or permanent."]]
        self._fill(self.codes, rows, colors)
        self.dtc_label.setText("Codes: " + (", ".join(summary) if summary else "none"))

    def _show_readiness(self, by_ecu: dict):
        rows, colors, mil, incomplete = [], {}, False, 0
        for ecu, s in by_ecu.items():
            mil |= s.mil
            for m in s.monitors:
                if not m.supported:
                    continue
                if not m.complete:
                    colors[len(rows)] = WARN
                    incomplete += 1
                rows.append([self._label(ecu), m.name, m.state])
        self._fill(self.readiness, rows, colors)
        codes = sum(s.dtc_count for s in by_ecu.values())
        if not by_ecu:
            self.ready_summary.setText("No module reported monitor status.")
        else:
            verdict = "READY" if not incomplete and not mil else "NOT READY"
            self.ready_summary.setText(f"{verdict}: MIL {'ON' if mil else 'off'}, {codes} confirmed code(s), "
                                       f"{incomplete} monitor(s) incomplete")
            self.ready_summary.setStyleSheet("font-size: 13pt; font-weight: bold; color: "
                                             + ("#2f9e44" if verdict == "READY" else "#c92a2a"))
        self.mil_label.setText(f"<b>MIL (check engine): {'ON' if mil else 'off'}</b>, "
                               f"{codes} confirmed code(s)" if by_ecu else "")

    def _show_live(self, readings: list):
        self._stop_stream()
        self._live_rows.clear()
        self._live_minmax.clear()
        self.table.setRowCount(0)
        for ecu, r in readings:
            self._show_live_value((ecu, r))
        self._update_enabled()

    def _show_live_value(self, payload):
        ecu, r = payload
        for name, value, unit in r.values:
            key = (ecu, r.pid, name)
            row = self._live_rows.get(key)
            if row is None:
                row = self._live_rows[key] = self.table.rowCount()
                self.table.insertRow(row)
                watch = QtWidgets.QTableWidgetItem()
                watch.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                watch.setCheckState(Qt.CheckState.Checked)
                self.table.setItem(row, 0, watch)
                self.table.setItem(row, 1, item(name, data=(ecu, r.pid, unit)))
                self.table.setItem(row, 5, item(self._label(ecu)))
            if isinstance(value, (int, float)):
                lo, hi = self._live_minmax.get(key, (value, value))
                self._live_minmax[key] = (min(lo, value), max(hi, value))
            self._paint_live(row, key, value, unit)

    def _paint_live(self, row: int, key, value, unit: str):
        system = self.ctx.units
        self.table.setItem(row, 2, item(_fmt(value, unit, system), align_right=True, data=value))
        lo_hi = self._live_minmax.get(key)
        for col, v in ((3, lo_hi[0] if lo_hi else ""), (4, lo_hi[1] if lo_hi else "")):
            self.table.setItem(row, col, item(_fmt(v, unit, system) if v != "" else "", align_right=True))

    def _reset_minmax(self):
        self._live_minmax.clear()
        for col in (3, 4):
            for row in range(self.table.rowCount()):
                self.table.setItem(row, col, item(""))

    def _show_freeze(self, by_ecu: dict):
        rows = []
        for ecu, readings in by_ecu.items():
            for r in readings:
                if r.pid == 0x02:
                    rows.append([self._label(ecu), "Caused by", f"{r.text}  {obd.describe_dtc(r.text)}"])
                    continue
                rows += [[self._label(ecu), name, _fmt(v, u, self.ctx.units)] for name, v, u in r.values]
        self._last["freeze"] = by_ecu
        self._fill(self.freeze, rows or [["", "No freeze frame stored.", ""]])

    def _show_results(self, results: list):
        rows, colors = [], {}
        for ecu, t in results:
            if not t.passed:
                colors[len(rows)] = BAD
            rows.append([self._label(ecu), t.monitor, t.test, f"{t.value:g} {t.unit}".strip(), f"{t.minimum:g}",
                         f"{t.maximum:g}", "PASS" if t.passed else "FAIL"])
        self._fill(self.results, rows or [["", "No test results reported.", "", "", "", "", ""]], colors)

    def _show_info(self, by_ecu: dict):
        rows = [[self._label(ecu), name, value] for ecu, items in by_ecu.items() for name, value in items]
        self._fill(self.info, rows or [["", "No vehicle info reported.", ""]])
        vin = next((v for items in by_ecu.values() for n, v in items if n == "VIN"), None)
        self.vin_label.setText(f"VIN: {vin}" if vin else "VIN: (not reported)")
        addr = self.client.addressing if self.client else None
        self.status.setText(f"{len(by_ecu)} module(s), {addr.name} CAN" if addr and by_ecu else "")

    def _show_tests(self, by_ecu: dict):
        self.test_pick.clear()
        for ecu, tids in by_ecu.items():
            for tid in tids:
                self.test_pick.addItem(f"{self._label(ecu)}: TID {tid:02X} "
                                       f"{obd.CONTROL_TESTS.get(tid, '(manufacturer-defined)')}", tid)
        if not by_ecu:
            self.test_log.addItem("No module supports Mode 08 tests.")
        self._update_enabled()

    def _reapply_units(self):
        for key, row in self._live_rows.items():
            value = self.table.item(row, 2).data(Qt.ItemDataRole.UserRole) if self.table.item(row, 2) else None
            unit = self.table.item(row, 1).data(Qt.ItemDataRole.UserRole)[2]
            if value is not None:
                self._paint_live(row, key, value, unit)
        if "freeze" in self._last:
            self._show_freeze(self._last["freeze"])

    # ------------------------------------------------------------------
    # live streaming
    # ------------------------------------------------------------------
    def _watched(self) -> list[tuple[int, int]]:
        out = []
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked:
                ecu, pid, _ = self.table.item(row, 1).data(Qt.ItemDataRole.UserRole)
                if (ecu, pid) not in out:
                    out.append((ecu, pid))
        return out

    def _toggle_stream(self):
        if self._streaming:
            self._stop_stream()
        else:
            self._start_stream()

    def _start_stream(self):
        if not self.can_talk or self._streaming:
            return
        self._streaming = True
        self.stream_btn.setText("■ Stop")
        client = self.client

        def loop():
            n, t0 = 0, time.time()
            while self._streaming:
                watched = self._watched_snapshot
                if not watched:
                    time.sleep(0.2)
                    continue
                for ecu, pid in watched:
                    if not self._streaming:
                        break
                    r = client.read_pid(ecu, pid)
                    if r is not None:
                        self._live_value.emit((ecu, r))
                        n += 1
                if time.time() - t0 >= 1.0:
                    self._rate = n / (time.time() - t0)
                    n, t0 = 0, time.time()

        self._watched_snapshot = self._watched()
        self.table.itemChanged.connect(self._watch_changed)
        self._rate = 0.0
        self._stream_thread = threading.Thread(target=loop, name="obd-live", daemon=True)
        self._stream_thread.start()
        self._update_enabled()

    def _watch_changed(self, it):
        if it.column() == 0:
            self._watched_snapshot = self._watched()

    def _stop_stream(self):
        if not self._streaming:
            return
        self._streaming = False
        try:
            self.table.itemChanged.disconnect(self._watch_changed)
        except (RuntimeError, TypeError):
            pass
        t = getattr(self, "_stream_thread", None)
        if t is not None:
            t.join(timeout=2.0)
        self.stream_btn.setText("▶ Stream")
        self.rate.setText("")
        self._update_enabled()

    def refresh_rate(self):
        if self._streaming:
            self.rate.setText(f"{getattr(self, '_rate', 0):.0f} readings/s")

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def _confirm(self, title: str, text: str) -> bool:
        r = QtWidgets.QMessageBox.warning(self, title, text,
                                          QtWidgets.QMessageBox.StandardButton.Yes
                                          | QtWidgets.QMessageBox.StandardButton.No,
                                          QtWidgets.QMessageBox.StandardButton.No)
        return r == QtWidgets.QMessageBox.StandardButton.Yes

    def _clear_codes(self):
        if not self.can_talk or not self._confirm("Clear trouble codes", CLEAR_WARNING):
            return

        def clear(c: ObdClient):
            result = c.clear_dtcs()
            time.sleep(0.5)                 # let the modules settle before reading back
            return result, c.read_dtcs_by_ecu(), c.read_readiness()

        def show(r):
            result, dtcs, ready = r
            self._show_codes(dtcs)
            self._show_readiness(ready)
            lines = [f"{self._label(ecu)}: {msg}" for ecu, (_, msg) in result.items()]
            text = "\n".join(lines) or "No module answered the clear request."
            self.ctx.status_message.emit("Clear codes (Mode 04): " + "; ".join(lines or ["no answer"]))
            QtWidgets.QMessageBox.information(self, "Clear trouble codes", text)
        self._job("Clearing codes", clear, show)

    def _run_test(self):
        tid = self.test_pick.currentData()
        if tid is None:
            return
        name = obd.CONTROL_TESTS.get(tid, f"manufacturer test TID {tid:02X}")
        if not self.can_talk or not self._confirm(
                "Run on-board test", f"Ask the vehicle to run the {name} (OBD-II Mode 08, TID {tid:02X})?\n\n"
                "The module takes control of the components involved until the test ends."):
            return

        def show(result: dict):
            stamp = time.strftime("%H:%M:%S")
            if not result:
                self.test_log.insertItem(0, f"{stamp}  TID {tid:02X}: no answer")
            for ecu, (_, msg) in result.items():
                self.test_log.insertItem(0, f"{stamp}  {self._label(ecu)}: TID {tid:02X} {name}: {msg}")
        self._job("Starting test", lambda c: c.run_control_test(tid), show)
