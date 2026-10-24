"""Reusable widgets: warning-lamp strip, connection status lamps, strip-chart plotter, table helpers."""

import os
import time
from collections import deque

from ..j1939.addresses import sa_name
from ..j1939.dtc import Lamps
from .flow import FlowLayout
from .qt import Qt, QtCore, QtGui, QtWidgets, Signal

MONO = QtGui.QFont("Monospace")
MONO.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)

LAMP_COLORS = {"MIL": "#f2a900", "Stop": "#e03131", "Warn": "#f2a900", "Protect": "#f2a900"}
MARKER_COLOR = "#c2255c"
SERIES_COLORS = ["#1c7ed6", "#e8590c", "#2f9e44", "#ae3ec9", "#c92a2a", "#0c8599", "#5c940d", "#862e9c"]


STATUS_COLORS = {"ok": "#2f9e44", "wait": "#f08c00", "bad": "#e03131", "off": "#868e96", "sim": "#1c7ed6",
                 "idle": "#748ffc"}


class StatusLamp(QtWidgets.QLabel):
    """A coloured dot and a few words (a ``core.link.Status``). Clickable."""

    clicked = Signal()

    def __init__(self, text: str = "", big: bool = False, parent=None):
        super().__init__(parent)
        self.big = big
        self.level = "off"
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_status("off", text)

    def set_status(self, level: str, text: str, detail: str = ""):
        color = STATUS_COLORS.get(level, STATUS_COLORS["off"])
        self.level = level
        size = "15pt" if self.big else "11pt"
        self.setText(f'<span style="color:{color}; font-size:{size}">●</span> <b>{text}</b>')
        self.setToolTip(detail or text)

    def mousePressEvent(self, ev):
        self.clicked.emit()
        super().mousePressEvent(ev)


class RefreshComboBox(QtWidgets.QComboBox):
    """A combo box that calls ``on_popup`` (to refresh its items) just before its list opens."""

    def __init__(self, on_popup, parent=None):
        super().__init__(parent)
        self._on_popup = on_popup

    def showPopup(self):
        self._on_popup()
        super().showPopup()


ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "canopener.svg")


def app_icon() -> QtGui.QIcon:
    return QtGui.QIcon(ICON_PATH)


def mono_font(size: int | None = None) -> QtGui.QFont:
    f = QtGui.QFont(MONO)
    if size:
        f.setPointSize(size)
    return f


FIT_MAX = 420           # px: a column auto-fits up to this (one long description can't take the table)


def setup_table(table: QtWidgets.QTableWidget, headers: list[str]):
    """The app's table look and column rule: columns fit their contents, packed to the left, and
    the last column takes the slack. Every column can be dragged wider / narrower; once someone
    does, that table stops auto-fitting so it doesn't undo them."""
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    hh = table.horizontalHeader()
    hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
    hh.setStretchLastSection(True)
    # Fit once the rows are in: a 0 ms timer runs after the code filling the table has finished.
    timer = QtCore.QTimer(table)
    timer.setSingleShot(True)
    timer.setInterval(0)
    timer.timeout.connect(lambda: fit_columns(table))
    table.model().rowsInserted.connect(lambda *_: timer.start())
    table.model().modelReset.connect(lambda *_: timer.start())

    def resized(index, _old, _new):
        # A drag with the mouse (not our own fitting, nor the stretching last column) is the user's call.
        if index < table.columnCount() - 1 and not table.property("fitting") and \
                QtWidgets.QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            table.setProperty("user_sized", True)
    hh.sectionResized.connect(resized)


def fit_columns(table: QtWidgets.QTableWidget, cap: int = FIT_MAX):
    """Size every column but the last (which stretches) to its contents, up to ``cap``. Does
    nothing once the user has resized a column of this table."""
    if table.property("user_sized"):
        return
    table.setProperty("fitting", True)
    try:
        for c in range(table.columnCount() - 1):
            if table.isColumnHidden(c):
                continue
            table.resizeColumnToContents(c)
            if table.columnWidth(c) > cap:
                table.setColumnWidth(c, cap)
    finally:
        table.setProperty("fitting", False)


