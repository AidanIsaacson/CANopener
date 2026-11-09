"""Connection > Pair / Manage Adapters: find, pair, name, test and forget Bluetooth OBD adapters.

Only the screen: the work is ``core.bluetooth`` (bluetoothctl) and ``can.elm327`` (Test), run on
a worker thread so a 10-second scan or a pairing doesn't freeze the window. Names are kept in
the shared settings, so the handheld shows the same ones.
"""

import threading
import time

from ..can import elm327
from ..core import bluetooth as bt
from ..core.settings import Settings
from .qt import Qt, QtCore, QtGui, QtWidgets, Signal

KIND_SHORT = {"serial": "Classic, serial port ✔", "classic": "Classic", "ble": "Low Energy only ✖"}


class _Done(QtCore.QObject):
    finished = Signal(object, object)       # (callback, result or exception)


class BluetoothDialog(QtWidgets.QDialog):
    def __init__(self, settings: Settings | None = None, busy_address: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bluetooth OBD adapters")
        self.settings = settings or Settings()
        self.busy_address = (busy_address or "").upper()   # connected right now: can't be tested
        self._done = _Done(self)
        self._done.finished.connect(self._finish)
        self._working = False
        self.paired: list[bt.Device] = []
        self.found: list[bt.Device] = []
        self._build()
        self.resize(640, 520)
        QtCore.QTimer.singleShot(0, self.refresh)

    # ------------------------------------------------------------------
    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)
        self.state = QtWidgets.QLabel()
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        self.power_btn = QtWidgets.QPushButton("Turn on Bluetooth")
        self.power_btn.clicked.connect(self._power_on)
        self.power_btn.hide()
        lay.addWidget(self.power_btn, 0, Qt.AlignmentFlag.AlignLeft)

        mine = QtWidgets.QGroupBox("Your adapters")
        ml = QtWidgets.QVBoxLayout(mine)
        self.mine = self._tree(["Name", "Address", "Picked"])
        self.mine.itemSelectionChanged.connect(self._update_buttons)
        self.mine.itemDoubleClicked.connect(lambda *_: self._use())
        ml.addWidget(self.mine)
        row = QtWidgets.QHBoxLayout()
        self.use_btn = self._button(row, "Use", self._use, "Connect to this adapter (Source: Bluetooth OBD)")
        self.rename_btn = self._button(row, "Rename…", self._rename, "Give it a name, e.g. 'vLinker (truck)'")
        self.test_btn = self._button(row, "Test", self._test, "Connect, ask the adapter what it is and the "
                                     "battery voltage, then disconnect")
        self.forget_btn = self._button(row, "Forget…", self._forget, "Unpair it from this computer")
        row.addStretch(1)
        ml.addLayout(row)
        lay.addWidget(mine, 1)

        near = QtWidgets.QGroupBox("Pair a new adapter")
        nl = QtWidgets.QVBoxLayout(near)
        hint = QtWidgets.QLabel("Plug the adapter into the vehicle's OBD port (some only wake with the key on), "
                                "then Scan. An adapter with both kinds of Bluetooth can be listed twice: pair "
                                "the Classic one (marked “serial port”).")
        hint.setWordWrap(True)
        nl.addWidget(hint)
        self.near = self._tree(["Name", "Address", "Kind"])
        self.near.itemSelectionChanged.connect(self._update_buttons)
        self.near.itemDoubleClicked.connect(lambda *_: self._pair())
        nl.addWidget(self.near)
        row = QtWidgets.QHBoxLayout()
        self.scan_btn = self._button(row, "Scan", self._scan, f"Look for nearby devices for {bt.SCAN_SECONDS} s")
        self.pair_btn = self._button(row, "Pair…", self._pair, "Pair with the selected device and use it")
        row.addStretch(1)
        nl.addLayout(row)
        lay.addWidget(near, 1)

        self.progress = QtWidgets.QLabel()
        lay.addWidget(self.progress)
        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def _dim(self):
        return self.palette().brush(QtGui.QPalette.ColorGroup.Disabled, QtGui.QPalette.ColorRole.Text)

    @staticmethod
    def _tree(cols):
        t = QtWidgets.QTreeWidget()
        t.setHeaderLabels(cols)
        t.setRootIsDecorated(False)
        t.setAllColumnsShowFocus(True)
        t.header().setStretchLastSection(True)
        t.setColumnWidth(0, 220)
        t.setColumnWidth(1, 150)
        return t

    @staticmethod
    def _button(row, text, fn, tip):
        b = QtWidgets.QPushButton(text)
        b.setToolTip(tip)
        b.clicked.connect(fn)
        row.addWidget(b)
        return b

    # ------------------------------------------------------------------
    # background work
    # ------------------------------------------------------------------
    def _run(self, label: str, fn, then):
        if self._working:
            return
        self._working = True
        self.progress.setText(label + "…")
        self._update_buttons()

        def work():
            try:
                result = fn()
            except Exception as e:          # shown to the user, never raised into Qt
                result = e
            self._done.finished.emit(then, result)
        threading.Thread(target=work, name="bluetooth", daemon=True).start()

    def _finish(self, then, result):
        self._working = False
        self.progress.setText("")
        if isinstance(result, Exception):
            QtWidgets.QMessageBox.warning(self, "Bluetooth", str(result))
        else:
            then(result)
        self._update_buttons()

    def wait_idle(self, timeout: float = 30.0):
        """Tests: let a background job finish."""
        end = time.time() + timeout
        QtWidgets.QApplication.processEvents()      # a job queued with a timer starts first
        while self._working and time.time() < end:
            QtWidgets.QApplication.processEvents()
            QtCore.QThread.msleep(20)

    # ------------------------------------------------------------------
    def refresh(self):
        def load():
            state = bt.controller()
            return state, (bt.paired_devices() if state.powered else [])
        self._run("Checking Bluetooth", load, self._show)

    def _show(self, result):
        state, paired = result
        problem = state.problem()
        self.state.setText(problem or f"Bluetooth is on ({state.name or 'this computer'}).")
        self.power_btn.setVisible(state.present and not state.powered and not state.blocked)
        self.paired = paired
        self._fill_mine()

    def _fill_mine(self):
        picked = (self.settings.get("elm.adapter") or "").upper()
        self.mine.clear()
        by_addr = {d.address: d for d in self.paired}
        for addr, label in bt.adapter_choices(self.settings, self.paired):
            dev = by_addr.get(addr)
            it = QtWidgets.QTreeWidgetItem([label, addr, "✔" if addr == picked else ""])
            it.setData(0, Qt.ItemDataRole.UserRole, addr)
            if dev is None:
                it.setToolTip(0, "Not paired with this computer any more: Forget it, or pair it again")
            self.mine.addTopLevelItem(it)
        others = [d for d in self.paired if d.address not in {a for a, _ in bt.adapter_choices(self.settings,
                                                                                               self.paired)}]
        for d in others:                    # other paired devices (headphones ...): usable if it's an adapter
            it = QtWidgets.QTreeWidgetItem([d.name or "(no name)", d.address, ""])
            it.setData(0, Qt.ItemDataRole.UserRole, d.address)
            it.setToolTip(0, "Paired, but doesn't look like an OBD adapter. Use it if it is one.")
            it.setForeground(0, self._dim())
            self.mine.addTopLevelItem(it)
        self._update_buttons()

    def _selected(self, tree) -> str:
        it = tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it is not None and it.isSelected() else ""

    def _update_buttons(self):
        mine, near = self._selected(self.mine), self._selected(self.near)
        idle = not self._working
        for b in (self.use_btn, self.rename_btn, self.forget_btn):
            b.setEnabled(idle and bool(mine))
        self.test_btn.setEnabled(idle and bool(mine) and mine != self.busy_address)
        self.test_btn.setToolTip("Disconnect first: it's connected now" if mine and mine == self.busy_address
                                 else "Connect, ask the adapter what it is and the battery voltage, then disconnect")
        dev = next((d for d in self.found if d.address == near), None)
        self.pair_btn.setEnabled(idle and dev is not None and dev.usable)
        self.scan_btn.setEnabled(idle)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _power_on(self):
        self._run("Turning Bluetooth on", bt.power_on, lambda ok: self.refresh())

    def _use(self):
        addr = self._selected(self.mine)
        if not addr:
            return
        if addr not in bt.names(self.settings):
            dev = next((d for d in self.paired if d.address == addr), None)
            bt.set_name(self.settings, addr, dev.name if dev and dev.name else addr)
        self.settings.set("elm.adapter", addr)
        self.accept()

    def _rename(self):
        addr = self._selected(self.mine)
        if not addr:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename adapter", f"Name for {addr}:",
                                                  text=bt.label(self.settings, addr))
        if ok:
            bt.set_name(self.settings, addr, name)
            self._fill_mine()

    def _forget(self):
        addr = self._selected(self.mine)
        if not addr:
            return
        r = QtWidgets.QMessageBox.question(
            self, "Forget adapter", f"Unpair {bt.label(self.settings, addr)} ({addr}) from this computer?\n\n"
            "You can pair it again later.", QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No, QtWidgets.QMessageBox.StandardButton.No)
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        def gone(_):
            bt.forget_name(self.settings, addr)
            self.refresh()
        self._run("Forgetting", lambda: bt.remove(addr), gone)

    def _test(self):
        addr = self._selected(self.mine)
        if not addr:
            return

        def test():
            b = elm327.ElmBackend(addr, protocol=self.settings.get("elm.protocol") or "0")
            b.open()
            try:
                return b.stn or b.version, b.device, b.voltage, b.protocol_name
            finally:
                b.close()

        def show(r):
            chip, device, volts, proto = r
            QtWidgets.QMessageBox.information(
                self, "Adapter test", f"{bt.label(self.settings, addr)} answered.\n\n"
                f"Adapter: {chip}" + (f" ({device})" if device else "") + f"\nBattery: {volts or 'not reported'}\n"
                f"Vehicle: {proto or 'no answer (key on? plugged into the OBD port?)'}")
        self._run(f"Testing {bt.label(self.settings, addr)}", test, show)

    def _scan(self):
        self._run(f"Scanning for {bt.SCAN_SECONDS} s", bt.scan, self._show_found)

    def _show_found(self, found):
        paired = {d.address for d in self.paired}
        self.found = [d for d in found if d.address not in paired]
        self.near.clear()
        for d in self.found:
            kind = KIND_SHORT.get(d.kind, "")
            it = QtWidgets.QTreeWidgetItem([d.name or "(no name)", d.address, kind])
            it.setData(0, Qt.ItemDataRole.UserRole, d.address)
            for col in range(3):
                it.setToolTip(col, d.kind_text or "Not sure which kind of Bluetooth: try pairing it")
            if not d.usable:
                it.setForeground(0, self._dim())
            self.near.addTopLevelItem(it)
        first = next((i for i, d in enumerate(self.found) if d.looks_like_obd and d.usable), None)
        if first is not None:                       # pre-select the likely adapter (never a BLE-only entry)
            self.near.setCurrentItem(self.near.topLevelItem(first))
        self.progress.setText(f"Found {len(self.found)} device(s)" if self.found else
                              "Nothing found. Is the adapter plugged in (and the key on)?")

    def _pair(self):
        addr = self._selected(self.near)
        dev = next((d for d in self.found if d.address == addr), None)
        if dev is None or not dev.usable:
            return
        pin, ok = QtWidgets.QInputDialog.getText(
            self, "Pair adapter", f"PIN for {dev.name or addr}, if it asks for one\n"
            "(most adapters use 1234, some 0000):", text=bt.DEFAULT_PIN)
        if not ok:
            return
        name = dev.name or addr

        def paired(msg):
            bt.set_name(self.settings, addr, bt.names(self.settings).get(addr) or name)
            self.settings.set("elm.adapter", addr)
            self.progress.setText(f"{msg}: {name} is now your Bluetooth OBD adapter")
            self.found = [d for d in self.found if d.address != addr]
            self._show_found(self.found)
            self.refresh()
        self._run(f"Pairing with {name}", lambda: bt.pair(addr, pin.strip() or bt.DEFAULT_PIN), paired)
