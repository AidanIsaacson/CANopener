"""OBD-II (automotive J1979) tab: read a car's VIN, trouble codes and live data over CAN.

Separate from the J1939 Fault Codes tab because OBD-II is a different protocol (ISO-TP on 11-bit
IDs, usually at 500 kbps). Reading requires sending requests, so it needs a connection that is not
listen-only. The protocol work is in core.obd.ObdClient; this tab runs it on a worker thread and
shows the result.
"""

import threading

from .. import obd
from ..core import units
from ..core.obd import ObdClient
from .qt import Qt, QtWidgets, Signal
from .widgets import setup_table


class ObdTab(QtWidgets.QWidget):
    title = "OBD-II (Auto)"
    _result = Signal(object)        # dict from a worker thread -> _show on the GUI thread

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._reading = False
        lay = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.read_btn = QtWidgets.QPushButton("Read Vehicle (OBD-II)")
        self.read_btn.setMinimumHeight(44)
        self.read_btn.clicked.connect(self._read)
        bar.addWidget(self.read_btn)
        bar.addStretch(1)
        self.vin_label = QtWidgets.QLabel("")
        bar.addWidget(self.vin_label)
        lay.addLayout(bar)

        self.dtc_label = QtWidgets.QLabel("")
        self.dtc_label.setWordWrap(True)
        lay.addWidget(self.dtc_label)

        self.table = QtWidgets.QTableWidget()
        setup_table(self.table, ["Parameter", "Value"], stretch=(0,))
        self.table.setColumnWidth(1, 140)
        lay.addWidget(self.table)

        self.note = QtWidgets.QLabel("Pick the OBD-II simulator (or a 500k SocketCAN car bus) and connect, "
                                     "then Read. Needs to send requests, so not available in listen-only.")
        self.note.setStyleSheet("color: gray")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        self._result.connect(self._show)
        ctx.session_changed.connect(lambda _: self._update_enabled())
        ctx.busy_changed.connect(lambda _: self._update_enabled())
        ctx.units_changed.connect(lambda _: self._reapply_units())
        self._last = None
        self._update_enabled()

    def _update_enabled(self):
        live = self.ctx.live and not self.ctx.session.listen_only
        self.read_btn.setEnabled(live and not self.ctx.busy and not self._reading)

    def _read(self):
        if self._reading or not self.ctx.live:
            return
        session = self.ctx.session
        self._reading = True
        self._update_enabled()
        self.ctx.busy = True
        self.ctx.busy_changed.emit(True)
        self.vin_label.setText("Reading...")

        def work():
            payload = {}
            try:
                client = ObdClient(session)
                try:
                    payload["info"] = client.read_info()
                    payload["dtcs"] = client.read_dtcs()
                    payload["live"] = [(r.name, r.value, r.unit) for r in client.read_live()]
                finally:
                    client.close()
            except Exception as e:                      # noqa: BLE001 - report any failure in the UI
                payload["error"] = str(e)
            self._result.emit(payload)

        threading.Thread(target=work, name="obd-read", daemon=True).start()

    def _show(self, payload: dict):
        self._reading = False
        self.ctx.busy = False
        self.ctx.busy_changed.emit(False)
        self._update_enabled()
        if "error" in payload:
            self.vin_label.setText("")
            QtWidgets.QMessageBox.warning(self, "OBD-II", f"Read failed: {payload['error']}")
            return
        self._last = payload
        info = dict(payload.get("info") or [])
        vin = info.get("VIN")
        extra = "  ·  ".join(f"{k}: {v}" for k, v in info.items() if k != "VIN")
        self.vin_label.setText((f"VIN: {vin}" if vin else "VIN: (not reported)")
                               + (f"   ({extra})" if extra else ""))
        dtcs = payload.get("dtcs") or {}
        lines = []
        for label in ("stored", "pending", "permanent"):
            codes = dtcs.get(label) or []
            if not codes:
                lines.append(f"<b>{label.capitalize()}:</b> none")
                continue
            described = "; ".join(f"{c} — {obd.describe_dtc(c)}" for c in codes)
            lines.append(f"<b>{label.capitalize()}:</b> {described}")
        self.dtc_label.setText("<br>".join(lines))
        self._reapply_units()

    def _reapply_units(self):
        live = (self._last or {}).get("live") or []
        self.table.setRowCount(len(live))
        for row, (name, value, unit) in enumerate(live):
            shown = units.fmt(value, unit, system=self.ctx.units) if unit else str(value)
            name_item = QtWidgets.QTableWidgetItem(name)
            val_item = QtWidgets.QTableWidgetItem(shown)
            val_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, val_item)
