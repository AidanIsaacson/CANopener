"""OBD-II (SAE J1979) tab: a generic scan tool for cars and light trucks.

Pages: Trouble Codes (check / clear, jump to a code's live values or freeze frame), Live Data
(the same screen as J1939's: plot, chart, hide plots, hide unticked, Log markers, over
``core.live.ObdLiveData``), Freeze Frame, Readiness, On-Board Tests (Mode 08), Test Results
(Mode 06), Vehicle Info; the main window adds Bus Monitor at the end. Shown for the OBD-II vehicle
profile.

Live values are decoded passively from the frames on the bus (``obd.ObdSniffer``): polling just
puts requests out, and the same decode serves a recorded log. Everything else talks through
``core.obd.ObdClient`` on worker threads. The commands that change the vehicle (clear codes, start
an on-board test) ask first. While the Log tool records, the tab keeps the bus busy: it reads the
codes once, then streams the ticked PIDs, so the capture has OBD-II data the way a J1939 bus has
its broadcasts. Nothing transmits in listen-only.
"""

import threading
import time

from .. import obd
from ..core import units
from ..core.live import ObdLiveData
from ..core.obd import CLEAR_WARNING, ObdClient
from .qt import Qt, QtCore, QtGui, QtWidgets, Signal
from .tab_live import LiveTab
from .widgets import item, setup_table

BAD = QtGui.QColor("#ffc9c9")
WARN = QtGui.QColor("#ffec99")
CAUSE = QtGui.QColor("#d0ebff")
DARK_TEXT = QtGui.QColor("#212529")
P_CODES, P_LIVE, P_FREEZE, P_READY, P_TESTS, P_RESULTS, P_INFO = range(7)
POLL_GAP = 0.01         # between live-data requests: ~100/s at most, polite to a busy gateway


def _fmt(value, unit: str, system: str) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    decimals = 0 if unit in ("rpm", "km", "s", "min") and float(value).is_integer() else \
        3 if unit in ("V", "") else 1
    return units.fmt(value, unit, decimals, system=system) if unit else f"{value:.{decimals}f}"


