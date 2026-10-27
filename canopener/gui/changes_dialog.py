"""'Find what changed': compare two moments and list the data that moved (``core.analyze``).

Live: hold the control one way and press Mark A, the other way and press Mark B (each uses the
last couple of seconds). From the Log tool: pick two tagged timestamps. On a loaded log: type the
two time ranges. Then Label… a result to name it.
"""

import time

from ..core import analyze
from ..j1939.addresses import sa_name
from ..j1939.constants import pgn_label
from .qt import QtWidgets
from .widgets import item, mono_font, setup_table

HEAD = ["ID", "PGN", "Module", "Where", "A", "B", "Score"]


class _Window(QtWidgets.QWidget):
    """One time range: from / to (seconds from the first buffered frame), Mark now, or a tag."""

    def __init__(self, title: str, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QtWidgets.QLabel(f"<b>{title}</b>"))
        self.t0 = QtWidgets.QDoubleSpinBox()
        self.t1 = QtWidgets.QDoubleSpinBox()
        for w in (self.t0, self.t1):
            w.setDecimals(1)
            w.setRange(0, 1e7)
            w.setSuffix(" s")
        lay.addWidget(self.t0)
        lay.addWidget(QtWidgets.QLabel("to"))
        lay.addWidget(self.t1)
        self.mark = QtWidgets.QPushButton("Mark now")
        self.mark.setToolTip(f"Use the last {analyze.MARK_SECONDS:g} s of traffic")
        self.mark.clicked.connect(self.mark_now)
        lay.addWidget(self.mark)
        self.tag = QtWidgets.QComboBox()
        self.tag.addItem("From a Log tool timestamp…", None)
        self.tag.currentIndexChanged.connect(self._tag_picked)
        lay.addWidget(self.tag, 1)

    def set_range(self, a: float, b: float):
        base = self.owner.base()
        self.t0.setValue(max(0.0, a - base))
        self.t1.setValue(max(0.0, b - base))

    def mark_now(self):
        now = self.owner.now()
        self.set_range(now - analyze.MARK_SECONDS, now)
        self.owner.find()

    def _tag_picked(self, _i):
        w = self.tag.currentData()
        if w:
            self.set_range(w[0], w[1])

    def range(self) -> tuple[float, float]:
        base = self.owner.base()
        return base + self.t0.value(), base + self.t1.value()


class ChangesDialog(QtWidgets.QDialog):
    def __init__(self, ctx, label_cb, parent=None):
        """``label_cb(candidate)`` opens the label dialog for a result."""
        super().__init__(parent)
        self.ctx, self.label_cb = ctx, label_cb
        self.results: list[analyze.Candidate] = []
        self._base: float | None = None
        self.setWindowTitle("Find what changed")
        self.resize(1000, 580)
        lay = QtWidgets.QVBoxLayout(self)
        help_ = QtWidgets.QLabel(
            "Hold the control (pedal, switch, lever) one way and press <b>Mark now</b> on A, then the "
            "other way and press <b>Mark now</b> on B. Or pick two Log tool timestamps, or type two "
            "time ranges. The data that differs is listed, most likely first: it only suggests where "
            "to look.")
        help_.setWordWrap(True)
        lay.addWidget(help_)
        self.a = _Window("A", self)
        self.b = _Window("B", self)
        lay.addWidget(self.a)
        lay.addWidget(self.b)
        bar = QtWidgets.QHBoxLayout()
        go = QtWidgets.QPushButton("Find")
        go.clicked.connect(self.find)
        bar.addWidget(go)
        self.summary = QtWidgets.QLabel("")
        bar.addWidget(self.summary, 1)
        lay.addLayout(bar)
        self.table = QtWidgets.QTableWidget()
        self.table.setFont(mono_font())
        setup_table(self.table, HEAD)
        self.table.itemDoubleClicked.connect(lambda *_: self.label_selected())
        lay.addWidget(self.table, 1)
        row = QtWidgets.QHBoxLayout()
        self.label_btn = QtWidgets.QPushButton("Label this…")
        self.label_btn.clicked.connect(self.label_selected)
        row.addWidget(self.label_btn)
        row.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.table.itemSelectionChanged.connect(self._selection)
        self._fill_tags()
        live = ctx.live
        self.a.mark.setEnabled(live)
        self.b.mark.setEnabled(live)
        if not live:
            end = self.now() - self.base()
            self.a.t1.setValue(min(analyze.MARK_SECONDS, end))
            self.b.t0.setValue(max(0.0, end - analyze.MARK_SECONDS))
            self.b.t1.setValue(end)
        self._selection()

    def _frames(self) -> list:
        s = self.ctx.session
        if s is None:
            return []
        with s.lock:
            return list(s.raw)

    def base(self) -> float:
        """Time 0 of the A / B boxes: the first buffered frame when the dialog opened (fixed, so
        a full buffer dropping old frames doesn't shift A while you mark B)."""
        if self._base is None:
            s = self.ctx.session
            if s is None:
                return 0.0
            with s.lock:
                self._base = s.raw[0].timestamp if s.raw else time.time()
        return self._base

    def now(self) -> float:
        s = self.ctx.session
        if s is not None and s.offline:
            with s.lock:
                return s.raw[-1].timestamp if s.raw else 0.0
        return time.time()

    def _fill_tags(self):
        cap = self.ctx.capture
        tags = [(cap.title(i), m.t) for i, m in cap.tagged()] if cap is not None else []
        for title, t0, t1 in analyze.tag_windows(tags, end=self.now()):
            for w in (self.a, self.b):
                w.tag.addItem(title, (t0, t1))
        for w in (self.a, self.b):
            w.tag.setEnabled(w.tag.count() > 1)

    def find(self):
        a, b = self.a.range(), self.b.range()
        if a[1] <= a[0] or b[1] <= b[0]:
            self.summary.setText("Set both A and B first.")
            return
        self.results = analyze.find_changes(self._frames(), a, b)
        self.table.setRowCount(0)
        for c in self.results:
            r = self.table.rowCount()
            self.table.insertRow(r)
            pgn = c.pgn
            vals = [c.id_str, "" if pgn is None else f"{pgn:04X} {pgn_label(pgn)}",
                    "" if c.sa is None else sa_name(c.sa), c.where, c.before, c.after, f"{c.score:.0f}"]
            for col, v in enumerate(vals):
                self.table.setItem(r, col, item(v, align_right=col == 6))
        self.summary.setText(f"{len(self.results)} candidate(s)" if self.results else
                             "Nothing differs clearly between A and B. Hold each position for a "
                             "couple of seconds.")
        if self.results:
            self.table.selectRow(0)

    def selected(self) -> analyze.Candidate | None:
        r = self.table.currentRow()
        return self.results[r] if 0 <= r < len(self.results) else None

    def _selection(self):
        c = self.selected()
        self.label_btn.setEnabled(c is not None and c.extended)
        self.label_btn.setToolTip("" if c is None or c.extended else
                                  "11-bit IDs (CANopen and others) can't be labeled yet")

    def label_selected(self):
        c = self.selected()
        if c is not None and c.extended:
            self.label_cb(c.signal_draft())
