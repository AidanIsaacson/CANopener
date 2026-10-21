"""Read a module over UDS (ISO 14229): its DTCs with status, and a data identifier.

This is the read side of UDS — how ABS, transmission, body and cluster modules report faults on
modern vehicles, which generic OBD-II doesn't reach. Reading sends requests, so it needs a
connection that isn't listen-only (same as a J1939 scan); it changes nothing on the module.
State-changing UDS services go through the confirm-first Transmit window instead.
"""

import threading

from .. import uds
from ..core.uds import UdsClient
from .qt import QtWidgets, Signal


class UdsDialog(QtWidgets.QDialog):
    _result = Signal(object)

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Read a module (UDS)")
        self.resize(560, 460)
        lay = QtWidgets.QVBoxLayout(self)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Module request ID (hex):"))
        self.module = QtWidgets.QLineEdit("7E0")
        self.module.setMaximumWidth(100)
        row.addWidget(self.module)
        row.addWidget(QtWidgets.QLabel("Data identifier (hex, optional):"))
        self.did = QtWidgets.QLineEdit("F190")
        self.did.setMaximumWidth(100)
        row.addWidget(self.did)
        row.addStretch(1)
        lay.addLayout(row)

        self.read_btn = QtWidgets.QPushButton("Read module")
        self.read_btn.clicked.connect(self._read)
        lay.addWidget(self.read_btn)

        self.out = QtWidgets.QTextBrowser()
        lay.addWidget(self.out, 1)
        self.note = QtWidgets.QLabel()
        self.note.setStyleSheet("color: #c92a2a")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        self._result.connect(self._show)
        self._reading = False
        self._update_enabled()

    def _update_enabled(self):
        ok = self.ctx.live and not self.ctx.session.listen_only and not self._reading
        self.read_btn.setEnabled(ok)
        if not self.ctx.live:
            self.note.setText("Connect to a bus first.")
        elif self.ctx.session.listen_only:
            self.note.setText("Listen-only is on: UDS reads need to send requests. Turn it off and reconnect.")
        else:
            self.note.setText("")

    def _read(self):
        try:
            req_id = int(self.module.text().strip(), 16)
        except ValueError:
            self.note.setText("Module ID must be hex, e.g. 7E0.")
            return
        did_text = self.did.text().strip()
        try:
            did = int(did_text, 16) if did_text else None
        except ValueError:
            self.note.setText("Data identifier must be hex, e.g. F190.")
            return
        session = self.ctx.session
        self._reading = True
        self._update_enabled()
        self.out.setText("Reading...")

        def work():
            lines = []
            client = UdsClient(session, request_id=req_id)
            try:
                try:
                    dtcs = client.read_dtcs()
                    if dtcs:
                        lines.append("<b>DTCs:</b>")
                        for code, status, flags in dtcs:
                            lines.append(f"&nbsp;&nbsp;{code} &nbsp; status 0x{status:02X} &nbsp; "
                                         f"{', '.join(flags) or '-'}")
                    else:
                        lines.append("<b>DTCs:</b> none")
                except uds.UdsError as e:
                    lines.append(f"<b>DTCs:</b> {e}")
                if did is not None:
                    try:
                        data = client.read_data_by_identifier(did)
                        text = bytes(b for b in data if 0x20 <= b < 0x7F).decode("ascii", "replace")
                        shown = data.hex(" ").upper() + (f" &nbsp; ({text})" if text.strip() else "")
                        lines.append(f"<b>DID {did:04X}:</b> {shown}")
                    except uds.UdsError as e:
                        lines.append(f"<b>DID {did:04X}:</b> {e}")
            finally:
                client.close()
            self._result.emit(lines)

        threading.Thread(target=work, name="uds-read", daemon=True).start()

    def _show(self, lines: list):
        self._reading = False
        self._update_enabled()
        self.out.setHtml("<br>".join(lines))