class ObdTab(QtWidgets.QWidget):
    title = "OBD-II (Auto)"
    _done = Signal(object)          # (callback, result) from a worker thread -> run on the GUI thread

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.client: ObdClient | None = None
        self._jobs = 0
        self._streaming = False
        self._auto_stream = False       # started by the Log tool's recording
        self._stream_pending = False    # Stream pressed before any PIDs were known: start after the read
        self._poll: list[tuple[int, int]] = []
        self._rate = 0.0
        self._freeze_by_ecu: dict = {}
        self._codes: list[tuple[int, str, str]] = []     # (ecu, kind, code) per table row
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.pages = QtWidgets.QTabWidget()
        self.pages.addTab(self._codes_page(), "Trouble Codes")
        self.live = LiveTab(ctx, self, model=ObdLiveData(), settings_prefix="obd_live",
                            empty_text="No OBD-II values yet: press Read live data (or Stream).",
                            filter_hint="Filter values (name, PID, module)...")
        self._add_live_controls()
        self.pages.addTab(self.live, "Live Data")
        self.pages.addTab(self._freeze_page(), "Freeze Frame")
        self.pages.addTab(self._readiness_page(), "Readiness")
        self.pages.addTab(self._tests_page(), "On-Board Tests")
        self.pages.addTab(self._results_page(), "Test Results")
        self.pages.addTab(self._info_page(), "Vehicle Info")
        lay.addWidget(self.pages, 1)

        self._done.connect(lambda cb_res: cb_res[0](cb_res[1]))
        ctx.session_changed.connect(self._session_changed)
        ctx.units_changed.connect(lambda _: self._units_changed())
        self._update_enabled()

    @property
    def model(self) -> ObdLiveData:
        return self.live.model

    # ------------------------------------------------------------------
    # pages
    # ------------------------------------------------------------------
    def _codes_page(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.check_btn = QtWidgets.QPushButton("Check codes")
        self.check_btn.setMinimumHeight(36)
        self.check_btn.setToolTip("Read codes from every module, with the MIL, readiness and freeze frame")
        self.check_btn.clicked.connect(self.check_codes)
        row.addWidget(self.check_btn)
        self.clear_btn = QtWidgets.QPushButton("Clear codes (Mode 04)…")
        self.clear_btn.setMinimumHeight(36)
        self.clear_btn.setStyleSheet("color: #c92a2a; font-weight: bold;")
        self.clear_btn.setToolTip("Erase codes, freeze frame and test results on every module; turns the MIL off")
        self.clear_btn.clicked.connect(self._clear_codes)
        row.addWidget(self.clear_btn)
        row.addStretch(1)
        self.mil_label = QtWidgets.QLabel("")
        self.mil_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self.mil_label)
        lay.addLayout(row)
        self.codes = QtWidgets.QTableWidget()
        setup_table(self.codes, ["Module", "Type", "Code", "Description"], stretch=(3,))
        self.codes.itemSelectionChanged.connect(self._code_selected)
        lay.addWidget(self.codes, 1)
        row = QtWidgets.QHBoxLayout()
        self.code_detail = QtWidgets.QLabel("")
        self.code_detail.setWordWrap(True)
        row.addWidget(self.code_detail, 1)
        self.show_live_btn = QtWidgets.QPushButton("Show in Live Data")
        self.show_live_btn.setToolTip("Plot the sensor values this code is about")
        self.show_live_btn.clicked.connect(self._show_code_live)
        row.addWidget(self.show_live_btn)
        self.show_freeze_btn = QtWidgets.QPushButton("Show freeze frame")
        self.show_freeze_btn.setToolTip("The values the module stored when this code was set")
        self.show_freeze_btn.clicked.connect(self._show_code_freeze)
        row.addWidget(self.show_freeze_btn)
        lay.addLayout(row)
        return w

    def _add_live_controls(self):
        self.live_btn = QtWidgets.QPushButton("Read live data")
        self.live_btn.setToolTip("Find every value the vehicle supports and read each once")
        self.live_btn.clicked.connect(self.read_live)
        self.stream_btn = QtWidgets.QPushButton("▶ Stream")
        self.stream_btn.setToolTip("Keep polling: the ticked values when 'Hide unticked' is on, else all")
        self.stream_btn.clicked.connect(self.toggle_stream)
        self.rate = QtWidgets.QLabel("")
        self.rate.setStyleSheet("color: gray")
        for i, w in enumerate((self.live_btn, self.stream_btn)):
            self.live.controls.insertWidget(i, w)
        self.live.controls.addWidget(self.rate)

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

    def _results_page(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.results_btn = QtWidgets.QPushButton("Read test results")
        self.results_btn.clicked.connect(self._read_results)
        row.addWidget(self.results_btn)
        row.addWidget(QtWidgets.QLabel("Mode 06: each monitor's last test value and its limits. Spots a part "
                                       "that passes, but only just."))
        row.addStretch(1)
        lay.addLayout(row)
        self.results = QtWidgets.QTableWidget()
        setup_table(self.results, ["Module", "Monitor", "Test", "Value", "Min", "Max", "Result"], stretch=(1, 2))
        lay.addWidget(self.results, 1)
        return w

    def _info_page(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.info_btn = QtWidgets.QPushButton("Read vehicle info")
        self.info_btn.clicked.connect(self._read_info)
        row.addWidget(self.info_btn)
        row.addStretch(1)
        self.vin_label = QtWidgets.QLabel("")
        self.vin_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.vin_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self.vin_label)
        lay.addLayout(row)
        self.info = QtWidgets.QTableWidget()
        setup_table(self.info, ["Module", "Item", "Value"], stretch=(2,))
        lay.addWidget(self.info, 1)
        return w

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _session_changed(self, session):
        self.stop_stream()
        if self.client is not None:
            self.client.close()
        self.client = ObdClient(session) if session is not None and not session.offline else None
        for t in (self.codes, self.freeze, self.readiness, self.results, self.info):
            t.setRowCount(0)
        self.mil_label.setText("")
        self.ready_summary.setText("")
        self._freeze_by_ecu = {}
        if session is not None and session.offline:
            self._show_log()         # the Live Data page has decoded the log by now
        self._update_enabled()

    @property
    def polling(self) -> bool:
        """Asking the car something right now (streaming, or a read / command in progress)."""
        return self._streaming or self._jobs > 0

    @property
    def can_talk(self) -> bool:
        return self.client is not None and self.ctx.live and not self.ctx.session.listen_only

    def _update_enabled(self):
        ok = self.can_talk and not self._jobs
        for b in (self.check_btn, self.clear_btn, self.live_btn, self.freeze_btn, self.ready_btn,
                  self.results_btn, self.info_btn, self.tests_btn):
            b.setEnabled(ok)
        self.stream_btn.setEnabled(self.can_talk)
        self.run_test_btn.setEnabled(ok and self.test_pick.count() > 0)
        self._code_selected()

    def _job(self, what: str, fn, done):
        """Run ``fn(client)`` on a worker thread, then ``done(result)`` on the GUI thread."""
        if not self.can_talk:
            return
        client = self.client
        self._jobs += 1
        if self._jobs == 1:
            self.ctx.busy_changed.emit(True)
        self.ctx.status_message.emit(f"OBD-II: {what}…")
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
        if not self._jobs:
            self.ctx.busy_changed.emit(False)
        self._update_enabled()
        if isinstance(result, Exception):
            self.ctx.status_message.emit(f"OBD-II: failed: {result}")
            QtWidgets.QMessageBox.warning(self, "OBD-II", f"Read failed: {result}")
            return
        self.ctx.status_message.emit("")
        done(result)

    def _label(self, ecu: int) -> str:
        if self.client is not None and self.client.ecu_names.get(ecu):
            return self.client.ecu_label(ecu)
        return self.model.sniffer.label(ecu)

    @staticmethod
    def _fill(table: QtWidgets.QTableWidget, rows: list[list], colors: dict[int, QtGui.QColor] | None = None):
        table.setRowCount(len(rows))
        for r, cells in enumerate(rows):
            for c, text in enumerate(cells):
                it = item(str(text))
                if colors and r in colors:
                    it.setBackground(colors[r])
                    it.setForeground(DARK_TEXT)     # light highlight: keep the text readable in a dark theme
                table.setItem(r, c, it)

    def tick(self):
        """Every UI tick (main window), while the OBD-II profile is active: decode new frames."""
        if self.ctx.session is not None:
            self.live.sample()
            self.live.refresh()
        if self._streaming:
            self._poll = self._poll_list()          # picked up by the polling thread
            self.rate.setText(f"{self._rate:.0f} readings/s")

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def check_codes(self):
        def check(c: ObdClient):
            if not c.ecu_names:
                c.read_ecu_names()
            return c.read_dtcs_by_ecu(), c.read_readiness(), c.read_freeze_frame()

        def show(r):
            dtcs, ready, freeze = r
            self._show_codes(dtcs)
            self._show_readiness(ready)
            self._show_freeze(freeze)
        self._job("checking codes", check, show)

    def _read_readiness(self):
        cycle = self.this_cycle.isChecked()
        self._job("reading readiness", lambda c: c.read_readiness(cycle), self._show_readiness)

    def read_live(self, then=None):
        """Find the supported PIDs and read each once: the rows appear in Live Data."""
        def probe(c: ObdClient):
            for ecu, pid in c.live_pids():
                c.read_pid(ecu, pid)

        def done(_):
            self.tick()
            if then is not None:
                then()
        self._job("reading live data", probe, done)

    def _read_freeze(self):
        self._job("reading freeze frame", lambda c: c.read_freeze_frame(), self._show_freeze)

    def _read_results(self):
        self._job("reading test results", lambda c: c.read_test_results(), self._show_results)

    def _read_info(self):
        self._job("reading vehicle info", lambda c: c.read_info_by_ecu(), self._show_info)

    def _read_tests(self):
        self._job("finding on-board tests", lambda c: c.control_tests(), self._show_tests)

    # ------------------------------------------------------------------
    # display
    # ------------------------------------------------------------------
    def _show_codes(self, by_ecu: dict):
        rows, colors, self._codes = [], {}, []
        for ecu, kinds in by_ecu.items():
            for kind in ("stored", "pending", "permanent"):
                for code in kinds.get(kind, []):
                    if kind == "stored":
                        colors[len(rows)] = BAD
                    rows.append([self._label(ecu), kind, code, obd.describe_dtc(code)])
                    self._codes.append((ecu, kind, code))
        if not rows:
            rows = [["", "", "", "No codes stored, pending or permanent."]]
        self._fill(self.codes, rows, colors)
        if self._codes:
            self.select_code(0)
        self._code_selected()

    def select_code(self, row: int):
        """Select a code row (through the selection model: unlike selectRow, never a toggle)."""
        sm = self.codes.selectionModel()
        sm.select(self.codes.model().index(row, 0), QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
                  | QtCore.QItemSelectionModel.SelectionFlag.Rows)

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
            self.mil_label.setText("")
            return
        verdict = "READY" if not incomplete and not mil else "NOT READY"
        self.ready_summary.setText(f"{verdict}: MIL {'ON' if mil else 'off'}, {codes} confirmed code(s), "
                                   f"{incomplete} monitor(s) incomplete")
        self.ready_summary.setStyleSheet("font-size: 13pt; font-weight: bold; color: "
                                         + ("#2f9e44" if verdict == "READY" else "#c92a2a"))
        self.mil_label.setText(f"Check engine light (MIL): {'ON' if mil else 'off'}")
        self.mil_label.setStyleSheet("font-weight: bold; color: " + ("#c92a2a" if mil else "#2f9e44"))

    def _show_freeze(self, by_ecu: dict):
        self._freeze_by_ecu = by_ecu
        rows, colors = [], {}
        for ecu, readings in by_ecu.items():
            for r in readings:
                if r.pid == 0x02:
                    colors[len(rows)] = CAUSE
                    rows.append([self._label(ecu), "Caused by", f"{r.text}  {obd.describe_dtc(r.text)}"])
                    continue
                rows += [[self._label(ecu), name, _fmt(v, u, self.ctx.units)] for name, v, u in r.values]
        self._fill(self.freeze, rows or [["", "No freeze frame stored.", ""]], colors)
        self._code_selected()

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
        self.vin_label.setText(f"VIN: {vin}" if vin else "")

    def _show_tests(self, by_ecu: dict):
        self.test_pick.clear()
        for ecu, tids in by_ecu.items():
            for tid in tids:
                self.test_pick.addItem(f"{self._label(ecu)}: TID {tid:02X} "
                                       f"{obd.CONTROL_TESTS.get(tid, '(manufacturer-defined)')}", tid)
        if not by_ecu:
            self.test_log.addItem("No module supports Mode 08 tests.")
        self._update_enabled()

    def _show_log(self):
        """A decoded log: show what the OBD-II traffic in it said."""
        sn = self.model.sniffer
        self._show_codes(sn.dtcs)
        if sn.readiness:
            self._show_readiness(sn.readiness)
        self._show_freeze({ecu: sn.freeze_rows(ecu) for ecu in sn.freeze})
        self._show_results([(k[0], r) for k, r in sn.results.items()])
        self._show_info({ecu: list(items.items()) for ecu, items in sn.info.items()})

    def _units_changed(self):
        if self._freeze_by_ecu:
            self._show_freeze(self._freeze_by_ecu)

    # ------------------------------------------------------------------
    # code -> live data / freeze frame
    # ------------------------------------------------------------------
    def _selected_code(self) -> tuple[int, str, str] | None:
        rows = self.codes.selectionModel().selectedRows() if self.codes.selectionModel() else []
        i = rows[0].row() if rows else -1
        return self._codes[i] if 0 <= i < len(self._codes) else None

    def _code_selected(self):
        sel = self._selected_code()
        pids = obd.pids_for_dtc(sel[2]) if sel else []
        cause = self._freeze_cause(sel[0]) if sel else None
        self.show_live_btn.setEnabled(bool(pids) and (self.can_talk or bool(self.model.find_pids(pids))))
        self.show_freeze_btn.setEnabled(sel is not None and cause == sel[2])
        if sel is None:
            self.code_detail.setText("")
            return
        ecu, kind, code = sel
        linked = ", ".join(obd.pid_name(p) for p in pids) or "none (no standard sensor value for this code)"
        snap = "Freeze frame: stored by this code." if cause == code else \
            f"Freeze frame: from {cause}." if cause else "No freeze frame read for this module."
        self.code_detail.setText(f"<b>{code}</b> ({kind}, {self._label(ecu)}): {obd.describe_dtc(code)}<br>"
                                 f"Live values: {linked}. {snap}")

    def _freeze_cause(self, ecu: int) -> str | None:
        rows = self._freeze_by_ecu.get(ecu) or []
        return next((r.text for r in rows if r.pid == 0x02 and r.text), None)

    def _show_code_live(self):
        sel = self._selected_code()
        if sel is None:
            return
        pids = obd.pids_for_dtc(sel[2])

        def focus():
            self.tick()
            keys = [k for k in self.model.find_pids(pids) if self.model.rows[k].sa == sel[0]] or \
                self.model.find_pids(pids)
            self.pages.setCurrentIndex(P_LIVE)
            if self.live.focus_keys(keys):
                if self.can_talk and not self._streaming:
                    self.start_stream()
            else:
                self.ctx.status_message.emit(f"{sel[2]}: the vehicle doesn't report "
                                             + ", ".join(obd.pid_name(p) for p in pids))
        if self.model.find_pids(pids) or not self.can_talk:
            focus()
        else:
            self.read_live(then=focus)         # no live data read yet: find it first

    def _show_code_freeze(self):
        sel = self._selected_code()
        if sel is None:
            return
        self.pages.setCurrentIndex(P_FREEZE)
        label = self._label(sel[0])
        for r in range(self.freeze.rowCount()):
            if self.freeze.item(r, 0).text() == label:
                self.freeze.selectRow(r)
                self.freeze.scrollToItem(self.freeze.item(r, 0))
                break

    # ------------------------------------------------------------------
    # live streaming (the answers are decoded passively by the Live Data model)
    # ------------------------------------------------------------------
    def _poll_list(self) -> list[tuple[int, int]]:
        """What to poll: the ticked values when 'Hide unticked' is on (or the Log tool is
        recording with something ticked), else every value listed."""
        polled = self.model.polled()
        picked = [(self.model.rows[k].sa, self.model.rows[k].sig.pid) for k in self.model.selected
                  if k in self.model.rows]
        if picked and (self.live.only_checked.isChecked() or self._auto_stream):
            return list(dict.fromkeys(picked))
        return polled

    def toggle_stream(self):
        if self._streaming or self._stream_pending:
            self.stop_stream()
        else:
            self.start_stream()

    def start_stream(self):
        if not self.can_talk or self._streaming:
            return
        if not self.model.order:
            # Nothing to poll yet: find the PIDs first. Stop pressed meanwhile cancels the start.
            self._stream_pending = True
            self.stream_btn.setText("■ Stop")
            self.read_live(then=self._start_pending)
            return
        self._stream_pending = False
        self._streaming = True
        self._poll = self._poll_list()
        self.stream_btn.setText("■ Stop")
        client = self.client

        def loop():
            n, t0 = 0, time.time()
            while self._streaming:
                todo = list(self._poll)
                if not todo:
                    time.sleep(0.2)
                for ecu, pid in todo:
                    if not self._streaming:
                        break
                    if client.read_pid(ecu, pid) is not None:
                        n += 1
                    time.sleep(POLL_GAP)
                if time.time() - t0 >= 1.0:
                    self._rate, n, t0 = n / (time.time() - t0), 0, time.time()

        self._stream_thread = threading.Thread(target=loop, name="obd-live", daemon=True)
        self._stream_thread.start()
        self._update_enabled()

    def _start_pending(self):
        if self._stream_pending and self.model.order:
            self.start_stream()
        else:
            self._stream_pending = False
            self.stream_btn.setText("▶ Stream")

    def stop_stream(self):
        self._auto_stream = False
        if self._stream_pending:
            self._stream_pending = False
            self.stream_btn.setText("▶ Stream")
        if not self._streaming:
            return
        self._streaming = False
        t = getattr(self, "_stream_thread", None)
        if t is not None:
            t.join(timeout=2.0)
        self.stream_btn.setText("▶ Stream")
        self.rate.setText("")
        self._update_enabled()

    # ------------------------------------------------------------------
    # the Log tool
    # ------------------------------------------------------------------
    def recording_changed(self, on: bool):
        """The Log tool started / stopped a capture. A car only talks when asked, so while
        recording keep it talking: codes once, then the live values, streaming."""
        if not on:
            if self._auto_stream:
                self.stop_stream()
            return
        if not self.can_talk:
            return
        self.check_codes()
        if not self._streaming:
            self._auto_stream = True
            self.start_stream()
            self._auto_stream = self._streaming or not self.model.order    # (may start after the read)

    def log_status(self) -> str:
        """A line for the Log tool while recording."""
        if self._streaming:
            return f"OBD-II: polling {len(self._poll)} value(s), {self._rate:.0f}/s"
        return "OBD-II: not polling (press Stream in Live Data)" if self.can_talk else ""

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
            return result, c.read_dtcs_by_ecu(), c.read_readiness(), c.read_freeze_frame()

        def show(r):
            result, dtcs, ready, freeze = r
            self._show_codes(dtcs)
            self._show_readiness(ready)
            self._show_freeze(freeze)
            lines = [f"{self._label(ecu)}: {msg}" for ecu, (_, msg) in result.items()]
            text = "\n".join(lines) or "No module answered the clear request."
            self.ctx.status_message.emit("Clear codes (Mode 04): " + "; ".join(lines or ["no answer"]))
            QtWidgets.QMessageBox.information(self, "Clear trouble codes", text)
        self._job("clearing codes", clear, show)

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
        self._job("starting test", lambda c: c.run_control_test(tid), show)
