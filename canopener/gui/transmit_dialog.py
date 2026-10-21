"""The confirm-first transmit window.

Deliberately separate from the read-only views. It stays harmless unless the user ticks
"Transmit mode ON", is inert whenever the session is listen-only or offline, and shows exactly
what will go on the bus (CAN ID, bytes, destination) in a confirmation step before every send.
Standard public actions (a J1939 Request, a raw frame) come from core.transmit; plugins can add
their own actions, which appear here behind the same confirmation.
"""

from ..core import transmit
from .qt import QtWidgets


class TransmitDialog(QtWidgets.QDialog):
    def __init__(self, ctx, transmitter: transmit.Transmitter, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.tx = transmitter
        self.setWindowTitle("Transmit a frame")
        self.resize(560, 460)
        lay = QtWidgets.QVBoxLayout(self)

        self.arm = QtWidgets.QCheckBox("Transmit mode ON — frames below will be sent to the bus")
        self.arm.setStyleSheet("QCheckBox { font-weight: bold; color: #c92a2a; }")
        self.arm.setChecked(self.tx.enabled)
        self.arm.toggled.connect(self._arm_toggled)
        lay.addWidget(self.arm)

        form_box = QtWidgets.QGroupBox("Frame to send")
        fl = QtWidgets.QVBoxLayout(form_box)
        self.action = QtWidgets.QComboBox()
        for a in transmit.actions():
            self.action.addItem(a.name, a)
        self.action.currentIndexChanged.connect(self._rebuild_fields)
        fl.addWidget(self.action)
        self.action_desc = QtWidgets.QLabel()
        self.action_desc.setWordWrap(True)
        self.action_desc.setStyleSheet("color: gray")
        fl.addWidget(self.action_desc)
        self.fields_host = QtWidgets.QWidget()
        self.fields_form = QtWidgets.QFormLayout(self.fields_host)
        fl.addWidget(self.fields_host)
        lay.addWidget(form_box)

        self.send_btn = QtWidgets.QPushButton("Send...")
        self.send_btn.clicked.connect(self._send)
        lay.addWidget(self.send_btn)

        lay.addWidget(QtWidgets.QLabel("Sent this session:"))
        self.log = QtWidgets.QListWidget()
        lay.addWidget(self.log, 1)
        self.reason = QtWidgets.QLabel()
        self.reason.setStyleSheet("color: #c92a2a")
        self.reason.setWordWrap(True)
        lay.addWidget(self.reason)

        self._editors: dict[str, QtWidgets.QLineEdit] = {}
        self._rebuild_fields()
        self._refresh_log()
        self._update_enabled()

    def _arm_toggled(self, on: bool):
        self.tx.enabled = on
        self._update_enabled()

    def _rebuild_fields(self):
        while self.fields_form.rowCount():
            self.fields_form.removeRow(0)
        self._editors = {}
        action: transmit.TransmitAction = self.action.currentData()
        if action is None:
            return
        self.action_desc.setText(action.description)
        for key, label, default in action.fields:
            edit = QtWidgets.QLineEdit(default)
            self.fields_form.addRow(label, edit)
            self._editors[key] = edit

    def _update_enabled(self):
        live = self.ctx.live and not self.ctx.session.listen_only
        if not self.ctx.live:
            msg = "Connect to a bus first."
        elif self.ctx.session.listen_only:
            msg = "Listen-only is on: transmitting is disabled. Turn it off and reconnect to send."
        elif not self.tx.enabled:
            msg = "Tick 'Transmit mode ON' above to allow sending."
        else:
            msg = ""
        self.reason.setText(msg)
        self.send_btn.setEnabled(bool(live and self.tx.enabled))

    def _send(self):
        action: transmit.TransmitAction = self.action.currentData()
        if action is None:
            return
        values = {k: e.text().strip() for k, e in self._editors.items()}
        try:
            frames = action.build(values)
        except (ValueError, KeyError) as e:
            QtWidgets.QMessageBox.warning(self, "Transmit", f"Couldn't build that frame: {e}")
            return
        preview = "\n".join(transmit.describe(f) for f in frames)
        confirm = QtWidgets.QMessageBox.warning(
            self, "Send to the bus?",
            f"This will transmit the following onto the live bus:\n\n{preview}\n\nSend it?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel)
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        for f in frames:
            try:
                ok = self.tx.send(f, action.name)
            except transmit.TransmitDisabled:
                ok = False
            if not ok:
                QtWidgets.QMessageBox.warning(self, "Transmit", "The frame was not sent (bus refused it "
                                              "or transmit mode is off).")
                break
        self._refresh_log()

    def _refresh_log(self):
        self.log.clear()
        for rec in self.tx.log[-200:]:
            mark = "✓" if rec.ok else "✗"
            self.log.addItem(f"{mark}  {transmit.describe(rec.frame)}")
        self.log.scrollToBottom()
