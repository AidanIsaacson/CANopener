"""Profiles > My labels: the module names and signals in the user profile in use.

Edit or remove them, rename the profile, teach it to recognise this machine from the bus, export
it (to a USB stick, say) or delete it. Saving goes through ``core.labeling``.
"""

from ..core import canopen, labeling, profiles
from ..j1939.addresses import sa_name
from .qt import Qt, QtWidgets
from .widgets import item, setup_table


class LabelsDialog(QtWidgets.QDialog):
    def __init__(self, ctx, edit_signal, parent=None):
        """``edit_signal(pid, saved_dict, key)`` opens the label dialog on a saved signal."""
        super().__init__(parent)
        self.ctx, self.edit_signal = ctx, edit_signal
        self.setWindowTitle("My labels")
        self.resize(720, 560)
        lay = QtWidgets.QVBoxLayout(self)
        self.head = QtWidgets.QLabel("")
        self.head.setWordWrap(True)
        lay.addWidget(self.head)

        lay.addWidget(QtWidgets.QLabel("<b>Modules</b>"))
        self.modules = QtWidgets.QTableWidget()
        setup_table(self.modules, ["Address", "Your name"])
        lay.addWidget(self.modules, 1)
        mrow = QtWidgets.QHBoxLayout()
        self.mod_edit = QtWidgets.QPushButton("Rename…")
        self.mod_edit.clicked.connect(self._rename_module)
        self.mod_del = QtWidgets.QPushButton("Remove")
        self.mod_del.clicked.connect(self._remove_module)
        mrow.addWidget(self.mod_edit)
        mrow.addWidget(self.mod_del)
        mrow.addStretch(1)
        lay.addLayout(mrow)

        lay.addWidget(QtWidgets.QLabel("<b>Signals</b>"))
        self.signals = QtWidgets.QTableWidget()
        setup_table(self.signals, ["Name", "PGN / ID", "From", "Where", "Scale", "Unit", "Group"])
        self.signals.itemDoubleClicked.connect(lambda *_: self._edit_signal())
        lay.addWidget(self.signals, 2)
        srow = QtWidgets.QHBoxLayout()
        self.sig_edit = QtWidgets.QPushButton("Edit…")
        self.sig_edit.clicked.connect(self._edit_signal)
        self.sig_del = QtWidgets.QPushButton("Remove")
        self.sig_del.clicked.connect(self._remove_signal)
        srow.addWidget(self.sig_edit)
        srow.addWidget(self.sig_del)
        srow.addStretch(1)
        lay.addLayout(srow)

        prow = QtWidgets.QHBoxLayout()
        self.rename_btn = QtWidgets.QPushButton("Rename profile…")
        self.rename_btn.clicked.connect(self._rename_profile)
        self.detect_btn = QtWidgets.QPushButton("Recognise this machine")
        self.detect_btn.setToolTip("Remember the modules on the bus now, so next time CANopener offers this "
                                   "profile when it sees them")
        self.detect_btn.clicked.connect(self._detect)
        self.export_btn = QtWidgets.QPushButton("Export…")
        self.export_btn.clicked.connect(self._export)
        self.delete_btn = QtWidgets.QPushButton("Delete profile…")
        self.delete_btn.clicked.connect(self._delete_profile)
        for b in (self.rename_btn, self.detect_btn, self.export_btn, self.delete_btn):
            prow.addWidget(b)
        prow.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        prow.addWidget(close)
        lay.addLayout(prow)
        ctx.profile_changed.connect(self.fill)
        self.fill()

    @property
    def pid(self) -> str | None:
        p = profiles.active()
        return p.id if labeling.is_editable(p) else None

    def fill(self):
        p = profiles.active()
        pid = self.pid
        if pid is None:
            name = p.name if p else "no profile"
            self.head.setText(f"The profile in use (<b>{name}</b>) isn't one of yours, so it has no labels. "
                              "Name a module (Network tab) or label some data (Bus Monitor) and CANopener "
                              "makes you a profile built on it. Or use Profiles → Save profile as….")
            mods, sigs = {}, []
        else:
            base = f", built on <b>{profiles.find(p.extends).name}</b>" if p.extends and profiles.find(p.extends) \
                else ""
            self.head.setText(f"Your profile <b>{p.name}</b>{base}. File: {labeling.path_for(pid)}")
            try:
                mods, sigs = labeling.module_names(pid), labeling.own_signals(pid)
            except (profiles.ProfileError, KeyError, ValueError) as e:
                self.head.setText(f"Couldn't read {labeling.path_for(pid)}: {e}")
                mods, sigs = {}, []
        self.modules.setRowCount(0)
        for sa, name in sorted(mods.items()):
            r = self.modules.rowCount()
            self.modules.insertRow(r)
            label = f"Node {sa - canopen.NODE_SRC:02X} (CANopen)" if canopen.is_node(sa) else f"{sa:02X}  ({sa})"
            self.modules.setItem(r, 0, item(label, data=sa))
            self.modules.setItem(r, 1, item(name))
        self._sigs = sigs
        self.signals.setRowCount(0)
        for sig, _d in sigs:
            r = self.signals.rowCount()
            self.signals.insertRow(r)
            where = f"byte {sig.start}" + (f" bit {sig.bit}, {sig.bits} bit(s)" if sig.bits else
                                           f", {sig.length} byte(s)")
            if sig.can_id is not None:
                src = canopen.source_name(canopen.id_src(sig.can_id))
            else:
                src = "any" if sig.sa is None else (", ".join(f"{a:02X}" for a in sig.sa)
                                                    if isinstance(sig.sa, tuple) else sa_name(sig.sa))
            if sig.big_endian:
                where += ", high byte first"
            for c, v in enumerate((sig.name, sig.ref, src, where, f"{sig.scale:g}", sig.unit, sig.group)):
                self.signals.setItem(r, c, item(v))
        for b in (self.mod_edit, self.mod_del, self.sig_edit, self.sig_del, self.rename_btn, self.export_btn,
                  self.delete_btn):
            b.setEnabled(pid is not None)
        self.detect_btn.setEnabled(pid is not None and self.ctx.session is not None
                                   and bool(self.ctx.session.known_ecus() or self.ctx.session.has_canopen))

    def _saved(self):
        self.ctx.labels_saved.emit(self.pid or "")

    def _module(self) -> int | None:
        it = self.modules.item(self.modules.currentRow(), 0)
        return None if it is None else it.data(Qt.ItemDataRole.UserRole)

    def _rename_module(self):
        sa = self._module()
        if sa is None:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename module", f"Name for {canopen.source_name(sa)}:",
                                                  text=labeling.module_names(self.pid).get(sa, ""))
        if ok:
            labeling.set_module_name(self.pid, sa, name)
            self._saved()

    def _remove_module(self):
        sa = self._module()
        if sa is not None:
            labeling.set_module_name(self.pid, sa, "")
            self._saved()

    def _signal(self):
        r = self.signals.currentRow()
        return self._sigs[r] if 0 <= r < len(self._sigs) else None

    def _edit_signal(self):
        got = self._signal()
        if got:
            self.edit_signal(self.pid, got[1], got[0].key)

    def _remove_signal(self):
        got = self._signal()
        if got and QtWidgets.QMessageBox.question(self, "Remove label", f"Remove '{got[0].name}'?") == \
                QtWidgets.QMessageBox.StandardButton.Yes:
            labeling.delete_signal(self.pid, got[0].key)
            self._saved()

    def _rename_profile(self):
        p = profiles.active()
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename profile", "Profile name:", text=p.name)
        if ok and name.strip():
            labeling.rename(self.pid, name)
            self._saved()

    def _detect(self):
        s = self.ctx.session
        found = labeling.set_detect(self.pid, s.known_ecus(), [n.node for n in s.canopen_nodes()])
        QtWidgets.QMessageBox.information(
            self, "Recognise this machine",
            f"Saved {len(found)} module(s): " + ", ".join(canopen.source_name(a) for a in found)
            + ". Next time CANopener sees them it offers this profile.")
        self._saved()

    def _export(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Export profile to folder (e.g. a USB stick)")
        if folder:
            path = labeling.export_to(self.pid, folder)
            QtWidgets.QMessageBox.information(self, "Export profile", f"Saved {path}")

    def _delete_profile(self):
        p = profiles.active()
        if QtWidgets.QMessageBox.question(
                self, "Delete profile", f"Delete your profile '{p.name}' and all its labels? This can't be "
                "undone (export it first to keep a copy).",
                defaultButton=QtWidgets.QMessageBox.StandardButton.No) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        fallback = p.extends or "generic"
        labeling.delete_profile(p.id)
        self.ctx.labels_saved.emit(fallback)

