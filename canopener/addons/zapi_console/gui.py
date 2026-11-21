"""The Zapi console mode on the desktop: a tab with the taps, the messages going by and the analysis.

The screen only: listening, joining messages and the analysis are ``console.Console``, shared with
the handheld. While it listens the Log tool records the console lines with the bus (the context's
``serial``); a capture already running when listening starts goes on without them.
"""

import time

from ...gui.qt import Qt, QtGui, QtWidgets
from ...gui.widgets import item, setup_table
from ...serialline import port as sp
from . import shared_console
from .console import LINES

FRAMINGS = [f"{b} {f}" for b in (1200, 2400, 4800, 9600, 19200, 38400) for f in ("8N1", "8E1", "7E1")]
COLS = ["Time", "From", "Bytes", "What it says"]
SHOWN = 1000


class ZapiConsoleTab(QtWidgets.QWidget):
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.ctx = api.ctx
        self.con = shared_console(self.ctx)
        self._seen = 0                  # messages already in the table (the console's count)
        lay = QtWidgets.QVBoxLayout(self)

        form = QtWidgets.QHBoxLayout()
        self.port_boxes = {}
        for line, key, what in LINES:
            form.addWidget(QtWidgets.QLabel(line.capitalize()))
            box = QtWidgets.QComboBox()
            box.setEditable(True)
            box.setMinimumWidth(170)
            box.setToolTip(f"The serial adapter on {what} (a port, or a pyserial URL)")
            box.setCurrentText(self.con.settings.get(key) or "")
            box.currentTextChanged.connect(lambda text, key=key: self.con.settings.set(key, text.strip()))
            self.port_boxes[key] = box
            form.addWidget(box)
        refresh = QtWidgets.QPushButton("Find ports")
        refresh.clicked.connect(self.fill_ports)
        form.addWidget(refresh)
        form.addWidget(QtWidgets.QLabel("Framing"))
        self.framing = QtWidgets.QComboBox()
        self.framing.setEditable(True)
        self.framing.addItems(FRAMINGS)
        self.framing.setCurrentText(self.con.settings.get("zapi.framing") or "9600 8N1")
        self.framing.setToolTip("Baud rate and framing: a guess until it's measured (canopener-cli serial baud)")
        self.framing.currentTextChanged.connect(lambda text: self.con.settings.set("zapi.framing", text.strip()))
        form.addWidget(self.framing)
        form.addStretch(1)
        lay.addLayout(form)

        bar = QtWidgets.QHBoxLayout()
        self.listen = QtWidgets.QPushButton("Listen")
        self.listen.setToolTip("Open both taps, listen-only: nothing is sent to the controller")
        self.listen.clicked.connect(self.toggle_listen)
        bar.addWidget(self.listen)
        self.analyze_btn = QtWidgets.QPushButton("Analyze")
        self.analyze_btn.setToolTip("Work out what can be worked out from what's been heard: message kinds, "
                                    "checksums, who answers whom, text")
        self.analyze_btn.clicked.connect(self.analyze)
        bar.addWidget(self.analyze_btn)
        self.status = QtWidgets.QLabel(self.con.status())
        bar.addWidget(self.status, 1)
        lay.addLayout(bar)

        split = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        self.table = QtWidgets.QTableWidget()
        setup_table(self.table, COLS)
        split.addWidget(self.table)
        self.report = QtWidgets.QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.report.setPlaceholderText("Analyze shows what the bytes look like here. How to tap the line and "
                                       "read this: docs/SERIAL-SNIFFING.md")
        split.addWidget(self.report)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay.addWidget(split)
        api.add_refresh(self.refresh)

    def fill_ports(self):
        if not sp.available():
            self.api.status(sp.INSTALL_HINT)
            return
        found = [sp.by_id(p.device) for p in sp.list_ports()]
        for box in self.port_boxes.values():
            keep = box.currentText()
            box.blockSignals(True)
            box.clear()
            box.addItems([""] + found)
            box.setCurrentText(keep)
            box.blockSignals(False)
        self.api.status(f"{len(found)} serial port(s) found")

    def toggle_listen(self):
        if self.con.listening:
            self.con.stop()
            self.ctx.serial = None
        else:
            why = self.con.start()
            if why:
                QtWidgets.QMessageBox.warning(self, "Zapi console", why)
                return
            self.ctx.serial = self.con.sniffer
            cap = getattr(self.ctx, "capture", None)
            if cap is not None and cap.state == "recording":
                self.api.status("Listening. The capture running now started without it: stop and start it to "
                                "record the console too")
        self.refresh()

    def analyze(self):
        r = self.con.analyze()
        self.report.setPlainText("\n".join(r.lines()) if r.messages else "Nothing heard yet.")

    def refresh(self):
        self.con.poll()
        self.listen.setText("Stop" if self.con.listening else "Listen")
        self.status.setText(self.con.status())
        if self.con.count < self._seen:         # listening again: a fresh list
            self.table.setRowCount(0)
            self._seen = 0
        fresh = min(self.con.count - self._seen, SHOWN)
        if fresh <= 0:
            return
        new = self.con.recent(fresh)
        self._seen = self.con.count
        t = self.table
        at_end = t.verticalScrollBar().value() >= t.verticalScrollBar().maximum() - 2
        for s in new:
            m = s.message
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, item(time.strftime("%H:%M:%S", time.localtime(m.t)) + f".{int(m.t * 1000) % 1000:03d}"))
            t.setItem(r, 1, item(m.line))
            t.setItem(r, 2, item(s.hex))
            t.setItem(r, 3, item(s.text))
        while t.rowCount() > SHOWN:
            t.removeRow(0)
        if at_end:
            t.scrollToBottom()
