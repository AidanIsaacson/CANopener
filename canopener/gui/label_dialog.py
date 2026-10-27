"""'Label this data…': name a value inside a message, with a live preview while the machine runs.

Pick where the value is (byte, size or bit field), then tune scale / offset while you work the
control and watch the number: the preview uses the same decoder as Live Data. Saved into the user
profile ``ctx.label_target()`` picked (``core.labeling``); the signal shows up in Live Data at once.
"""

import time
from collections import deque

from ..core import labeling, profiles, units
from ..j1939.addresses import sa_name
from ..j1939.constants import pgn_label
from .qt import Qt, QtCore, QtWidgets
from .widgets import StripChart, mono_font

SIZES = [("1 byte", 1), ("2 bytes", 2), ("4 bytes", 4), ("Bits", 0)]
COMMON_UNITS = ["", "%", "V", "A", "rpm", "km/h", "°C", "bar", "kPa", "°", "mm", "kg", "h", "s",
                "counts"]


def _num(text: str, default=None):
    text = text.strip().replace(",", ".")
    if not text:
        return default
    return float(text)


def _fmt(v) -> str:
    return "" if v is None else f"{v:g}"


def parse_enum(text: str) -> dict:
    """'0 = Off' lines -> {"0": "Off"} (also accepts '0: Off'). Raises ValueError on a bad line."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        sep = "=" if "=" in line else ":"
        k, _, v = line.partition(sep)
        if not v.strip():
            raise ValueError(f"states: write each line as 'value = text', not {line!r}")
        out[str(profiles._int(k.strip()))] = v.strip()
    return out


class LabelDialog(QtWidgets.QDialog):
    def __init__(self, ctx, pid: str, draft: dict, replace_key: str | None = None, parent=None):
        super().__init__(parent)
        self.ctx, self.pid, self.replace_key = ctx, pid, replace_key
        self.saved = None
        d = dict(draft)
        self.pgn = profiles._int(d["pgn"])
        sa = d.get("sa")
        if isinstance(sa, (list, tuple)):
            self.sa_value = tuple(profiles._int(x) for x in sa)
            self.src = self.sa_value[0] if self.sa_value else None
        else:
            self.sa_value = None if sa is None else profiles._int(sa)
            self.src = self.sa_value
        self.setWindowTitle("Edit label" if replace_key else "Label this data")
        self.resize(640, 640)
        lay = QtWidgets.QVBoxLayout(self)

        where = f"PGN {self.pgn:04X}  {pgn_label(self.pgn)}"
        if self.src is not None:
            where += f"\nfrom {sa_name(self.src)}"
        head = QtWidgets.QLabel(where)
        head.setWordWrap(True)
        lay.addWidget(head)
        target = profiles.find(pid)
        note = QtWidgets.QLabel(f"Saves to your profile <b>{target.name if target else pid}</b>.")
        note.setStyleSheet("color: gray")
        lay.addWidget(note)

        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit(str(d.get("name", "")))
        self.name.setPlaceholderText("e.g. Accelerator pedal")
        form.addRow("Name", self.name)

        pos = QtWidgets.QHBoxLayout()
        self.start = QtWidgets.QSpinBox()
        self.start.setRange(0, 1784)
        self.start.setValue(int(d.get("start", 0)))
        pos.addWidget(QtWidgets.QLabel("byte"))
        pos.addWidget(self.start)
        self.size = QtWidgets.QComboBox()
        for text, n in SIZES:
            self.size.addItem(text, n)
        bits = int(d.get("bits", 0) or 0)
        self.size.setCurrentIndex(self.size.findData(0 if bits else int(d.get("length", 1))))
        pos.addWidget(self.size)
        self.bit = QtWidgets.QSpinBox()
        self.bit.setRange(0, 7)
        self.bit.setValue(int(d.get("bit", 0)))
        self.bit.setPrefix("from bit ")
        self.bits = QtWidgets.QSpinBox()
        self.bits.setRange(1, 32)
        self.bits.setValue(bits or 1)
        self.bits.setSuffix(" bit(s)")
        pos.addWidget(self.bit)
        pos.addWidget(self.bits)
        self.signed = QtWidgets.QCheckBox("signed")
        self.signed.setChecked(bool(d.get("signed", False)))
        pos.addWidget(self.signed)
        pos.addStretch(1)
        form.addRow("Where", pos)

        conv = QtWidgets.QHBoxLayout()
        self.scale = QtWidgets.QLineEdit(_fmt(d.get("scale", 1)))
        self.offset = QtWidgets.QLineEdit(_fmt(d.get("offset", 0)))
        self.unit = QtWidgets.QComboBox()
        self.unit.setEditable(True)
        self.unit.addItems(COMMON_UNITS)
        self.unit.setCurrentText(str(d.get("unit", "")))
        self.decimals = QtWidgets.QSpinBox()
        self.decimals.setRange(0, 6)
        self.decimals.setValue(int(d.get("decimals", 0 if bits else 1)))
        for label, w in (("× scale", self.scale), ("+ offset", self.offset), ("unit", self.unit),
                         ("decimals", self.decimals)):
            conv.addWidget(QtWidgets.QLabel(label))
            conv.addWidget(w)
        form.addRow("Value = raw", conv)

        self.group = QtWidgets.QLineEdit(str(d.get("group", "My labels")))
        self.group.setPlaceholderText("Live Data group, e.g. Drive")
        form.addRow("Group", self.group)
        rng = QtWidgets.QHBoxLayout()
        self.low = QtWidgets.QLineEdit(_fmt(d.get("low")))
        self.high = QtWidgets.QLineEdit(_fmt(d.get("high")))
        self.low.setPlaceholderText("low")
        self.high.setPlaceholderText("high")
        rng.addWidget(self.low)
        rng.addWidget(QtWidgets.QLabel("to"))
        rng.addWidget(self.high)
        form.addRow("Normal range", rng)
        self.enum = QtWidgets.QPlainTextEdit("\n".join(f"{k} = {v}" for k, v in (d.get("enum") or {}).items()))
        self.enum.setPlaceholderText("Optional states, one per line:  0 = Off   1 = On")
        self.enum.setMaximumHeight(70)
        form.addRow("States", self.enum)
        if isinstance(self.sa_value, tuple):
            only = "Only from modules " + ", ".join(f"{a:02X}" for a in self.sa_value)
        elif self.sa_value is not None:
            only = f"Only from module {sa_name(self.sa_value)}"
        else:
            only = "Only from one module"
        self.only = QtWidgets.QCheckBox(only)
        self.only.setChecked(self.sa_value is not None)
        self.only.setEnabled(self.sa_value is not None)
        form.addRow("", self.only)
        lay.addLayout(form)

        box = QtWidgets.QGroupBox("Live preview (work the control and watch)")
        bl = QtWidgets.QVBoxLayout(box)
        self.bytes_label = QtWidgets.QLabel("")
        self.bytes_label.setFont(mono_font())
        self.bytes_label.setTextFormat(Qt.TextFormat.RichText)
        bl.addWidget(self.bytes_label)
        self.value_label = QtWidgets.QLabel("")
        f = self.value_label.font()
        f.setPointSizeF(f.pointSizeF() * 1.6)
        f.setBold(True)
        self.value_label.setFont(f)
        bl.addWidget(self.value_label)
        self.points: deque = deque(maxlen=600)
        self.chart = StripChart()
        self.chart.set_window(30)
        self.chart.setMinimumHeight(110)
        bl.addWidget(self.chart)
        lay.addWidget(box, 1)

        self.error = QtWidgets.QLabel("")
        self.error.setStyleSheet("color: #e03131")
        self.error.setWordWrap(True)
        lay.addWidget(self.error)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save
                                             | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        for w in (self.start, self.bit, self.bits, self.decimals):
            w.valueChanged.connect(self._changed)
        self.size.currentIndexChanged.connect(self._changed)
        self.signed.toggled.connect(self._changed)
        for w in (self.scale, self.offset, self.name, self.low, self.high):
            w.textChanged.connect(self._changed)
        self.unit.currentTextChanged.connect(self._changed)
        self.enum.textChanged.connect(self._changed)
        self._last_t = None
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(200)
        self._changed()

    # --- the draft ----------------------------------------------------------------------------
    def draft(self) -> dict:
        """The signal as it would be saved. Raises ValueError with a plain message."""
        n = self.size.currentData()
        d = {"name": self.name.text().strip(), "pgn": self.pgn, "start": self.start.value(),
             "scale": _num(self.scale.text(), 1.0), "offset": _num(self.offset.text(), 0.0),
             "unit": units.normalize_unit(self.unit.currentText().strip()), "decimals": self.decimals.value(),
             "signed": self.signed.isChecked(), "group": self.group.text().strip(),
             "low": _num(self.low.text()), "high": _num(self.high.text()),
             "enum": parse_enum(self.enum.toPlainText())}
        if n:
            d["length"] = n
        else:
            d.update(bit=self.bit.value(), bits=self.bits.value())
        if self.only.isChecked() and self.sa_value is not None:
            d["sa"] = list(self.sa_value) if isinstance(self.sa_value, tuple) else self.sa_value
        return d

    def _changed(self, *_):
        bits = self.size.currentData() == 0
        self.bit.setVisible(bits)
        self.bits.setVisible(bits)
        self.points.clear()
        self._last_t = None
        self.tick(force=True)

    def _data(self) -> tuple[float, bytes] | None:
        s = self.ctx.session
        if s is None:
            return None
        with s.lock:
            if self.src is not None:
                return s.latest.get((self.pgn, self.src))
            got = [v for (pgn, _sa), v in s.latest.items() if pgn == self.pgn]
        return max(got, default=None)

    def _span(self) -> range:
        start = self.start.value()
        n = self.size.currentData()
        width = n * 8 if n else self.bit.value() + self.bits.value()
        return range(start, start + (width + 7) // 8)

    def tick(self, force: bool = False):
        got = self._data()
        if got is None:
            self.bytes_label.setText("<i>no data from this message yet</i>")
            self.value_label.setText("")
            return
        t, data = got
        if t == self._last_t and not force:
            return
        self._last_t = t
        span = self._span()
        cells = [f"<b style='background:#ffa94d;color:black'>&nbsp;{b:02X}&nbsp;</b>" if i in span
                 else f"&nbsp;{b:02X}&nbsp;" for i, b in enumerate(data[:max(8, span.stop)])]
        self.bytes_label.setText(" ".join(cells))
        try:
            d = self.draft()
            raw, _ = labeling.preview({**d, "scale": 1, "offset": 0, "enum": {}, "low": None, "high": None}, data)
            v, text = labeling.preview(d, data)
        except ValueError as e:
            self.error.setText(str(e))
            self.value_label.setText("")
            return
        self.error.setText("")
        raw_s = "n/a" if raw is None else f"{raw:g}"
        self.value_label.setText(f"{text}     <span style='font-size:small;color:gray'>raw {raw_s}</span>")
        if v is not None:
            self.points.append((time.time() if not self.ctx.session.offline else t, v))
            self.chart.follow_data = self.ctx.session.offline
            self.chart.set_series([("v", d["name"] or "value", d["unit"], d["decimals"], self.points)])

    def accept(self):
        try:
            self.saved = labeling.put_signal(self.pid, self.draft(), self.replace_key)
        except (ValueError, profiles.ProfileError) as e:
            self.error.setText(f"Can't save yet: {e}")
            return
        self.timer.stop()
        self.ctx.labels_saved.emit(self.pid)
        super().accept()

    def reject(self):
        self.timer.stop()
        super().reject()


def open_label_dialog(ctx, draft: dict, parent=None, pid: str | None = None,
                      replace_key: str | None = None) -> bool:
    """Open the dialog for ``draft`` (pgn / sa / start ...); without ``pid``, ask the main window
    which user profile to save into (it may offer to make one). True if a label was saved."""
    pid = pid or ctx.label_target()
    if not pid:
        return False
    return bool(LabelDialog(ctx, pid, draft, replace_key, parent).exec())


def name_module(ctx, sa: int, parent=None) -> bool:
    """'Name this module…': a name for a source address, saved in the user profile."""
    pid = ctx.label_target()
    if not pid:
        return False
    name, ok = QtWidgets.QInputDialog.getText(
        parent, "Name this module",
        f"Name for the module at address {sa:02X} ({sa}).\nLeave empty to go back to the standard name.",
        text=sa_name(sa, with_hex=False))
    if not ok:
        return False
    labeling.set_module_name(pid, sa, name)
    ctx.labels_saved.emit(pid)
    return True
