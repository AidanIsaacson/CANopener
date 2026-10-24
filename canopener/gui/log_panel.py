"""The Log tool: a sidebar to plan, record, timestamp, edit and save captures.

Idle:       name the job, note the concern, plan timestamps (or load a template).
Recording:  Start/Stop and Timestamp keys (Enter / Space by default), number keys tag a
            specific timestamp, Backspace undoes the last tag.
Stopped:    relabel / delete / reorder / add notes, then Save, Save As or Discard.

All capture logic lives in ``canopener.core.capture``; this is only the screen for it.
"""

import os
import time

from ..core import capture as cap
from ..core import config, report
from ..core.capture import Capture, fmt_elapsed
from .hotkeys import typing
from .qt import Qt, QtCore, QtGui, QtWidgets, Signal

BIG = "font-size: 13pt; font-weight: bold; padding: 8px;"
REC_STYLE = BIG + "background: #e03131; color: white; border-radius: 6px;"
START_STYLE = BIG + "background: #2f9e44; color: white; border-radius: 6px;"
STAMP_STYLE = BIG + "background: #1c7ed6; color: white; border-radius: 6px;"
NEXT_BG = QtGui.QColor("#d0ebff")
COL_NUM, COL_NAME, COL_TIME, COL_NOTE = range(4)
FLASH_BG = QtGui.QColor("#b2f2bb")


