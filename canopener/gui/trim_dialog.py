"""Trim a capture down to the stretch around its timestamps before saving it.

An hour of driving around until the intermittent fault shows, then three timestamps: keep a few
minutes before the first and after the last, drop the rest. The work is ``Capture.trim``; this
is the screen: the margins (remembered), what will be kept, and whether to keep the full log too.
"""

import os

from ..core import capture as cap
from ..core.capture import fmt_elapsed
from .qt import QtWidgets

TRIM, SAVE_ALL = 1, 2           # dialog results besides Rejected


def _mb(n: float) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 1e5 else f"{n / 1e3:.0f} kB"


class TrimDialog(QtWidgets.QDialog):
    def __init__(self, capture, settings, offer_save_all: bool = True, parent=None):
        super().__init__(parent)
        self.capture = capture
        self.settings = settings
        self.setWindowTitle("Trim the capture")
        lay = QtWidgets.QVBoxLayout(self)
        n = len(capture.tagged())
        intro = QtWidgets.QLabel(
            f"This capture is <b>{fmt_elapsed(capture.elapsed())[1:]}</b> long. Keep just the stretch around "
            f"your {n} timestamp{'s' if n != 1 else ''} and drop the rest?")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self.before = self._minutes("log/trim_before", cap.TRIM_BEFORE)
        self.after = self._minutes("log/trim_after", cap.TRIM_AFTER)
        form.addRow("Keep before the first timestamp", self.before)
        form.addRow("Keep after the last timestamp", self.after)
        lay.addLayout(form)
        self.preview = QtWidgets.QLabel()
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("font-weight: bold;")
        lay.addWidget(self.preview)
        self.keep_full = QtWidgets.QCheckBox("Also keep the full, untrimmed log (capture_full.log)")
        self.keep_full.setChecked(settings.value("log/trim_keep_full", "false") in (True, "true"))
        lay.addWidget(self.keep_full)

        bb = QtWidgets.QDialogButtonBox()
        self.trim_btn = bb.addButton("Trim and save" if offer_save_all else "Trim",
                                     QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self.trim_btn.clicked.connect(lambda: self._finish(TRIM))
        if offer_save_all:
            all_btn = bb.addButton("Save everything", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
            all_btn.clicked.connect(lambda: self._finish(SAVE_ALL))
        bb.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel).clicked.connect(self.reject)
        lay.addWidget(bb)
        for box in (self.before, self.after):
            box.valueChanged.connect(self._update)
        self._update()

    def _minutes(self, key: str, default_s: float) -> QtWidgets.QDoubleSpinBox:
        box = QtWidgets.QDoubleSpinBox()
        box.setRange(0, 240)
        box.setDecimals(1)
        box.setSingleStep(0.5)
        box.setSuffix(" min")
        try:
            box.setValue(float(self.settings.value(key, default_s / 60)))
        except (TypeError, ValueError):
            box.setValue(default_s / 60)
        return box

    @property
    def margins(self) -> tuple[float, float]:
        """(before, after) in seconds."""
        return self.before.value() * 60, self.after.value() * 60

    def _update(self):
        c = self.capture
        lo, hi = c.trim_window(*self.margins)
        total = c.elapsed() or 1.0
        try:
            size = os.path.getsize(c.log_path)
        except (OSError, TypeError):
            size = 0
        share = max(0.0, min(1.0, (hi - lo) / total))
        self.preview.setText(f"Keeps {fmt_elapsed(hi - lo)[1:]} of {fmt_elapsed(total)[1:]}"
                             + (f" (about {_mb(size * share)} of {_mb(size)})" if size else ""))
        self.trim_btn.setEnabled(share < 0.999)

    def _finish(self, result: int):
        before, after = self.margins
        self.settings.setValue("log/trim_before", before / 60)
        self.settings.setValue("log/trim_after", after / 60)
        self.settings.setValue("log/trim_keep_full", self.keep_full.isChecked())
        self.done(result)
