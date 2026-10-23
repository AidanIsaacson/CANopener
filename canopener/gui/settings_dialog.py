"""Settings dialogs."""

from ..core import sound, units
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


class SoundDialog(QtWidgets.QDialog):
    """Where sound cues go: the computer's speaker and/or a piezo buzzer (see ``core.sound``)."""

    BUZZERS = [("Off", "off"), ("Auto-detect beeper device", sound.AUTO), ("Device or PWM channel…", "custom")]

    def __init__(self, speaker: bool, buzzer: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sound cues")
        lay = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "Cues play when the signal finder locks on, and when traffic drops out or comes back. "
            "A PWM-driven piezo plays the same notes as the speaker.")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.speaker = QtWidgets.QCheckBox("Speaker (laptop / HDMI / headphone audio)")
        self.speaker.setChecked(speaker)
        lay.addWidget(self.speaker)
        form = QtWidgets.QFormLayout()
        self.mode = QtWidgets.QComboBox()
        for label, value in self.BUZZERS:
            self.mode.addItem(label, value)
        self.path = QtWidgets.QLineEdit()
        self.path.setPlaceholderText("/dev/input/event0  or  pwm:0:0")
        self.path.setToolTip("A kernel beeper input device (pwm-beeper / gpio-beeper), or a sysfs PWM "
                             "channel as pwm:CHIP:CHANNEL (on a Pi, dtoverlay=pwm,pin=18,func=2 gives pwm:0:0)")
        if buzzer in ("off", sound.AUTO, ""):
            self.mode.setCurrentIndex(max(self.mode.findData(buzzer or "off"), 0))
        else:
            self.mode.setCurrentIndex(self.mode.findData("custom"))
            self.path.setText(buzzer)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Buzzer", self.mode)
        form.addRow("", self.path)
        lay.addLayout(form)
        self.found = QtWidgets.QLabel()
        self.found.setWordWrap(True)
        self.found.setStyleSheet("color: gray")
        lay.addWidget(self.found)
        test = QtWidgets.QPushButton("Test")
        test.clicked.connect(self._test)
        lay.addWidget(test)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._mode_changed()

    def _mode_changed(self, *_):
        custom = self.mode.currentData() == "custom"
        self.path.setVisible(custom)
        beeper = sound.find_beeper()
        self.found.setText(f"Beeper device found: {beeper}" if beeper else
                           "No writable beeper device found (pwm-beeper / gpio-beeper overlay, and the "
                           "'input' group for access). A PWM channel can be used instead.")
        self.found.setVisible(self.mode.currentData() != "off")

    def buzzer(self) -> str:
        mode = self.mode.currentData()
        return self.path.text().strip() if mode == "custom" else mode

    def _test(self):
        spk = self.speaker.isChecked() and sound.play_speaker("test")
        bz = sound.play_buzzer("test", self.buzzer(), wait=True)
        if not spk and not bz:
            QtWidgets.QMessageBox.warning(
                self, "Sound cues", "Nothing could play the test. Speaker: needs pw-play, paplay or aplay. "
                "Buzzer: check the device path and that your user can write to it.")
