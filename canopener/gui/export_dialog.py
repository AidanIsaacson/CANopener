"""Export Logs: copy captures and reports to a USB drive (then eject it) or to a folder."""

import os
import shutil
import threading

from ..core import capture as cap
from ..core import config, storage
from .qt import Qt, QtCore, QtWidgets


def _size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return ""


class ExportDialog(QtWidgets.QDialog):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Export Logs")
        self.resize(620, 560)
        self._drives: list[storage.Drive] = []
        self._worker: threading.Thread | None = None
        self._progress = (0, 1)
        self._result = None
        lay = QtWidgets.QVBoxLayout(self)

        lay.addWidget(QtWidgets.QLabel("<b>1. What to export</b>"))
        self.items = QtWidgets.QListWidget()
        lay.addWidget(self.items, 1)
        il = QtWidgets.QHBoxLayout()
        self.show_all = QtWidgets.QCheckBox("Also show captures from earlier sessions")
        self.show_all.toggled.connect(self._fill_items)
        il.addWidget(self.show_all)
        il.addStretch(1)
        for text, state in (("All", True), ("None", False)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _=False, v=state: self._check_all(v))
            il.addWidget(b)
        lay.addLayout(il)

        lay.addWidget(QtWidgets.QLabel("<b>2. Where to</b>"))
        self.to_usb = QtWidgets.QRadioButton("USB drive")
        self.to_usb.setChecked(True)
        self.to_folder = QtWidgets.QRadioButton("Folder")
        dl = QtWidgets.QGridLayout()
        dl.addWidget(self.to_usb, 0, 0)
        self.drive = QtWidgets.QComboBox()
        dl.addWidget(self.drive, 0, 1)
        self.drive_hint = QtWidgets.QLabel()
        self.drive_hint.setStyleSheet("color: gray")
        dl.addWidget(self.drive_hint, 1, 1)
        self.eject = QtWidgets.QCheckBox("Eject the drive when done")
        self.eject.setChecked(True)
        dl.addWidget(self.eject, 2, 1)
        dl.addWidget(self.to_folder, 3, 0)
        fr = QtWidgets.QHBoxLayout()
        self.folder = QtWidgets.QLineEdit(os.path.expanduser("~"))
        fr.addWidget(self.folder, 1)
        browse = QtWidgets.QPushButton("Choose…")
        browse.clicked.connect(self._browse)
        fr.addWidget(browse)
        dl.addLayout(fr, 3, 1)
        dl.setColumnStretch(1, 1)
        lay.addLayout(dl)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        bl = QtWidgets.QHBoxLayout()
        bl.addStretch(1)
        self.export_btn = QtWidgets.QPushButton("Export")
        self.export_btn.setDefault(True)
        self.export_btn.setMinimumHeight(40)
        self.export_btn.setStyleSheet("font-weight: bold; padding: 4px 24px;")
        self.export_btn.clicked.connect(self._export)
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        bl.addWidget(self.export_btn)
        bl.addWidget(self.close_btn)
        lay.addLayout(bl)

        for w in (self.to_usb, self.to_folder, self.drive, self.folder):
            sig = w.toggled if isinstance(w, QtWidgets.QRadioButton) else \
                w.currentIndexChanged if isinstance(w, QtWidgets.QComboBox) else w.textChanged
            sig.connect(self._update_buttons)
        self.items.itemChanged.connect(self._update_buttons)

        self._fill_items()
        self._poll_drives()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(700)

    # ------------------------------------------------------------------
    def _candidates(self) -> list[str]:
        this_session = [p for p in self.ctx.saved_paths if os.path.exists(p)]
        if not self.show_all.isChecked():
            return this_session
        older = cap.saved_captures(config.logs_dir())
        reports = config.reports_dir()
        older += sorted((os.path.join(reports, f) for f in os.listdir(reports)), reverse=True) \
            if os.path.isdir(reports) else []
        return this_session + [p for p in older if p not in this_session]

    def _fill_items(self):
        self.items.blockSignals(True)
        self.items.clear()
        session = set(self.ctx.saved_paths)
        for path in self._candidates():
            kind = "capture" if os.path.isdir(path) else "file"
            try:
                size = _size(storage.export_size([path]))
            except OSError:
                continue
            it = QtWidgets.QListWidgetItem(f"{os.path.basename(path)}   ({kind}, {size})")
            it.setData(Qt.ItemDataRole.UserRole, path)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if path in session else Qt.CheckState.Unchecked)
            it.setToolTip(path)
            self.items.addItem(it)
        if not self.items.count():
            it = QtWidgets.QListWidgetItem("Nothing saved this session yet. Save a capture or report first, "
                                           "or tick 'earlier sessions'.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.items.addItem(it)
        self.items.blockSignals(False)
        self._update_buttons()

    def _check_all(self, on: bool):
        for i in range(self.items.count()):
            it = self.items.item(i)
            if it.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def selected(self) -> list[str]:
        out = []
        for i in range(self.items.count()):
            it = self.items.item(i)
            if it.checkState() == Qt.CheckState.Checked and it.data(Qt.ItemDataRole.UserRole):
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def _browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Export to folder", self.folder.text())
        if d:
            self.folder.setText(d)
            self.to_folder.setChecked(True)

    # ------------------------------------------------------------------
    def _poll_drives(self):
        drives = storage.removable_drives()
        if [d.device for d in drives] == [d.device for d in self._drives]:
            return
        new = [d for d in drives if d.device not in {x.device for x in self._drives}]
        current = self.drive.currentData()
        self._drives = drives
        self.drive.blockSignals(True)
        self.drive.clear()
        for d in drives:
            self.drive.addItem(d.describe(), d.device)
        # Select a stick the moment it's plugged in.
        pick = new[0].device if new else current
        idx = self.drive.findData(pick)
        self.drive.setCurrentIndex(max(idx, 0))
        self.drive.blockSignals(False)
        if new and self._worker is None:
            self.to_usb.setChecked(True)
        self._update_buttons()

    def _tick(self):
        if self._worker is None:
            self._poll_drives()
            return
        done, total = self._progress
        self.bar.setValue(int(done * 100 / max(total, 1)))
        if not self._worker.is_alive():
            self._worker = None
            self._finished()

    def _update_buttons(self, *_):
        busy = self._worker is not None
        if self.to_usb.isChecked():
            ok_dest = bool(self._drives)
            self.drive_hint.setText("" if self._drives else "Insert a USB drive… (it's picked up automatically)")
        else:
            ok_dest = bool(self.folder.text().strip())
            self.drive_hint.setText("")
        self.drive.setEnabled(self.to_usb.isChecked() and bool(self._drives))
        self.eject.setEnabled(self.to_usb.isChecked())
        self.export_btn.setEnabled(not busy and ok_dest and bool(self.selected()))
        self.close_btn.setEnabled(not busy)

    # ------------------------------------------------------------------
    def _export(self):
        items = self.selected()
        if self.to_usb.isChecked():
            drive = self._drives[self.drive.currentIndex()]
            dest = drive.path
        else:
            drive, dest = None, config.ensure_dir(os.path.expanduser(self.folder.text().strip()))
        try:
            need = storage.export_size(items)
            free = shutil.disk_usage(dest).free
        except OSError as e:
            self.status.setText(f"Can't write there: {e}")
            return
        if need > free:
            self.status.setText(f"Not enough space: need {_size(need)}, {_size(free)} free.")
            return
        self._drive_used = drive
        self._result = None
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.status.setText(f"Copying {_size(need)}…")

        def progress(done, total):
            self._progress = (done, total)

        def work():
            try:
                folder, n = storage.export_items(items, dest, progress)
                ejected = None
                if drive is not None and self.eject.isChecked():
                    ejected = storage.eject(drive)
                self._result = ("ok", folder, n, ejected)
            except OSError as e:
                self._result = ("error", str(e))

        self._worker = threading.Thread(target=work, name="export", daemon=True)
        self._worker.start()
        self._update_buttons()

    def _finished(self):
        r = self._result
        self.bar.setValue(100)
        if r is None or r[0] == "error":
            self.status.setText(f"<b style='color:#e03131'>Export failed:</b> {r[1] if r else 'unknown error'}")
        else:
            _, folder, n, ejected = r
            msg = f"<b style='color:#2f9e44'>Exported {n} file(s)</b> to {folder}."
            if ejected is not None:
                ok, why = ejected
                msg += (" The drive is ejected - <b>safe to remove</b>." if ok
                        else f"<br>Couldn't eject automatically ({why}). Eject it from the file manager "
                             "before pulling it out.")
            self.status.setText(msg)
            self._drives = []
            self._poll_drives()
        self._update_buttons()