def item(text, align_right: bool = False, data=None) -> QtWidgets.QTableWidgetItem:
    it = QtWidgets.QTableWidgetItem("" if text is None else str(text))
    if align_right:
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if data is not None:
        it.setData(Qt.ItemDataRole.UserRole, data)
    return it


class LampIndicator(QtWidgets.QWidget):
    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.lamp = None
        self.blink_on = True
        self.setMinimumSize(84, 26)
        self.setToolTip(name)

    def set_lamp(self, lamp):
        self.lamp = lamp
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = min(self.height() - 6, 18)
        rect = QtCore.QRectF(3, (self.height() - r) / 2, r, r)
        on = self.lamp is not None and self.lamp.on and (self.blink_on or not self.lamp.flashing)
        if self.lamp is None or self.lamp.status >= 2:
            color = QtGui.QColor("#adb5bd")
            color.setAlpha(90)
        elif on:
            color = QtGui.QColor(LAMP_COLORS[self.name])
        else:
            color = QtGui.QColor("#495057")
            color.setAlpha(70)
        p.setBrush(color)
        p.setPen(QtGui.QPen(QtGui.QColor("#343a40"), 1))
        p.drawEllipse(rect)
        p.setPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText))
        p.drawText(QtCore.QRectF(r + 8, 0, self.width() - r - 8, self.height()),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.name)


