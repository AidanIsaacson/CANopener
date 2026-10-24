"""Help > Check for updates: see what's new on GitHub, install it, restart. The app checks only
when asked, or at startup if Help > Check for updates at startup is ticked (off by default).

The work is ``core.updates`` (git fetch / pull --ff-only, pip only when dependencies changed),
run on a worker thread so the window stays responsive. An update that also changes the system
setup (drivers, rules) says so and gives the installer command to run once in a terminal.
"""

import threading

from ..core import updates
from .qt import Qt, QtWidgets, Signal


class UpdateDialog(QtWidgets.QDialog):
    _done = Signal(object)          # (callback, result) from the worker thread

    def __init__(self, restart, parent=None, status: updates.UpdateStatus | None = None):
        super().__init__(parent)
        self.restart = restart      # () -> None: close the app and start the new version
        self.status = status
        self.setWindowTitle("Check for updates")
        self.resize(560, 420)
        lay = QtWidgets.QVBoxLayout(self)
        self.headline = QtWidgets.QLabel()
        self.headline.setWordWrap(True)
        self.headline.setStyleSheet("font-size: 12pt; font-weight: bold;")
        lay.addWidget(self.headline)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        lay.addWidget(self.details, 1)
        row = QtWidgets.QHBoxLayout()
        self.check_btn = QtWidgets.QPushButton("Check again")
        self.check_btn.clicked.connect(self.check)
        row.addWidget(self.check_btn)
        row.addStretch(1)
        self.install_btn = QtWidgets.QPushButton("Install update")
        self.install_btn.setProperty("big", True)
        self.install_btn.clicked.connect(self.install)
        row.addWidget(self.install_btn)
        self.restart_btn = QtWidgets.QPushButton("Restart now")
        self.restart_btn.setProperty("big", True)
        self.restart_btn.clicked.connect(self._restart)
        row.addWidget(self.restart_btn)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        lay.addLayout(row)
        self._done.connect(lambda cb_res: cb_res[0](cb_res[1]))
        self.restart_btn.hide()
        self._busy = False
        if status is not None:
            self._show_status(status)
        else:
            self.check()

    def _run(self, text: str, fn, done):
        self._busy = True
        self.headline.setText(text)
        self._update_buttons()
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)

        def work():
            try:
                result = fn()
            except Exception as e:          # noqa: BLE001 - shown in the dialog
                result = e
            self._done.emit((lambda r: self._finish(r, done), result))

        threading.Thread(target=work, name="update", daemon=True).start()

    def _finish(self, result, done):
        QtWidgets.QApplication.restoreOverrideCursor()
        self._busy = False
        if isinstance(result, Exception):
            self.headline.setText(f"Something went wrong: {result}")
            self._update_buttons()
            return
        done(result)

    def check(self):
        self._run("Checking GitHub for updates…", updates.check, self._show_status)

    def _show_status(self, s: updates.UpdateStatus):
        self.status = s
        self.headline.setText(s.message)
        lines = [f"Installed: CANopener {s.current}" + (f" (branch {s.branch})" if s.branch else "")]
        if s.latest and s.available:
            lines.append(f"Available: CANopener {s.latest}")
        if s.commits:
            lines += ["", "What's new:"] + [f"  • {c}" for c in s.commits]
        if s.dirty:
            lines += ["", "Files changed on this computer (an update that touches them will stop, "
                          "leaving them as they are):"] + [f"  {f}" for f in s.dirty]
        if s.available and s.installer_changed:
            lines += ["", "This update also changes the system setup (drivers, rules or permissions): after "
                          "installing, run the installer once in a terminal:",
                      f"  {updates.installer_command()}"]
        self.details.setPlainText("\n".join(lines))
        self._update_buttons()

    def install(self):
        if self.status is None or not self.status.available:
            return
        self._run("Installing the update…", updates.apply, self._installed)

    def _installed(self, result):
        ok, msg = result
        self.headline.setText("Update installed." if ok else "The update didn't install.")
        self.details.setPlainText(msg)
        if ok:
            self.status = None
            self.restart_btn.show()
        self._update_buttons()

    def _update_buttons(self):
        can_install = (not self._busy and self.status is not None and self.status.available
                       and not self.status.ahead)
        self.install_btn.setVisible(self.restart_btn.isHidden())
        self.install_btn.setEnabled(can_install)
        self.check_btn.setEnabled(not self._busy)
        self.restart_btn.setEnabled(not self._busy)

    def _restart(self):
        self.accept()
        self.restart()
