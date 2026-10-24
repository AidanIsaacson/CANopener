"""Read a module over UDS (ISO 14229): its DTCs with status, and a data identifier; and the two
standard commands, clear its codes (0x14) and reset it (0x11).

UDS is how ABS, transmission, body and cluster modules report faults on modern vehicles, which
generic OBD-II doesn't reach. It sends requests, so it needs a connection that isn't listen-only.
The commands ask first. Other services (routines, I/O control, writes) can be composed in the
Transmit window or sent with ``UdsClient.request``.
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

        btns = QtWidgets.QHBoxLayout()
        self.read_btn = QtWidgets.QPushButton("Read module")
        self.read_btn.clicked.connect(self._read)
        btns.addWidget(self.read_btn)
        btns.addStretch(1)
        self.clear_btn = QtWidgets.QPushButton("Clear codes (0x14)…")
        self.clear_btn.setToolTip("ClearDiagnosticInformation, all groups")
        self.clear_btn.clicked.connect(self._clear)
        btns.addWidget(self.clear_btn)
        self.reset_box = QtWidgets.QComboBox()
        for kind, name in uds.RESETS.items():
            self.reset_box.addItem(name, kind)
        btns.addWidget(self.reset_box)
        self.reset_btn = QtWidgets.QPushButton("Reset module (0x11)…")
        self.reset_btn.clicked.connect(self._reset)
        btns.addWidget(self.reset_btn)
        lay.addLayout(btns)

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
        for b in (self.read_btn, self.clear_btn, self.reset_btn):
            b.setEnabled(ok)
        if not self.ctx.live:
            self.note.setText("Connect to a bus first.")
        elif self.ctx.session.listen_only:
            self.note.setText("Listen-only is on: UDS reads need to send requests. Turn it off and reconnect.")
        else:
            self.note.setText("")

    def _module_id(self) -> int | None:
        try:
            return int(self.module.text().strip(), 16)
        except ValueError:
            self.note.setText("Module ID must be hex, e.g. 7E0.")
            return None

    def _confirm(self, text: str) -> bool:
        r = QtWidgets.QMessageBox.warning(self, "UDS command", text,
                                          QtWidgets.QMessageBox.StandardButton.Yes
                                          | QtWidgets.QMessageBox.StandardButton.No,
                                          QtWidgets.QMessageBox.StandardButton.No)
        return r == QtWidgets.QMessageBox.StandardButton.Yes

    def _command(self, label: str, fn):
        """Run ``fn(client)`` for a state-changing service and report the outcome."""
        req_id = self._module_id()
        if req_id is None:
            return
        session = self.ctx.session
        self._reading = True
        self._update_enabled()

        def work():
            client = UdsClient(session, request_id=req_id)
            try:
                fn(client)
                line = f"<b>{label}:</b> done (module 0x{req_id:X})"
            except uds.UdsError as e:
                line = f"<b>{label}:</b> {e}"
            finally:
                client.close()
            self._result.emit([line])

        threading.Thread(target=work, name="uds-command", daemon=True).start()

    def _clear(self):
        if self._confirm(f"Clear all trouble codes in module 0x{self.module.text().strip().upper()} "
                         "(UDS ClearDiagnosticInformation)?\n\nThis also erases its freeze-frame / "
                         "snapshot data. Engine off, key on."):
            self._command("Clear codes", lambda c: c.clear_dtcs())

    def _reset(self):
        kind, name = self.reset_box.currentData(), self.reset_box.currentText()
        if self._confirm(f"{name} module 0x{self.module.text().strip().upper()} (UDS ECUReset)?\n\n"
                         "The module restarts: whatever it controls stops for a moment. Never while "
                         "driving."):
            self._command(name, lambda c: c.ecu_reset(kind))

    def _read(self):
        req_id = self._module_id()
        if req_id is None:
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
