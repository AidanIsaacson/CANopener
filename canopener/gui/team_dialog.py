"""Help > Team access: this computer's serial, joining with an invite link (or asking for access),
and what's installed. The work is ``core.team``, run on a worker thread so the window stays
responsive. Withdrawing access happens in the background check (``core.startup_check``), never
here: this dialog only shows the result."""

import threading

from ..core import install, team
from .qt import Qt, QtWidgets, Signal


class TeamDialog(QtWidgets.QDialog):
    _done = Signal(object)          # (callback, result) from the worker thread

    def __init__(self, restart, parent=None, server_hint: str = ""):
        super().__init__(parent)
        self.restart = restart      # () -> None: close the app and start it again
        self.setWindowTitle("Team access")
        self.resize(560, 460)
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel("Team access gives this computer the team plugin from your team's server, "
                                 "once whoever looks after it approves this computer.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("This computer:"))
        self.serial = QtWidgets.QLabel(install.serial())
        self.serial.setStyleSheet("font-size: 14pt; font-weight: bold;")
        self.serial.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.serial)
        copy = QtWidgets.QPushButton("Copy")
        copy.setToolTip("Copy this computer's serial, to send to whoever looks after team access")
        copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.serial.text()))
        row.addWidget(copy)
        row.addStretch(1)
        lay.addLayout(row)

        self.headline = QtWidgets.QLabel()
        self.headline.setWordWrap(True)
        self.headline.setStyleSheet("font-weight: bold;")
        lay.addWidget(self.headline)
        self.details = QtWidgets.QLabel()
        self.details.setWordWrap(True)
        lay.addWidget(self.details)

        # Not joined yet: an invite link, or a request.
        self.join_box = QtWidgets.QGroupBox("Join")
        form = QtWidgets.QFormLayout(self.join_box)
        self.link = QtWidgets.QLineEdit()
        self.link.setPlaceholderText("https://…/invite/XXXX-XXXX-XXXX")
        form.addRow("Invite link:", self.link)
        self.name = QtWidgets.QLineEdit(team.default_name())
        self.name.setToolTip("What this computer is called on the server, so it's easy to recognise")
        form.addRow("Name for this computer:", self.name)
        self.join_btn = QtWidgets.QPushButton("Join")
        self.join_btn.setProperty("big", True)
        self.join_btn.clicked.connect(self.join)
        form.addRow("", self.join_btn)
        form.addRow(QtWidgets.QLabel("No invite link? Ask for access instead (then read out the serial above):"))
        self.server = QtWidgets.QLineEdit(server_hint)
        self.server.setPlaceholderText("https://your-team-server")
        form.addRow("Server address:", self.server)
        self.request_btn = QtWidgets.QPushButton("Ask for access")
        self.request_btn.clicked.connect(self.ask)
        form.addRow("", self.request_btn)
        lay.addWidget(self.join_box)
        lay.addStretch(1)

        buttons = QtWidgets.QHBoxLayout()
        self.check_btn = QtWidgets.QPushButton("Check now")
        self.check_btn.clicked.connect(self.check)
        buttons.addWidget(self.check_btn)
        self.leave_btn = QtWidgets.QPushButton("Leave team access…")
        self.leave_btn.clicked.connect(self.leave)
        buttons.addWidget(self.leave_btn)
        buttons.addStretch(1)
        self.install_btn = QtWidgets.QPushButton("Install update")
        self.install_btn.setProperty("big", True)
        self.install_btn.clicked.connect(self.install_update)
        buttons.addWidget(self.install_btn)
        self.restart_btn = QtWidgets.QPushButton("Restart now")
        self.restart_btn.setProperty("big", True)
        self.restart_btn.clicked.connect(self._restart)
        buttons.addWidget(self.restart_btn)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        lay.addLayout(buttons)

        self._done.connect(lambda cb_res: cb_res[0](cb_res[1]))
        self._busy = False
        self._changed = False
        self.status = team.describe()
        self._show(self.status)

    # --- work on a thread ---
    def _run(self, text: str, fn, done=None):
        self._busy = True
        self.headline.setText(text)
        self._update_buttons()
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)

        def work():
            try:
                result = fn()
            except team.TeamError as e:
                result = team.Status(self.status.state, str(e), ok=False, version=self.status.version,
                                     name=self.status.name, server=self.status.server)
            except Exception as e:          # noqa: BLE001 - shown in the dialog
                result = team.Status(self.status.state, f"Something went wrong: {e}", ok=False)
            self._done.emit((done or self._show, result))

        threading.Thread(target=work, name="team", daemon=True).start()

    def _show(self, st: team.Status):
        if self._busy:
            QtWidgets.QApplication.restoreOverrideCursor()
            self._busy = False
        self.status = st
        self._changed |= st.changed
        headline = {team.NONE: "Not joined.", team.PENDING: "Waiting for approval.",
                    team.APPROVED: "Approved.", team.WITHDRAWN: "Access withdrawn."}.get(st.state, "")
        self.headline.setText(headline if st.ok else "That didn't work.")
        bits = [st.message]
        if st.server:
            bits.append(f"Server: {st.server}" + (f"   This computer is called: {st.name}" if st.name else ""))
        self.details.setText("\n\n".join(b for b in bits if b))
        self._update_buttons()

    def _update_buttons(self):
        st, busy = self.status, self._busy
        joined = st.state in (team.PENDING, team.APPROVED)
        self.join_box.setVisible(not joined)
        for b in (self.join_btn, self.request_btn, self.check_btn, self.leave_btn, self.install_btn):
            b.setEnabled(not busy)
        self.check_btn.setVisible(st.state != team.NONE)
        self.leave_btn.setVisible(st.state != team.NONE)
        self.install_btn.setVisible(bool(st.update))
        self.restart_btn.setVisible(self._changed)

    # --- actions ---
    def join(self):
        link, name = self.link.text().strip(), self.name.text().strip()
        if not link:
            self._show(team.Status(self.status.state, "Paste the invite link you were sent first.", ok=False))
            return
        self._run("Joining…", lambda: team.join(link, name))

    def ask(self):
        server, name = self.server.text().strip(), self.name.text().strip()
        self._run("Sending the request…", lambda: team.request(server, name))

    def check(self):
        self._run("Checking with the server…", lambda: team.check())

    def install_update(self):
        self._run("Installing the team plugin…", lambda: team.install_update())

    def leave(self):
        if QtWidgets.QMessageBox.question(
                self, "Leave team access",
                "Leave team access? The team plugin is removed from this computer, and joining again needs a "
                "new invite or approval.",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._run("Leaving…", lambda: team.leave())

    def _restart(self):
        self.accept()
        self.restart()
