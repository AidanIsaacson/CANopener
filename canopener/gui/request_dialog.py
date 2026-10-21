"""Diagnostics > Request a PGN: ask one ECU (or all) for any parameter group and show the answer."""

import time

from ..j1939.addresses import sa_name
from ..j1939.constants import GLOBAL_SA, pgn_label
from ..j1939.fields import hexs
from ..j1939.info import describe as describe_info
from ..j1939.signals import all_signals
from .qt import QtCore, QtWidgets
from .widgets import mono_font

WAIT_S = 1.5


def parse_number(text: str) -> int:
    text = text.strip()
    return int(text, 16) if text.lower().startswith("0x") or any(c in "abcdefABCDEF" for c in text) \
        else int(text)


class RequestDialog(QtWidgets.QDialog):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Request a PGN")
        self.resize(640, 420)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel("Sends one J1939 Request (PGN 59904) and shows what comes back. "
                                       "Read-only: ECUs answer with data, a NACK, or nothing."))
        form = QtWidgets.QFormLayout()
        self.pgn = QtWidgets.QLineEdit("0xFEEB")
        self.pgn.setPlaceholderText("e.g. 0xFEEB (component ID) or 65259")
        form.addRow("PGN", self.pgn)
        self.dest = QtWidgets.QComboBox()
        self.dest.setEditable(True)
        self.dest.addItem("FF  Global (everyone)", GLOBAL_SA)
        s = ctx.session
        for sa in (s.known_ecus() if s else []):
            self.dest.addItem(sa_name(sa), sa)
        form.addRow("Ask", self.dest)
        lay.addLayout(form)
        self.send = QtWidgets.QPushButton("Send request")
        self.send.clicked.connect(self._send)
        lay.addWidget(self.send)
        self.out = QtWidgets.QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(mono_font())
        lay.addWidget(self.out, 1)
        self._t0 = None
        self._pgn = None
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._collect)

    def _dest(self) -> int:
        data = self.dest.currentData()
        if data is not None and self.dest.currentText() == self.dest.itemText(self.dest.currentIndex()):
            return data
        return parse_number(self.dest.currentText().split()[0])

    def _send(self):
        s = self.ctx.session
        if not self.ctx.live:
            self.out.setPlainText("Connect to a bus first.")
            return
        try:
            pgn, da = parse_number(self.pgn.text()), self._dest()
        except ValueError:
            self.out.setPlainText("PGN and destination must be numbers (decimal or 0x hex).")
            return
        if s.listen_only:
            self.out.setPlainText("Listen-only is on: nothing can be sent.")
            return
        self._t0, self._pgn = time.time(), pgn
        if not s.request(pgn, da):
            self.out.setPlainText(f"Send failed: {s.health.tx_error or 'unknown error'}")
            return
        self.out.setPlainText(f"Requested PGN {pgn:05X} ({pgn_label(pgn) or 'unknown'}) from "
                              f"{sa_name(da)} - waiting {WAIT_S:.1f} s ...")
        self.send.setEnabled(False)
        self.timer.start(int(WAIT_S * 1000))

    def _collect(self):
        self.timer.stop()
        self.send.setEnabled(True)
        s = self.ctx.session
        if s is None:
            return
        got = {sa: d for (p, sa), (t, d) in s.snapshot_since(self._t0).items() if p == self._pgn}
        nacks = [a for a in s.acks_since(self._t0) if a.pgn == self._pgn]
        lines = [self.out.toPlainText(), ""]
        sigs = [sig for sig in all_signals() if sig.pgn == self._pgn]
        for sa, d in sorted(got.items()):
            lines.append(f"{sa_name(sa)}: {len(d)} byte(s)")
            lines.append(f"  raw: {hexs(d)}")
            info = describe_info(self._pgn, d)
            if info and info != hexs(d):
                lines.append(f"  decoded: {info}")
            for sig in sigs:
                if sig.accepts(sa):
                    v = sig.decode(d)
                    lines.append(f"  {sig.name}: {sig.text(v) or ('n/a' if v is None else f'{v:g} {sig.unit}')}")
        for a in nacks:
            lines.append(f"{sa_name(a.sa)}: {a.text}")
        if not got and not nacks:
            lines.append("No answer. The ECU may not support this PGN, or doesn't answer global requests "
                         "(try asking it directly).")
        self.out.setPlainText("\n".join(lines))