class LogPanel(QtWidgets.QDockWidget):
    open_capture_log = Signal(str)       # ask the main window to decode a capture's log offline
    export_requested = Signal()
    keys_requested = Signal()
    recording_changed = Signal(bool)     # a capture started / stopped (OBD-II starts polling)

    def __init__(self, ctx, hotkeys, settings: QtCore.QSettings, meta_fn, parent=None):
        super().__init__("Log", parent)
        self.setObjectName("log_panel")
        self.ctx = ctx
        self.hotkeys = hotkeys
        self.settings = settings
        self.meta_fn = meta_fn              # () -> dict describing the connection, for capture.json
        self.status_extra = None            # () -> a line for the status while recording (e.g. OBD-II polling)
        self.extra_reports = None           # () -> {file name: text} saved with the capture
        self._flash: tuple[int, float] | None = None
        self._rebuilding = False
        plan = settings.value("log/plan")
        self.capture = Capture(plan=list(plan) if isinstance(plan, list) else
                               ([str(plan)] if isinstance(plan, str) and plan else []))
        self.ctx.capture = self.capture
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
                         | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self._build()
        self.hotkeys.triggered.connect(self.handle_action)
        ctx.session_changed.connect(self._session_changed)
        self._sync()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 6)

        self.state_label = QtWidgets.QLabel()
        self.state_label.setStyleSheet("font-size: 14pt; font-weight: bold;")
        lay.addWidget(self.state_label)
        self.detail_label = QtWidgets.QLabel()
        self.detail_label.setStyleSheet("color: gray")
        lay.addWidget(self.detail_label)

        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("Job / machine (e.g. 'Unit 14 - shuts down after 10 min')")
        self.name.textChanged.connect(self._name_changed)
        lay.addWidget(self.name)
        self.notes = QtWidgets.QPlainTextEdit()
        self.notes.setPlaceholderText("Customer concern / notes")
        self.notes.setMaximumHeight(70)
        self.notes.textChanged.connect(self._notes_changed)
        lay.addWidget(self.notes)

        tl = QtWidgets.QHBoxLayout()
        self.templates = QtWidgets.QComboBox()
        self.templates.setToolTip("Saved timestamp lists")
        tl.addWidget(self.templates, 1)
        self.load_tpl = QtWidgets.QPushButton("Load")
        self.load_tpl.clicked.connect(self._load_template)
        tl.addWidget(self.load_tpl)
        self.save_tpl = QtWidgets.QPushButton("Save as…")
        self.save_tpl.setToolTip("Save this timestamp list as a template")
        self.save_tpl.clicked.connect(self._save_template)
        tl.addWidget(self.save_tpl)
        self.del_tpl = QtWidgets.QToolButton()
        self.del_tpl.setText("✕")
        self.del_tpl.setToolTip("Delete the selected template (built-in ones can't be deleted)")
        self.del_tpl.clicked.connect(self._delete_template)
        tl.addWidget(self.del_tpl)
        lay.addLayout(tl)
        self._fill_templates()

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Timestamp", "Time", "Note"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_NUM, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_NAME, QtWidgets.QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_TIME, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_NOTE, QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(COL_NOTE, 90)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked
                                   | QtWidgets.QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.itemChanged.connect(self._item_changed)
        lay.addWidget(self.table, 1)

        rl = QtWidgets.QHBoxLayout()
        self.row_buttons = {}
        for key, text, tip, fn in (
            ("add", "+", "Add a timestamp to the list and type its name",
             lambda: self._add_planned(name_it=not self.recording)),
            ("del", "−", "Delete the selected timestamp", self._delete_row),
            ("up", "↑", "Move up", lambda: self._move(-1)),
            ("down", "↓", "Move down", lambda: self._move(1)),
            ("untag", "Clear time", "Remove the time from the selected timestamp (keeps it in the plan)",
             self._untag_row),
        ):
            b = QtWidgets.QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            rl.addWidget(b)
            self.row_buttons[key] = b
        rl.addStretch(1)
        lay.addLayout(rl)

        self.start_btn = QtWidgets.QPushButton()
        self.start_btn.setMinimumHeight(58)
        self.start_btn.clicked.connect(lambda: self.handle_action("start_stop"))
        lay.addWidget(self.start_btn)
        self.stamp_btn = QtWidgets.QPushButton()
        self.stamp_btn.setMinimumHeight(58)
        self.stamp_btn.clicked.connect(lambda: self.handle_action("timestamp"))
        lay.addWidget(self.stamp_btn)
        self.undo_btn = QtWidgets.QPushButton()
        self.undo_btn.clicked.connect(lambda: self.handle_action("undo"))
        lay.addWidget(self.undo_btn)

        self.save_row = QtWidgets.QWidget()
        sl = QtWidgets.QHBoxLayout(self.save_row)
        sl.setContentsMargins(0, 0, 0, 0)
        self.save_btn = QtWidgets.QPushButton("Save")
        self.save_btn.setStyleSheet("font-weight: bold; padding: 6px;")
        self.save_btn.clicked.connect(lambda: self.save())
        self.save_as_btn = QtWidgets.QPushButton("Save As…")
        self.save_as_btn.clicked.connect(self.save_as)
        self.discard_btn = QtWidgets.QPushButton("Discard")
        self.discard_btn.clicked.connect(self.discard)
        for b in (self.save_btn, self.save_as_btn, self.discard_btn):
            sl.addWidget(b)
        lay.addWidget(self.save_row)

        self.saved_row = QtWidgets.QWidget()
        svl = QtWidgets.QHBoxLayout(self.saved_row)
        svl.setContentsMargins(0, 0, 0, 0)
        open_btn = QtWidgets.QPushButton("Open folder")
        open_btn.clicked.connect(self._open_folder)
        view_btn = QtWidgets.QPushButton("View data")
        view_btn.setToolTip("Open this capture's log in the other tabs (disconnects from the bus)")
        view_btn.clicked.connect(lambda: self.capture.log_path and self.open_capture_log.emit(self.capture.log_path))
        new_btn = QtWidgets.QPushButton("New capture")
        new_btn.clicked.connect(self.new_capture)
        for b in (open_btn, view_btn, new_btn):
            svl.addWidget(b)
        lay.addWidget(self.saved_row)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        lay.addWidget(line)
        fl = QtWidgets.QHBoxLayout()
        self.folder_label = QtWidgets.QLabel()
        self.folder_label.setWordWrap(True)
        self.folder_label.setStyleSheet("color: gray")
        fl.addWidget(self.folder_label, 1)
        change = QtWidgets.QPushButton("Change…")
        change.setToolTip("Choose where captures and reports are saved")
        change.clicked.connect(self._change_folder)
        fl.addWidget(change)
        lay.addLayout(fl)

        bl = QtWidgets.QHBoxLayout()
        export = QtWidgets.QPushButton("⏏  Export Logs…")
        export.setStyleSheet("font-weight: bold; padding: 6px;")
        export.setToolTip("Copy this session's captures and reports to a USB drive or a folder")
        export.clicked.connect(self.export_requested.emit)
        bl.addWidget(export, 1)
        open_saved = QtWidgets.QPushButton("Open…")
        open_saved.setToolTip("Open a saved capture to view, relabel and re-save it")
        open_saved.clicked.connect(self.open_saved)
        bl.addWidget(open_saved)
        keys = QtWidgets.QPushButton("Keys…")
        keys.setToolTip("Change the Log tool keys")
        keys.clicked.connect(self.keys_requested.emit)
        bl.addWidget(keys)
        lay.addLayout(bl)
        self.keys_label = QtWidgets.QLabel()
        self.keys_label.setWordWrap(True)
        self.keys_label.setStyleSheet("color: gray; font-size: 9pt")
        lay.addWidget(self.keys_label)

        # Scrollable, so the panel never sets the window's minimum height: on a 1280x720 laptop the
        # whole window has ~650 px under the title bar and taskbar, and this panel alone wants ~530.
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(w)
        scroll.setMinimumWidth(w.minimumSizeHint().width()
                               + scroll.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ScrollBarExtent))
        self.setWidget(scroll)
        self.setMinimumWidth(330)

    # ------------------------------------------------------------------
    # actions (buttons and hotkeys both end up here)
    # ------------------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self.capture.state == cap.RECORDING

    def handle_action(self, action: str):
        c = self.capture
        if action == "start_stop":
            self.stop() if self.recording else self.start()
        elif action == "timestamp":
            if self.recording:
                self._tagged(c.tag_next())
            elif c.state == cap.IDLE:
                self._add_planned(name_it=True)
        elif action == "add_timestamp":
            if c.state == cap.SAVED:
                self._message("This capture is saved - press New capture to plan the next one")
            else:
                # While recording, just add it: typing a name would pause the other keys.
                self._add_planned(name_it=not self.recording)
        elif action == "undo":
            if self.recording and c.undo_last() is not None:
                self._sync()
        elif action.startswith("tag_") and self.recording:
            i = c.tag(int(action[4:]) - 1)
            if i is None:
                self._message(f"There's no timestamp {action[4:]} in the list")
            else:
                self._tagged(i)

    def _tagged(self, index: int):
        self._flash = (index, time.monotonic())
        self._sync()
        self._message(f"Tagged {self.capture.title(index)} at "
                      f"{fmt_elapsed(self.capture.markers[index].t - self.capture.started)}")

    def start(self):
        s = self.ctx.session
        if s is None or s.offline:
            self._message("Connect to a bus (or the simulator) first")
            return
        if self.capture.state == cap.STOPPED:
            # A fresh start while the last capture is unsaved: keep it (it can still be
            # reopened and edited) rather than make anyone deal with a dialog mid-job.
            folder = self.save(quiet=True)
            if folder is None:
                return
        if self.capture.state == cap.SAVED:
            self.new_capture()
        try:
            self.capture.start(s, config.logs_dir(), self.meta_fn())
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Start capture", str(e))
            return
        self.settings.setValue("log/plan", self.capture.plan_labels())
        self.start_btn.setFocus()               # out of any text box, so the keys work
        self._sync()
        self._message("Recording")
        self.recording_changed.emit(True)

    def stop(self):
        if not self.recording:
            return
        self.capture.stop()
        self.recording_changed.emit(False)
        self._sync()
        self._message("Stopped - edit the timestamps if needed, then Save")

    def save(self, parent_dir: str | None = None, quiet: bool = False) -> str | None:
        c = self.capture
        if c.state not in (cap.STOPPED, cap.SAVED):
            return None
        extra = {}
        res = self.ctx.result
        if res is not None and not res.offline and c.started and res.finished >= c.started:
            extra["report.txt"] = report.build_report(res, self.ctx.session, full=True, system=self.ctx.units)
        if self.extra_reports is not None:
            extra.update(self.extra_reports())
        dlg = QtWidgets.QProgressDialog("Writing summary and live-data spreadsheet…", None, 0, 100, self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(400)

        def progress(i, n):
            dlg.setValue(int(i * 100 / max(n, 1)))
            QtWidgets.QApplication.processEvents()

        try:
            folder = c.save(parent_dir, extra_files=extra, system=self.ctx.units, progress=progress)
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Save capture", str(e))
            return None
        finally:
            dlg.close()
        if folder not in self.ctx.saved_paths:
            self.ctx.saved_paths.append(folder)
        self._sync()
        self._message(f"Saved {os.path.basename(folder)}" + (" (start pressed)" if quiet else ""))
        return folder

    def save_as(self):
        start = self.capture.root or config.logs_dir()
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Save capture into folder", start)
        if d:
            self.save(d)

    def discard(self):
        if self.capture.state != cap.STOPPED:
            return
        if QtWidgets.QMessageBox.question(self, "Discard capture",
                                          "Delete this recording and its timestamps?") \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        plan = self.capture.fresh_copy()
        self.capture.discard()
        self._set_capture(plan)
        self._message("Capture discarded")

    def new_capture(self):
        self._set_capture(self.capture.fresh_copy())

    def _set_capture(self, c: Capture):
        self.capture = c
        self.ctx.capture = c
        self._flash = None
        self._sync()

    # ------------------------------------------------------------------
    # opening saved / recovering pending captures
    # ------------------------------------------------------------------
    def open_saved(self, folder: str | None = None):
        if self.recording:
            self._message("Stop the current capture first")
            return
        if self.capture.state == cap.STOPPED and not self.confirm_unsaved():
            return
        if folder is None:
            folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Open capture folder", config.logs_dir())
            if not folder:
                return
        try:
            c = Capture.load(folder)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.critical(self, "Open capture", f"Not a capture folder:\n{e}")
            return
        self._set_capture(c)
        self.show()

    def check_pending(self):
        """Offer to recover captures that were never saved (app closed or crashed)."""
        for folder in cap.pending_captures():
            try:
                c = Capture.load(folder)
            except (OSError, ValueError):
                continue
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.started or 0))
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("Unsaved capture")
            box.setText(f"Found an unsaved capture from {when}"
                        + (f" ({c.name})" if c.name else "") + f", {len(c.tagged())} timestamp(s).")
            recover = box.addButton("Recover", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Later", QtWidgets.QMessageBox.ButtonRole.RejectRole)
            delete = box.addButton("Delete", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
            box.exec()
            if box.clickedButton() is recover:
                self._set_capture(c)
                self.show()
                return
            if box.clickedButton() is delete:
                c.discard()

    def confirm_unsaved(self) -> bool:
        """Before closing / replacing an unsaved capture. False = cancel."""
        if self.recording:
            self.stop()
        if self.capture.state != cap.STOPPED:
            return True
        r = QtWidgets.QMessageBox.question(
            self, "Unsaved capture", "Save the capture you just recorded?",
            QtWidgets.QMessageBox.StandardButton.Save | QtWidgets.QMessageBox.StandardButton.Discard
            | QtWidgets.QMessageBox.StandardButton.Cancel, QtWidgets.QMessageBox.StandardButton.Save)
        if r == QtWidgets.QMessageBox.StandardButton.Save:
            return self.save() is not None
        if r == QtWidgets.QMessageBox.StandardButton.Discard:
            self.capture.discard()
            return True
        return False

    # ------------------------------------------------------------------
    # editing
    # ------------------------------------------------------------------
    def _selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def owns_name_editor(self, widget) -> bool:
        """True for the text box that opens when editing a timestamp's name (or note)."""
        return isinstance(widget, QtWidgets.QLineEdit) and self.table.isAncestorOf(widget)

    def _commit_editor(self):
        """Keep whatever is typed in an open name box (as Enter would)."""
        w = QtWidgets.QApplication.focusWidget()
        if self.owns_name_editor(w):
            delegate = self.table.itemDelegate()
            delegate.commitData.emit(w)
            delegate.closeEditor.emit(w, QtWidgets.QAbstractItemDelegate.EndEditHint.NoHint)

    def _add_planned(self, name_it: bool = False):
        """Add a timestamp to the list; with ``name_it`` its name box opens ready to type over."""
        self._commit_editor()
        i = self.capture.add_planned()
        self._sync()
        self.table.selectRow(i)
        if self.capture.state == cap.IDLE:
            self.settings.setValue("log/plan", self.capture.plan_labels())
        if name_it:
            item = self.table.item(i, COL_NAME)
            self.table.setCurrentItem(item)
            self.table.scrollToItem(item)
            self.table.editItem(item)           # the placeholder name starts selected: just type

    def _delete_row(self):
        i = self._selected_row()
        if i is not None:
            self.capture.delete(i)
            self._after_edit()

    def _move(self, delta):
        i = self._selected_row()
        if i is not None:
            self.capture.move(i, i + delta)
            self._after_edit()
            self.table.selectRow(max(0, min(i + delta, len(self.capture.markers) - 1)))

    def _untag_row(self):
        i = self._selected_row()
        if i is not None:
            self.capture.clear_tag(i)
            self._after_edit()

    def _after_edit(self):
        if self.capture.state == cap.IDLE:
            self.settings.setValue("log/plan", self.capture.plan_labels())
        self._sync()

    def _item_changed(self, it):
        if self._rebuilding:
            return
        row, col = it.row(), it.column()
        if row >= len(self.capture.markers):
            return
        m = self.capture.markers[row]
        if col == COL_NAME:
            label = it.text().strip()
            if not label:                       # emptied: keep the old name
                self._rebuilding = True
                it.setText(m.label)
                self._rebuilding = False
                return
            if label == m.label:
                return
            self.capture.rename(row, label)
        elif col == COL_NOTE:
            if it.text() == m.note:
                return
            self.capture.set_note(row, it.text())
        else:
            return
        if self.capture.state == cap.IDLE:
            self.settings.setValue("log/plan", self.capture.plan_labels())
        # No table rebuild here: the cell already shows the edit, and Shift+Space may be about
        # to open the next name box. Just refresh what shows the name elsewhere.
        self._sync(table=False)

    def _name_changed(self, text):
        self.capture.name = text
        self.capture._changed()

    def _notes_changed(self):
        self.capture.notes = self.notes.toPlainText()
        self.capture._changed()

    # templates
    def _fill_templates(self):
        self.templates.clear()
        for name in cap.load_templates():
            self.templates.addItem(name)

    def _load_template(self):
        if self.capture.state != cap.IDLE:
            self._message("Templates load into a new capture (press New capture first)")
            return
        name = self.templates.currentText()
        labels = cap.load_templates().get(name)
        if labels is None:
            return
        if self.capture.markers and QtWidgets.QMessageBox.question(
                self, "Load template", f"Replace the current timestamp list with '{name}'?") \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.capture.markers = [cap.Marker(label) for label in labels]
        self._after_edit()

    def _save_template(self):
        labels = [m.label for m in self.capture.markers]
        if not labels:
            self._message("Add some timestamps first")
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "Save template", "Template name:",
                                                  text=self.capture.name or "")
        if ok and name.strip():
            cap.save_template(name.strip(), labels)
            self._fill_templates()
            self.templates.setCurrentText(name.strip())

    def _delete_template(self):
        name = self.templates.currentText()
        if cap.delete_template(name):
            self._fill_templates()
        else:
            self._message("Built-in templates can't be deleted")

    # folders
    def _change_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Save captures and reports in", config.data_dir())
        if d:
            config.set_data_dir(d)
            self.settings.setValue("data_dir", d)
            self._sync()

    def _open_folder(self):
        folder = self.capture.folder or config.logs_dir()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(config.ensure_dir(folder)))

    def _session_changed(self, session):
        if self.recording and self.capture._session is not session:
            self.stop()
            self._message("Disconnected - capture stopped")
        self._sync()

    def _message(self, text):
        self.ctx.status_message.emit(f"Log: {text}")

    # ------------------------------------------------------------------
    # display
    # ------------------------------------------------------------------
    def _sync(self, table: bool = True):
        """Bring every widget in line with the capture's state."""
        c = self.capture
        idle, rec, stopped, saved = (c.state == s for s in (cap.IDLE, cap.RECORDING, cap.STOPPED, cap.SAVED))
        live = self.ctx.session is not None and not self.ctx.session.offline
        for wdg, text in ((self.name, c.name), (self.notes, c.notes)):
            current = wdg.text() if isinstance(wdg, QtWidgets.QLineEdit) else wdg.toPlainText()
            if current != text:
                wdg.blockSignals(True)
                wdg.setText(text) if isinstance(wdg, QtWidgets.QLineEdit) else wdg.setPlainText(text)
                wdg.blockSignals(False)
        for b in (self.load_tpl, self.templates):
            b.setEnabled(idle)

        hk = self.hotkeys.label
        if rec:
            self.start_btn.setText(f"■  STOP   ({hk('start_stop')})")
            self.start_btn.setStyleSheet(REC_STYLE)
            self.stamp_btn.setText(f"⏱  TIMESTAMP   ({hk('timestamp')})\nnext: {c.next_title()}")
        else:
            label = "START NEW" if stopped or saved else "START"
            self.start_btn.setText(f"●  {label}   ({hk('start_stop')})")
            self.start_btn.setStyleSheet(START_STYLE if live else BIG)
            self.stamp_btn.setText(f"+  ADD TIMESTAMP   ({hk('timestamp')})" if idle else "⏱  TIMESTAMP")
        self.stamp_btn.setStyleSheet(STAMP_STYLE if (rec or idle) else BIG)
        self.start_btn.setEnabled(live or rec)
        self.stamp_btn.setEnabled(rec or idle)
        self.undo_btn.setText(f"↶  Undo last timestamp   ({hk('undo')})")
        self.undo_btn.setVisible(rec)
        self.save_row.setVisible(stopped)
        self.saved_row.setVisible(saved)

        if table:
            self._rebuild_table()
        self._update_status()
        self.folder_label.setText(f"Saving to: {config.logs_dir()}")
        self._sync_keys_label()

    def _sync_keys_label(self):
        hk = self.hotkeys.label
        if getattr(self, "_paused_shown", False):
            if self.owns_name_editor(QtWidgets.QApplication.focusWidget()):
                text = (f"Naming a timestamp: type, then {hk('add_timestamp')} for the next one, "
                        "Enter to finish, Esc to cancel")
            else:
                text = "Keys paused while typing - press Esc (or click outside the box) to resume"
        else:
            nums = f"{hk('tag_1')}–{hk('tag_10')}" if hk("tag_1") == "1" and hk("tag_10") == "0" else "number keys"
            text = (f"Keys (while this panel is open): {hk('start_stop')} start/stop · "
                    f"{hk('timestamp')} timestamp · {nums} tag that timestamp · "
                    f"{hk('undo')} undo · {hk('add_timestamp')} add + name a timestamp")
        if self.keys_label.text() != text:
            self.keys_label.setText(text)

    def _rebuild_table(self):
        c = self.capture
        self._rebuilding = True
        t = self.table
        sel = self._selected_row()
        t.setRowCount(len(c.markers))
        nxt = c.next_index() if c.state in (cap.IDLE, cap.RECORDING) else None
        for i, m in enumerate(c.markers):
            num = QtWidgets.QTableWidgetItem(str(i + 1))
            num.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            title = QtWidgets.QTableWidgetItem(m.label)
            when = QtWidgets.QTableWidgetItem(
                fmt_elapsed(m.t - c.started) if m.tagged and c.started else ("next ▸" if i == nxt else ""))
            when.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            when.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            note = QtWidgets.QTableWidgetItem(m.note)
            for it in (num, title, when, note):
                if i == nxt and c.state == cap.RECORDING:
                    it.setBackground(NEXT_BG)
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                if not m.tagged and c.state in (cap.STOPPED, cap.SAVED):
                    it.setForeground(QtGui.QColor("gray"))
            t.setItem(i, COL_NUM, num)
            t.setItem(i, COL_NAME, title)
            t.setItem(i, COL_TIME, when)
            t.setItem(i, COL_NOTE, note)
        if sel is not None and sel < t.rowCount():
            t.selectRow(sel)
        if nxt is not None and c.state == cap.RECORDING:
            t.scrollToItem(t.item(nxt, COL_NAME))
        self._rebuilding = False

    def _update_status(self):
        c = self.capture
        if c.state == cap.RECORDING:
            self.state_label.setText(f"● REC  {fmt_elapsed(c.elapsed())[1:]}")
            self.state_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: #e03131;")
            extra = self.status_extra() if self.status_extra is not None else ""
            self.detail_label.setText(f"{c.recorded_frames:,} frames · {len(c.tagged())}/{len(c.markers)} tagged"
                                      + (f"\n{extra}" if extra else ""))
        elif c.state == cap.STOPPED:
            self.state_label.setText(f"Stopped ({fmt_elapsed(c.elapsed())[1:]}) - not saved yet")
            self.state_label.setStyleSheet("font-size: 12pt; font-weight: bold; color: #e8590c;")
            self.detail_label.setText("Edit names / notes (double-click), then Save")
        elif c.state == cap.SAVED:
            self.state_label.setText("Saved ✓")
            self.state_label.setStyleSheet("font-size: 12pt; font-weight: bold; color: #2f9e44;")
            self.detail_label.setText(os.path.basename(c.folder or ""))
        else:
            live = self.ctx.session is not None and not self.ctx.session.offline
            self.state_label.setText("Ready" if live else "Not connected")
            self.state_label.setStyleSheet("font-size: 12pt; font-weight: bold;")
            self.detail_label.setText("Plan your timestamps, then Start" if live
                                      else "Connect to a bus or the simulator to record")

    def refresh(self):
        """Called by the main window's timer."""
        if self.recording:
            self._update_status()
        paused = self.isVisible() and typing()
        if paused != getattr(self, "_paused_shown", None):
            self._paused_shown = paused
            self.keys_label.setStyleSheet("color: #e8590c; font-size: 9pt; font-weight: bold" if paused
                                          else "color: gray; font-size: 9pt")
        self._sync_keys_label()     # cheap; the focused box can change without anything else changing
        if self._flash:
            i, t0 = self._flash
            age = time.monotonic() - t0
            if age > 1.5 or i >= self.table.rowCount():
                self._flash = None
                self._rebuild_table()
            else:
                self._rebuilding = True      # recolouring a cell emits itemChanged; it isn't an edit
                for col in range(self.table.columnCount()):
                    it = self.table.item(i, col)
                    if it is not None:
                        it.setBackground(FLASH_BG)
                self._rebuilding = False

