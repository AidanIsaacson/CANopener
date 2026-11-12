"""The Log tool's Machine… dialog: what was logged (make, model, serial, ...), the concern, and
what to look at in the data. Saved in the capture (``Capture.set_machine``), in any state."""

from ..core import capture as cap
from ..core import identity
from .qt import QtWidgets

QUESTION_HINT = ("What to look at in the data, for whoever reads it next: e.g. \"PGN 0xFF12 from the "
                 "controller doesn't decode\" or \"does the voltage dip before the shutdown?\"")


class MachineDialog(QtWidgets.QDialog):
    def __init__(self, capture, session=None, obd_info_fn=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Machine details")
        self.capture = capture
        self.session = session
        self.obd_info_fn = obd_info_fn          # () -> ObdSniffer.info, or None
        lay = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        self.recent = QtWidgets.QComboBox()
        self.recent.setToolTip("Machines you've entered before")
        self.recent.addItem("Recent machines…", None)
        for m in cap.recent_machines():
            self.recent.addItem(cap.machine_summary(m) or "(no make / model)", m)
        self.recent.setEnabled(self.recent.count() > 1)
        self.recent.activated.connect(self._pick_recent)
        top.addWidget(self.recent, 1)
        self.bus_btn = QtWidgets.QPushButton("From the bus ▾")
        self.bus_btn.setToolTip("Fill in from what the machine sent: Component ID, VIN, CANopen identity, hours. "
                                "Read Machine Info (Machine Info tab) asks for them.")
        self.bus_btn.clicked.connect(self._from_bus)
        top.addWidget(self.bus_btn)
        lay.addLayout(top)

        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit(capture.name)
        self.name.setPlaceholderText("Job / log name (the folder is named after it)")
        form.addRow("Log name", self.name)
        self.fields: dict[str, QtWidgets.QLineEdit] = {}
        for key, label in cap.MACHINE_FIELDS:
            e = QtWidgets.QLineEdit(capture.machine.get(key, ""))
            self.fields[key] = e
            form.addRow(label, e)
        self.notes = QtWidgets.QPlainTextEdit(capture.notes)
        self.notes.setPlaceholderText("What the customer / operator says it does")
        self.notes.setMaximumHeight(80)
        form.addRow("Concern", self.notes)
        self.question = QtWidgets.QPlainTextEdit(capture.question)
        self.question.setPlaceholderText(QUESTION_HINT)
        self.question.setMaximumHeight(80)
        form.addRow("What to look at", self.question)
        lay.addLayout(form)

        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: gray")
        lay.addWidget(self.hint)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(480, self.sizeHint().height())

    def machine(self) -> dict:
        return {k: e.text() for k, e in self.fields.items()}

    def fill(self, values: dict, only_empty: bool = False):
        for k, v in values.items():
            e = self.fields.get(k)
            if e is not None and v and not (only_empty and e.text().strip()):
                e.setText(str(v))

    def _pick_recent(self, i: int):
        m = self.recent.itemData(i)
        if m:
            for e in self.fields.values():
                e.clear()
            self.fill(m)
        self.recent.setCurrentIndex(0)

    def candidates(self) -> list:
        info = self.obd_info_fn() if self.obd_info_fn else None
        return identity.candidates(self.session, info)

    def _from_bus(self):
        got = self.candidates()
        if not got:
            self.hint.setText("Nothing heard yet that names the machine. Connect, press Read Machine Info "
                              "(Machine Info tab), then try again.")
            return
        menu = QtWidgets.QMenu(self)
        for c in got:
            act = menu.addAction(c.label)
            act.setToolTip(c.source)
            act.triggered.connect(lambda _=False, c=c: self._use(c))
        menu.exec(self.bus_btn.mapToGlobal(self.bus_btn.rect().bottomLeft()))

    def _use(self, c):
        self.fill(c.fields)
        self.hint.setText(f"Filled in from {c.source}.")

    def accept(self):
        self.capture.set_machine(self.machine(), name=self.name.text(), notes=self.notes.toPlainText(),
                                 question=self.question.toPlainText())
        cap.remember_machine(self.capture.machine)
        super().accept()
