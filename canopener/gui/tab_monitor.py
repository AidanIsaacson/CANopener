"""Bus Monitor tab: raw frames, scrolling or one row per ID with changed bytes highlighted. 11-bit
IDs show their CANopen meaning (TPDO1 node 05, Heartbeat ...)."""

import time

from ..can.frame import CanFrame
from ..canopen import cob
from ..core import canopen
from ..j1939.addresses import sa_name
from ..j1939.constants import pgn_label
from ..j1939.ids import parse_id
from .qt import Qt, QtCore, QtGui, QtWidgets
from .widgets import mono_font

MAX_SCROLL_ROWS = 5000
CHANGE_FADE = 1.5     # seconds a changed byte stays highlighted
_ROOT = QtCore.QModelIndex()


def _decode(fr: CanFrame):
    if fr.error:
        return None, None, None, None
    if not fr.extended:
        return None, None, None, None
    j = parse_id(fr.can_id)
    return j.priority, j.pgn, j.sa, j.da


def _labels(fr: CanFrame, pgn, sa) -> tuple[str, str]:
    """(Label, Src) columns: J1939 PGN / address names, or the CANopen meaning of an 11-bit ID."""
    if fr.extended:
        return ("" if pgn is None else pgn_label(pgn)), ("" if sa is None else sa_name(sa))
    node = cob.node_of(fr.can_id)
    return cob.label(fr.can_id), (canopen.source_name(canopen.node_src(node)) if node else "")


class ScrollModel(QtCore.QAbstractTableModel):
    HEAD = ["Time", "Dir", "ID", "Pri", "PGN", "Label", "Src", "Dst", "DLC", "Data", "ASCII"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[tuple] = []
        self.t0 = None

    def rowCount(self, parent=_ROOT):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=_ROOT):
        return len(self.HEAD)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEAD[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole:
            if row[1] == "ERR":
                return QtGui.QBrush(QtGui.QColor("#e03131"))
            if row[1] == "TX":
                return QtGui.QBrush(QtGui.QColor("#1c7ed6"))
        return None

    def add(self, frames: list[CanFrame]):
        if not frames:
            return
        if self.t0 is None:
            self.t0 = frames[0].timestamp
        new = []
        for fr in frames:
            pri, pgn, sa, da = _decode(fr)
            if fr.error:
                new.append((f"{fr.timestamp - self.t0:10.4f}", "ERR", "", "", "", fr.error, "", "", "",
                            fr.data.hex(" ").upper(), ""))
                continue
            label, src = _labels(fr, pgn, sa)
            new.append((
                f"{fr.timestamp - self.t0:10.4f}", "TX" if fr.tx else "RX", fr.id_str(),
                "" if pri is None else str(pri), "" if pgn is None else f"{pgn:05X}", label, src,
                "" if da is None or da == 0xFF else f"{da:02X}", str(fr.dlc), fr.data.hex(" ").upper(),
                "".join(chr(b) if 32 <= b < 127 else "." for b in fr.data),
            ))
        new = new[-MAX_SCROLL_ROWS:]
        overflow = len(self.rows) + len(new) - MAX_SCROLL_ROWS
        if overflow > 0:
            self.beginRemoveRows(QtCore.QModelIndex(), 0, overflow - 1)
            del self.rows[:overflow]
            self.endRemoveRows()
        first = len(self.rows)
        self.beginInsertRows(QtCore.QModelIndex(), first, first + len(new) - 1)
        self.rows.extend(new)
        self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.rows.clear()
        self.t0 = None
        self.endResetModel()


class _IdState:
    __slots__ = ("frame", "count", "first", "last", "changed", "period")

    def __init__(self, fr):
        self.frame = fr
        self.count = 1
        self.first = self.last = fr.timestamp
        self.changed = [0.0] * 8
        self.period = 0.0


class ByIdModel(QtCore.QAbstractTableModel):
    """One row per CAN ID (like cansniffer); byte cells flash when they change."""

    HEAD = ["ID", "PGN", "Label", "Src", "Count", "Period", "DLC"] + [f"B{i}" for i in range(8)] + ["ASCII"]
    BYTE0 = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ids: dict[tuple, _IdState] = {}
        self.order: list[tuple] = []
        self.wall_offset = 0.0        # map frame timestamps to wall clock (non-zero for logs)

    def rowCount(self, parent=_ROOT):
        return 0 if parent.isValid() else len(self.order)

    def columnCount(self, parent=_ROOT):
        return len(self.HEAD)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEAD[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        st = self.ids[self.order[index.row()]]
        fr, c = st.frame, index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if c >= self.BYTE0 and c < self.BYTE0 + 8:
                b = c - self.BYTE0
                return f"{fr.data[b]:02X}" if b < fr.dlc else ""
            pri, pgn, sa, da = _decode(fr)
            label, src = _labels(fr, pgn, sa)
            return [
                fr.id_str(), "" if pgn is None else f"{pgn:05X}", label, src, str(st.count),
                f"{st.period * 1000:.0f} ms" if st.period else "", str(fr.dlc),
            ][c] if c < self.BYTE0 else "".join(chr(b) if 32 <= b < 127 else "." for b in fr.data)
        if role == Qt.ItemDataRole.BackgroundRole and self.BYTE0 <= c < self.BYTE0 + 8:
            age = time.time() - (st.changed[c - self.BYTE0] + self.wall_offset)
            if 0 <= age < CHANGE_FADE:
                col = QtGui.QColor("#ffa94d")
                col.setAlpha(int(230 * (1 - age / CHANGE_FADE)) + 25)
                return QtGui.QBrush(col)
        if role == Qt.ItemDataRole.TextAlignmentRole and self.BYTE0 <= c < self.BYTE0 + 8:
            return int(Qt.AlignmentFlag.AlignCenter)
        return None

    def add(self, frames: list[CanFrame]):
        added = False
        for fr in frames:
            if fr.error:
                continue
            key = (fr.can_id, fr.extended)
            st = self.ids.get(key)
            if st is None:
                st = self.ids[key] = _IdState(fr)
                self.order.append(key)
                added = True
                continue
            old = st.frame.data
            for i in range(min(8, fr.dlc)):
                if i >= len(old) or old[i] != fr.data[i]:
                    st.changed[i] = fr.timestamp
            dt = fr.timestamp - st.last
            st.period = dt if not st.period else st.period * 0.9 + dt * 0.1
            st.count += 1
            st.last = fr.timestamp
            st.frame = fr
        if added:
            self.beginResetModel()
            self.order.sort()
            self.endResetModel()
        elif self.order:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.order) - 1, len(self.HEAD) - 1))

    def clear(self):
        self.beginResetModel()
        self.ids.clear()
        self.order.clear()
        self.endResetModel()


