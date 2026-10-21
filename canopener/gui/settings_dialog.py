"""Settings dialogs."""

from ..core import units
from .qt import QtWidgets


class UnitsDialog(QtWidgets.QDialog):
    """Pick a display unit per quantity (e.g. psi for pressure but °C for temperature)."""

    def __init__(self, current: dict[str, str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Units")
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel("Choose how each kind of value is shown. Logs and saved data "
                                       "keep the original units; this only changes the display, "
                                       "reports and spreadsheets."))
        form = QtWidgets.QFormLayout()
        self.boxes: dict[str, QtWidgets.QComboBox] = {}
        for q, (_, options) in units.QUANTITIES.items():
            box = QtWidgets.QComboBox()
            for u in options:
                box.addItem(u, u)
            box.setCurrentIndex(max(box.findData(current.get(q)), 0))
            form.addRow(units.QUANTITY_LABELS[q], box)
            self.boxes[q] = box
        lay.addLayout(form)
        pl = QtWidgets.QHBoxLayout()
        pl.addWidget(QtWidgets.QLabel("Start from:"))
        for label, system in (("Metric", units.METRIC), ("Imperial", units.IMPERIAL)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, s=system: self._preset(s))
            pl.addWidget(b)
        pl.addStretch(1)
        lay.addLayout(pl)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _preset(self, system: str):
        for q, u in units.PRESETS[system].items():
            self.boxes[q].setCurrentIndex(max(self.boxes[q].findData(u), 0))

    def choice(self) -> dict[str, str]:
        return {q: box.currentData() for q, box in self.boxes.items()}
