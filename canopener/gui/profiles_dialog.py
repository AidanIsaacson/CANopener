"""Profiles > Manage profiles: which installed profiles the picker offers, and in what order.

Untick a profile to hide it (from the picker and from machine detection) without uninstalling it,
e.g. a plugin's profiles you keep but don't use. Drag, or use Up / Down, to order the picker. The
choice is saved by ``core.profiles.save_list_prefs``.
"""

from ..core import profiles
from .qt import Qt, QtWidgets


class ProfilesDialog(QtWidgets.QDialog):
    def __init__(self, found: list, current: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage profiles")
        self.resize(560, 440)
        self._default = [p.id for p in found]
        self._by_id = {p.id: p for p in found}
        lay = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel("Ticked profiles appear in the profile picker, in this order (drag to reorder). "
                                "Unticked ones stay installed but aren't offered or auto-detected.")
        note.setWordWrap(True)
        lay.addWidget(note)

        row = QtWidgets.QHBoxLayout()
        self.list = QtWidgets.QListWidget()
        self.list.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        row.addWidget(self.list, 1)
        side = QtWidgets.QVBoxLayout()
        for text, fn in (("Up", lambda: self._move(-1)), ("Down", lambda: self._move(1)),
                         ("Enable all", lambda: self._check_all(True)), ("Reset order", self._reset)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            side.addWidget(b)
        side.addStretch(1)
        row.addLayout(side)
        lay.addLayout(row, 1)
        self.current_note = QtWidgets.QLabel("")
        self.current_note.setStyleSheet("color: gray")
        self.current_note.setWordWrap(True)
        lay.addWidget(self.current_note)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.current = current
        _, disabled = profiles.list_prefs()
        self._fill([p.id for p in profiles.arrange(found, include_disabled=True)], disabled)
        self.list.itemChanged.connect(lambda _: self._note())

    def _fill(self, order: list[str], disabled):
        self.list.clear()
        for pid in order:
            p = self._by_id[pid]
            it = QtWidgets.QListWidgetItem(f"{p.name}   ({profiles.origin(p)})")
            it.setData(Qt.ItemDataRole.UserRole, pid)
            it.setToolTip(p.description or p.path)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)      # drop between items, not onto them
            it.setCheckState(Qt.CheckState.Unchecked if pid in disabled else Qt.CheckState.Checked)
            self.list.addItem(it)
        self._note()

    def _move(self, delta: int):
        r = self.list.currentRow()
        to = r + delta
        if r < 0 or not 0 <= to < self.list.count():
            return
        it = self.list.takeItem(r)
        self.list.insertItem(to, it)
        self.list.setCurrentRow(to)

    def _check_all(self, on: bool):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _reset(self):
        self._fill(list(self._default), self.disabled())

    def _note(self):
        hidden = self.current and self.current in self.disabled()
        self.current_note.setText("The profile in use now is unticked: the picker switches to (none) when you "
                                  "press OK." if hidden else "")

    def order(self) -> list[str]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def disabled(self) -> set[str]:
        return {self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() != Qt.CheckState.Checked}

    def accept(self):
        profiles.save_list_prefs(self.order(), self.disabled())
        super().accept()