class _Filter(QtCore.QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""

    def set_text(self, text):
        self.text = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if not self.text:
            return True
        m = self.sourceModel()
        hay = " ".join(str(m.data(m.index(row, c, parent)) or "") for c in range(m.columnCount())).lower()
        return all(term in hay for term in self.text.split())


class MonitorTab(QtWidgets.QWidget):
    title = "Bus Monitor"

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.seq = 0
        lay = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["By ID (changes highlighted)", "Scrolling"])
        self.mode.currentIndexChanged.connect(self._mode_changed)
        bar.addWidget(self.mode)
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("Filter: words must all match, e.g. 'FECA'  '27 Proprietary'  'TX'")
        bar.addWidget(self.filter, 2)
        self.pause = QtWidgets.QPushButton("Pause")
        self.pause.setCheckable(True)
        bar.addWidget(self.pause)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self.clear)
        bar.addWidget(clear)
        self.follow = QtWidgets.QCheckBox("Auto-scroll")
        self.follow.setChecked(True)
        bar.addWidget(self.follow)
        self.label_btn = QtWidgets.QPushButton("Label…")
        self.label_btn.setToolTip("Name a value in the selected message (with a live preview); saved in "
                                  "your profile and shown in Live Data")
        self.label_btn.clicked.connect(self.label_selected)
        bar.addWidget(self.label_btn)
        changes = QtWidgets.QPushButton("Find changes…")
        changes.setToolTip("Compare two moments (pedal up / down, switch off / on) and list the data that moved")
        changes.clicked.connect(self.find_changes)
        bar.addWidget(changes)
        lay.addLayout(bar)

        self.by_id = ByIdModel(self)
        self.scroll = ScrollModel(self)
        self.proxy = _Filter(self)
        self.view = QtWidgets.QTableView()
        self.view.setFont(mono_font())
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(22)
        self.view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setModel(self.proxy)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        act = QtGui.QAction("Label this data…", self.view)
        act.triggered.connect(self.label_selected)
        self.view.addAction(act)
        self.view.doubleClicked.connect(lambda *_: self.label_selected())
        lay.addWidget(self.view)
        self.filter.textChanged.connect(self.proxy.set_text)

        self.info = QtWidgets.QLabel("")
        lay.addWidget(self.info)
        self._mode_changed(0)
        ctx.session_changed.connect(self._session_changed)
        ctx.profile_changed.connect(lambda: self.view.viewport().update())

    def _mode_changed(self, i):
        model = self.by_id if i == 0 else self.scroll
        self.proxy.setSourceModel(model)
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        if model is self.by_id:
            widths = [90, 60, 220, 190, 70, 70, 40] + [34] * 8 + [80]
        else:
            widths = [95, 40, 90, 35, 60, 200, 190, 40, 40, 200, 80]
        for c, w in enumerate(widths):
            self.view.setColumnWidth(c, w)
        self.follow.setEnabled(model is self.scroll)

    def selected_frame(self) -> CanFrame | None:
        """The latest frame of the selected row's ID (either view)."""
        idx = self.view.currentIndex()
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        if self.proxy.sourceModel() is self.by_id:
            return self.by_id.ids[self.by_id.order[src.row()]].frame
        text = self.scroll.rows[src.row()][2]
        if not text:
            return None
        st = self.by_id.ids.get((int(text, 16), len(text) == 8))
        return st.frame if st else None

    def label_selected(self):
        fr = self.selected_frame()
        if fr is None:
            QtWidgets.QMessageBox.information(self, "Label this data", "Select a message (a row) first.")
            return
        st = self.by_id.ids.get((fr.can_id, fr.extended))
        start = max(range(min(8, fr.dlc)), key=lambda i: st.changed[i]) if st and fr.dlc else 0
        if fr.extended:
            j = parse_id(fr.can_id)
            draft = {"pgn": j.pgn, "sa": j.sa, "start": start, "length": 1}
        else:                       # 11-bit: a CANopen PDO or a plain CAN message
            draft = {"id": fr.can_id, "start": start, "length": 1}
        from .label_dialog import open_label_dialog
        open_label_dialog(self.ctx, draft, self)

    def find_changes(self):
        from .changes_dialog import ChangesDialog
        from .label_dialog import open_label_dialog
        ChangesDialog(self.ctx, lambda draft: open_label_dialog(self.ctx, draft, self), self).exec()

    def _session_changed(self, session):
        self.clear()
        if session is not None and session.offline:
            frames = list(session.raw)
            self.seq = session.frame_count
            # Make change-highlighting meaningless for logs instead of wrongly "live".
            self.by_id.wall_offset = -1e12
            self.by_id.add(frames)
            self.scroll.add(frames[-MAX_SCROLL_ROWS:])
        else:
            self.by_id.wall_offset = 0.0

    def clear(self):
        s = self.ctx.session
        self.seq = s.frame_count if s else 0
        self.by_id.clear()
        self.scroll.clear()

    def refresh(self):
        s = self.ctx.session
        if s is None or s.offline:
            return
        self.seq, frames = s.frames_since(self.seq)
        if self.pause.isChecked() or not frames:
            if self.isVisible():
                self.view.viewport().update()   # keep the change-highlight fade running
            return
        self.by_id.add(frames)
        self.scroll.add(frames)
        if self.isVisible() and self.follow.isChecked() and self.proxy.sourceModel() is self.scroll:
            self.view.scrollToBottom()
        self.info.setText(f"{len(self.by_id.order)} IDs, {s.rx_count} frames received, {s.tx_count} sent, "
                          f"{s.health.rx_errors} error frames")