class LampPanel(QtWidgets.QFrame):
    """One row per ECU showing MIL / red stop / amber warning / protect lamps from DM1."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        # A flow layout so per-ECU lamp clusters wrap onto a second line on a narrow screen,
        # instead of forcing the whole window's minimum width wide (one row per ECU could reach
        # ~1100 px with a few ECUs, pushing the window off a small laptop screen).
        self._layout = FlowLayout(self, margin=2, spacing=6)
        sp = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Minimum)
        sp.setHeightForWidth(True)              # let the strip grow taller when clusters wrap
        self.setSizePolicy(sp)
        self._title = QtWidgets.QLabel("<b>Lamps</b>")
        self._layout.addWidget(self._title)
        self._empty = QtWidgets.QLabel("no DM1 received")
        self._empty.setStyleSheet("color: gray")
        self._layout.addWidget(self._empty)
        self._groups: dict[int, tuple[QtWidgets.QWidget, list[LampIndicator]]] = {}
        self._blink = True
        t = QtCore.QTimer(self)
        t.timeout.connect(self._toggle_blink)
        t.start(500)

    def _toggle_blink(self):
        self._blink = not self._blink
        for _, inds in self._groups.values():
            for ind in inds:
                if ind.lamp is not None and ind.lamp.flashing:
                    ind.blink_on = self._blink
                    ind.update()

    def set_lamps(self, lamps: dict[int, Lamps]):
        for sa in list(self._groups):
            if sa not in lamps:
                w, _ = self._groups.pop(sa)
                w.deleteLater()
        for sa, lp in sorted(lamps.items()):
            if sa not in self._groups:
                w = QtWidgets.QWidget()
                lay = QtWidgets.QHBoxLayout(w)
                lay.setContentsMargins(10, 0, 10, 0)
                lay.addWidget(QtWidgets.QLabel(sa_name(sa)))
                inds = [LampIndicator(n) for n, _ in lp.items()]
                for ind in inds:
                    lay.addWidget(ind)
                self._layout.addWidget(w)
                self._groups[sa] = (w, inds)
            for ind, (_, lamp) in zip(self._groups[sa][1], lp.items()):
                ind.set_lamp(lamp)
        self._empty.setVisible(not lamps)


class StripChart(QtWidgets.QWidget):
    """Stacked rolling plots, one lane per series, each with its own y-scale."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.window = 60.0
        self.series: dict[str, dict] = {}       # key -> {label, unit, points: deque}
        self.paused_at: float | None = None
        self.follow_data = False
        self.markers: list[tuple[float, str]] = []     # (time, "3 - drive") from the Log tool
        self.setMinimumHeight(120)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def set_series(self, keys_labels: list[tuple[str, str, str, int, deque]]):
        """[(key, label, unit, decimals, points)] - points is a shared deque of (t, value)."""
        self.series = {k: {"label": lbl, "unit": u, "decimals": nd, "points": pts}
                       for k, lbl, u, nd, pts in keys_labels}
        self.update()

    def set_window(self, seconds: float):
        self.window = seconds
        self.update()

    def set_paused(self, paused: bool):
        self.paused_at = time.time() if paused else None
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(QtGui.QPalette.ColorRole.Base))
        text_color = pal.color(QtGui.QPalette.ColorRole.Text)
        grid = QtGui.QColor(text_color)
        grid.setAlpha(40)
        if not self.series:
            p.setPen(text_color)
            p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter),
                       "Tick 'Plot' on signals above to chart them here")
            return
        data = {k: [(t, v) for t, v in list(s["points"]) if v is not None] for k, s in self.series.items()}
        if self.paused_at:
            now = self.paused_at
        elif self.follow_data:      # log files: the newest sample is "now"
            now = max((pts[-1][0] for pts in data.values() if pts), default=time.time())
        else:
            now = time.time()
        t_min = now - self.window
        n = len(self.series)
        lane_h = self.height() / n
        left, right = 8, 70
        fm = p.fontMetrics()
        for i, (key, s) in enumerate(self.series.items()):
            top = i * lane_h
            plot = QtCore.QRectF(left, top + fm.height() + 4, self.width() - left - right,
                                 lane_h - fm.height() - 10)
            pts = [(t, v) for t, v in data[key] if t_min <= t <= now]
            color = QtGui.QColor(SERIES_COLORS[i % len(SERIES_COLORS)])
            p.setPen(QtGui.QPen(grid, 1))
            p.drawRect(plot)
            for frac in (0.25, 0.5, 0.75):
                y = plot.top() + plot.height() * frac
                p.drawLine(QtCore.QPointF(plot.left(), y), QtCore.QPointF(plot.right(), y))
            cur = f"{pts[-1][1]:.{s['decimals']}f}" if pts else "n/a"
            p.setPen(color)
            p.drawText(QtCore.QPointF(left, top + fm.ascent() + 2), f"{s['label']}:  {cur} {s['unit']}")
            if len(pts) < 2:
                continue
            vals = [v for _, v in pts]
            lo, hi = min(vals), max(vals)
            if hi - lo < 1e-9:
                lo, hi = lo - 1, hi + 1
            pad = (hi - lo) * 0.08
            lo, hi = lo - pad, hi + pad
            path = QtGui.QPainterPath()
            for j, (t, v) in enumerate(pts):
                x = plot.left() + (t - t_min) / self.window * plot.width()
                y = plot.bottom() - (v - lo) / (hi - lo) * plot.height()
                if j == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            p.setPen(QtGui.QPen(color, 1.6))
            p.drawPath(path)
            p.setPen(text_color)
            p.drawText(QtCore.QPointF(plot.right() + 4, plot.top() + fm.ascent()), f"{hi:.4g}")
            p.drawText(QtCore.QPointF(plot.right() + 4, plot.bottom()), f"{lo:.4g}")
        self._draw_markers(p, now, t_min, left, right, fm)
        p.setPen(text_color)
        p.drawText(QtCore.QPointF(left, self.height() - 2),
                   f"last {int(self.window)} s" + ("  (paused)" if self.paused_at else ""))

    def _draw_markers(self, p, now, t_min, left, right, fm):
        """Capture timestamps as dashed vertical lines with their label."""
        width = self.width() - left - right
        pen = QtGui.QPen(QtGui.QColor(MARKER_COLOR), 1.2, Qt.PenStyle.DashLine)
        last_label_x = -1e9
        for t, title in sorted(self.markers):
            if not t_min <= t <= now:
                continue
            x = left + (t - t_min) / self.window * width
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(x, fm.height() + 4), QtCore.QPointF(x, self.height() - fm.height()))
            if x - last_label_x > fm.horizontalAdvance(title) + 6:    # don't pile labels on each other
                p.setPen(QtGui.QColor(MARKER_COLOR))
                p.drawText(QtCore.QPointF(x + 3, self.height() - fm.descent() - fm.height()), title)
                last_label_x = x
