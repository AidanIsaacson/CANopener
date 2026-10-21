"""Single-key controls for the Log tool, and the dialog that remaps them.

Keys only act while the Log tool is open, the main window is focused, no dialog is up, and
you're not typing in a text box (the one exception: the "add timestamp" key also works while
typing a timestamp's name, so you can name a whole list without touching the mouse). Held
keys don't auto-repeat. That makes plain keys like
Enter and Space safe to use as big "hardware" buttons, and the handheld's physical buttons
can later be wired up (gpio-key overlay) to send whichever keys are configured here.
"""

import time

from .qt import Qt, QtCore, QtGui, QtWidgets, Signal

ACTIONS: list[tuple[str, str, str]] = [
    ("start_stop", "Start / stop recording", "Return"),
    ("timestamp", "Timestamp: tag the next one (adds one to the plan when not recording)", "Space"),
    ("add_timestamp", "Add a timestamp and type its name (also works while naming one)", "Shift+Space"),
    ("undo", "Undo the last timestamp", "Backspace"),
] + [(f"tag_{n}", f"Tag timestamp {n}", str(n % 10)) for n in range(1, 11)]
ACTION_NAMES = {a: name for a, name, _ in ACTIONS}
DEFAULTS = {a: key for a, _, key in ACTIONS}
DEBOUNCE = {"start_stop": 0.8}      # seconds: a bumped key can't stop a recording it just started
WHILE_NAMING = {"add_timestamp"}    # actions allowed while typing in a timestamp's name box

_TEXT_INPUTS = (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit,
                QtWidgets.QAbstractSpinBox, QtWidgets.QKeySequenceEdit)
_MODIFIER_KEYS = {Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta,
                  Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock}


def key_text(event) -> str | None:
    """Normalised key name for a key event: numpad Enter = Return, numpad 1 = 1."""
    key = Qt.Key(event.key())
    if key in _MODIFIER_KEYS or key == Qt.Key.Key_unknown:
        return None
    if key == Qt.Key.Key_Enter:
        key = Qt.Key.Key_Return
    mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
    return QtGui.QKeySequence(QtCore.QKeyCombination(mods, key)).toString(
        QtGui.QKeySequence.SequenceFormat.PortableText)


def typing() -> bool:
    w = QtWidgets.QApplication.focusWidget()
    if isinstance(w, _TEXT_INPUTS):
        return not getattr(w, "isReadOnly", lambda: False)()
    return isinstance(w, QtWidgets.QComboBox) and w.isEditable()


class Hotkeys(QtCore.QObject):
    triggered = Signal(str)

    def __init__(self, settings: QtCore.QSettings, is_active, parent=None, naming_box=None):
        super().__init__(parent)
        self.settings = settings
        self.is_active = is_active
        self.naming_box = naming_box or (lambda widget: False)   # is this widget a timestamp-name editor?
        self.bindings = dict(DEFAULTS)
        self._last: dict[str, float] = {}
        self.load()

    def load(self):
        self.settings.beginGroup("hotkeys")
        for action in DEFAULTS:
            v = self.settings.value(action)
            if v is not None:
                self.bindings[action] = str(v)
        self.settings.endGroup()

    def save(self):
        self.settings.beginGroup("hotkeys")
        for action, key in self.bindings.items():
            self.settings.setValue(action, key)
        self.settings.endGroup()

    def key_for(self, action: str) -> str:
        return self.bindings.get(action, "")

    def label(self, action: str) -> str:
        """Short human name of the key bound to an action, e.g. 'Enter'."""
        k = self.key_for(action)
        return {"Return": "Enter", "Backspace": "Bksp", "": "unbound"}.get(k, k)

    def action_for(self, key: str) -> str | None:
        return next((a for a, k in self.bindings.items() if k and k == key), None)

    def eventFilter(self, obj, ev):
        if ev.type() != QtCore.QEvent.Type.KeyPress or ev.isAutoRepeat():
            return False
        if not self.is_active() or QtWidgets.QApplication.activeModalWidget() is not None:
            return False
        key = key_text(ev)
        action = self.action_for(key) if key else None
        if typing():
            focus = QtWidgets.QApplication.focusWidget()
            if self.naming_box(focus):
                if action not in WHILE_NAMING:
                    return False        # normal typing; Enter keeps the name, Esc cancels it
            else:
                if Qt.Key(ev.key()) == Qt.Key.Key_Escape:   # Esc leaves the text box: keys work again
                    focus.clearFocus()
                    return True
                return False
        if action is None:
            return False
        now = time.monotonic()
        if now - self._last.get(action, 0) < DEBOUNCE.get(action, 0):
            return True
        self._last[action] = now
        self.triggered.emit(action)
        return True


class KeybindDialog(QtWidgets.QDialog):
    """Remap the Log tool keys. Click a key box and press the new key."""

    def __init__(self, hotkeys: Hotkeys, parent=None):
        super().__init__(parent)
        self.hotkeys = hotkeys
        self.setWindowTitle("Log tool keys")
        self.resize(560, 520)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel(
            "These keys work while the <b>Log</b> tool is open and you're not typing in a text box "
            "(the <i>add a timestamp</i> key also works while naming one). "
            "Click a box and press the new key."))
        self.table = QtWidgets.QTableWidget(len(ACTIONS), 3)
        self.table.setHorizontalHeaderLabels(["Action", "Key", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 60)
        self.edits: dict[str, QtWidgets.QKeySequenceEdit] = {}
        for row, (action, name, _) in enumerate(ACTIONS):
            it = QtWidgets.QTableWidgetItem(name)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(row, 0, it)
            edit = QtWidgets.QKeySequenceEdit(QtGui.QKeySequence(hotkeys.key_for(action)))
            if hasattr(edit, "setMaximumSequenceLength"):        # Qt 6.5+
                edit.setMaximumSequenceLength(1)
            edit.editingFinished.connect(self._check)
            self.table.setCellWidget(row, 1, edit)
            clear = QtWidgets.QPushButton("Clear")
            clear.clicked.connect(lambda _=False, e=edit: (e.clear(), self._check()))
            self.table.setCellWidget(row, 2, clear)
            self.edits[action] = edit
        lay.addWidget(self.table)
        self.warning = QtWidgets.QLabel("")
        self.warning.setStyleSheet("color: #e03131")
        lay.addWidget(self.warning)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel
                                        | QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._defaults)
        lay.addWidget(bb)
        self.ok_button = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)

    def _key(self, action) -> str:
        seq = self.edits[action].keySequence()
        if seq.isEmpty():
            return ""
        return QtGui.QKeySequence(seq[0]).toString(QtGui.QKeySequence.SequenceFormat.PortableText)

    def current(self) -> dict[str, str]:
        return {a: self._key(a) for a in self.edits}

    def _check(self):
        keys = [k for k in self.current().values() if k]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        self.warning.setText(f"Used twice: {', '.join(dupes)}" if dupes else "")
        self.ok_button.setEnabled(not dupes)
        return not dupes

    def _defaults(self):
        for action, edit in self.edits.items():
            edit.setKeySequence(QtGui.QKeySequence(DEFAULTS[action]))
        self._check()

    def _accept(self):
        if not self._check():
            return
        self.hotkeys.bindings = self.current()
        self.hotkeys.save()
        self.accept()
