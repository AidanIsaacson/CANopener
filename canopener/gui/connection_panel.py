"""Connection setup: a sidebar (like the Log tool) with the source, interface, bitrate and
Connect controls, the adapter / bus lamps, and the signal finder.

The signal finder is for back-probing a harness: start it, go and move the probes, and it keeps
listening (listen-only, cycling bitrates if Auto) until it hears steady good traffic. Then it
plays a chime, locks the bitrate in and (optionally) connects. All of its logic is
``core.link.SignalFinder``; this is only the screen for it. The main window owns connecting
(``MainWindow.connect``) and wires the buttons here to it.
"""

import queue
import time

from ..can import pythoncan
from ..core import connection, interfaces, link, sound
from .qt import Qt, QtCore, QtWidgets, Signal
from .widgets import STATUS_COLORS, RefreshComboBox, StatusLamp

SOURCES = [("SocketCAN adapter", connection.SOCKETCAN), ("Simulator", connection.SIMULATOR),
           ("Simulate the selected profile", connection.PROFILE_SIM),
           ("OBD-II simulator (car)", connection.OBD_SIM), ("Replay log file", connection.REPLAY)]
AUTO_BITRATE = 0
HISTORY = 8
BIG = "font-size: 13pt; font-weight: bold; padding: 8px; border-radius: 6px; color: white;"
FIND_STYLE = BIG + "background: #2f9e44;"
STOP_STYLE = BIG + "background: #e03131;"
STATE_LEVEL = {"searching": "wait", "heard": "wait", "error": "wait", "no_adapter": "bad",
               "failed": "bad", "locked": "ok"}


class ConnectionPanel(QtWidgets.QDockWidget):
    finder_starting = Signal()          # about to take over the interface: disconnect first
    locked = Signal(object)             # the finder locked on: bitrate (None for vcan)
    sudoers_requested = Signal()        # offer the passwordless-access setup (main window does it)

    def __init__(self, settings: QtCore.QSettings, iface_popup, parent=None):
        super().__init__("Connection", parent)
        self.setObjectName("connection_panel")
        self.settings = settings
        self.finder: link.SignalFinder | None = None
        self._events: queue.Queue = queue.Queue()
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
                         | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self._build(iface_popup)
        self._poll = QtCore.QTimer(self)
        self._poll.timeout.connect(self._drain)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build(self, iface_popup):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 6)

        self.adapter_lamp = StatusLamp("Adapter", big=True)
        self.bus_lamp = StatusLamp("Bus", big=True)
        lay.addWidget(self.adapter_lamp)
        lay.addWidget(self.bus_lamp)
        self.status_detail = QtWidgets.QLabel()
        self.status_detail.setWordWrap(True)
        self.status_detail.setStyleSheet("color: gray")
        lay.addWidget(self.status_detail)
        self.link_sound = QtWidgets.QCheckBox("Chime when the bus traffic drops out or comes back")
        self.link_sound.setToolTip("While connected: a falling tone when traffic stops (a probe slipped, key "
                                   "off), a rising one when it's back")
        self.link_sound.setChecked(self.settings.value("sound/link_cues", "true") in (True, "true"))
        self.link_sound.toggled.connect(lambda on: self.settings.setValue("sound/link_cues", on))
        lay.addWidget(self.link_sound)

        box = QtWidgets.QGroupBox("Connection")
        form = QtWidgets.QFormLayout(box)
        self.source = QtWidgets.QComboBox()
        for label, kind in SOURCES:
            self.source.addItem(label, kind)
        form.addRow("Source", self.source)

        # Lists only the interfaces that exist right now, re-checked each time the list opens.
        # Still editable, so a name can be typed (e.g. vcan0 before creating it).
        row = QtWidgets.QHBoxLayout()
        self.iface = RefreshComboBox(iface_popup)
        self.iface.setEditable(True)
        self.iface.setMinimumWidth(110)
        row.addWidget(self.iface, 1)
        self.rescan_btn = QtWidgets.QToolButton()
        self.rescan_btn.setText("⟳")
        self.rescan_btn.setToolTip("Look for CAN interfaces again")
        row.addWidget(self.rescan_btn)
        self.iface_label = QtWidgets.QLabel("Interface")
        form.addRow(self.iface_label, row)

        self.bitrate = QtWidgets.QComboBox()
        for br in interfaces.BITRATES:
            self.bitrate.addItem(f"{br // 1000}k", br)
        self.bitrate.addItem("Auto", AUTO_BITRATE)
        self.bitrate.setItemData(self.bitrate.count() - 1, "Listen at each bitrate (listen-only, so nothing "
                                 "is transmitted) until valid traffic is seen", Qt.ItemDataRole.ToolTipRole)
        self.bitrate_label = QtWidgets.QLabel("Bitrate")
        form.addRow(self.bitrate_label, self.bitrate)
        self.listen_only = QtWidgets.QCheckBox("Listen-only")
        self.listen_only.setToolTip("Transmit nothing at all. DM1 and live data still work (they're broadcast).")
        form.addRow(self.listen_only)

        btns = QtWidgets.QHBoxLayout()
        self.setup_btn = QtWidgets.QPushButton("Set Up Interface")
        self.setup_btn.setToolTip("Configure bitrate / listen-only and bring the interface up (asks for your password)")
        btns.addWidget(self.setup_btn)
        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.setMinimumWidth(100)
        self.connect_btn.setMinimumHeight(34)
        self.connect_btn.setStyleSheet("font-weight: bold;")
        btns.addWidget(self.connect_btn)
        form.addRow(btns)
        lay.addWidget(box)

        self.finder_box = QtWidgets.QGroupBox("Find signal (while placing probes)")
        fl = QtWidgets.QVBoxLayout(self.finder_box)
        intro = QtWidgets.QLabel("Keeps listening, listen-only, until it hears steady CAN traffic, "
                                 "then chimes and locks the bitrate in. Start it, then go move the probes.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray")
        fl.addWidget(intro)
        self.find_btn = QtWidgets.QPushButton()
        self.find_btn.setMinimumHeight(52)
        self.find_btn.clicked.connect(self.toggle_finder)
        fl.addWidget(self.find_btn)
        self.finder_state = QtWidgets.QLabel()
        self.finder_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.finder_state.setWordWrap(True)
        fl.addWidget(self.finder_state)
        self.finder_detail = QtWidgets.QLabel()
        self.finder_detail.setWordWrap(True)
        fl.addWidget(self.finder_detail)
        self.history = QtWidgets.QListWidget()
        self.history.setMaximumHeight(130)
        self.history.setToolTip("Most recent attempt first")
        self.history.setWordWrap(True)
        fl.addWidget(self.history)
        self.connect_when_found = QtWidgets.QCheckBox("Connect when found")
        self.connect_when_found.setChecked(self.settings.value("finder/connect", "true") in (True, "true"))
        self.connect_when_found.toggled.connect(lambda on: self.settings.setValue("finder/connect", on))
        fl.addWidget(self.connect_when_found)
        self.sound = QtWidgets.QCheckBox("Chime when the signal finder locks on")
        self.sound.setChecked(self.settings.value("finder/sound", "true") in (True, "true"))
        self.sound.toggled.connect(lambda on: self.settings.setValue("finder/sound", on))
        fl.addWidget(self.sound)
        lay.addWidget(self.finder_box)
        lay.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setWidget(w)
        self.setWidget(scroll)
        self._show_finder_idle()

    # ------------------------------------------------------------------
    # status lamps
    # ------------------------------------------------------------------
    def show_status(self, adapter: link.Status, bus: link.Status):
        self.adapter_lamp.set_status(adapter.level, adapter.text, adapter.detail)
        self.bus_lamp.set_status(bus.level, bus.text, bus.detail)
        bad = [s.detail for s in (adapter, bus) if s.level in (link.BAD, link.WAIT, link.IDLE)]
        self.status_detail.setText("\n".join(bad))

    def cue(self, name: str, link_status: bool = False):
        """Play a sound cue on the speaker and/or buzzer (Settings > Sound cues), if that kind is on:
        ``link_status`` cues (traffic lost / back while connected) have their own checkbox, the
        signal finder's lock-on chime another. Falls back to the system beep when nothing played."""
        if not (self.link_sound if link_status else self.sound).isChecked():
            return
        speaker = self.settings.value("sound/speaker", "true") in (True, "true")
        if not sound.play(name, speaker=speaker, buzzer=str(self.settings.value("sound/buzzer", sound.AUTO))):
            QtWidgets.QApplication.beep()

    # ------------------------------------------------------------------
    # signal finder
    # ------------------------------------------------------------------
    @property
    def finding(self) -> bool:
        return self.finder is not None

    def toggle_finder(self):
        if self.finding:
            self.stop_finder()
        else:
            self.start_finder()

    def start_finder(self, quiet_check=None) -> bool:
        """Start listening for good traffic. ``quiet_check(name)`` (default
        ``interfaces.can_configure_quietly``) says whether bitrate changes need a password."""
        if self.finding:
            return True
        if self.source.currentData() != connection.SOCKETCAN:
            self.source.setCurrentIndex(self.source.findData(connection.SOCKETCAN))
        name = self.iface.currentText().strip() or "can0"
        if pythoncan.is_spec(name):
            QtWidgets.QMessageBox.information(
                self, "Find signal", f"Find signal works with Linux CAN interfaces like can0. For {name} (a "
                "python-can adapter), pick the bitrate and press Connect.")
            return False
        if not interfaces.valid_name(name):
            QtWidgets.QMessageBox.critical(self, "Find signal", f"Bad interface name {name!r}")
            return False
        choice = self.bitrate.currentData()
        last = int(self.settings.value("finder/last_bitrate", 0) or 0) or None
        order = link.bitrate_order(last or interfaces.BITRATES[0]) if choice == AUTO_BITRATE else [choice]
        quiet_check = quiet_check or interfaces.can_configure_quietly
        if len(order) > 1 and not name.startswith("vcan") and not quiet_check(name):
            r = QtWidgets.QMessageBox.question(
                self, "Find signal",
                "Searching every bitrate changes the adapter's bitrate about once a second, and each "
                "change needs root. Without passwordless CAN access that's a password prompt every "
                "second.\n\nSet up passwordless CAN access now (one password prompt)? Or pick a "
                "bitrate instead of Auto and the finder only asks once.",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Yes)
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                return False
            self.sudoers_requested.emit()
            if not quiet_check(name):
                return False
        self.finder_starting.emit()
        self.history.clear()
        self.finder = link.SignalFinder(name, order, self._events.put, listen_only=self.listen_only.isChecked())
        self.finder.start()
        self._poll.start(100)
        self._set_running(True)
        self._show_state("wait", "SEARCHING…", f"Listening on {name} at "
                         + ("each bitrate" if len(order) > 1 else f"{order[0] // 1000}k") + ", listen-only.")
        return True

    def stop_finder(self):
        f, self.finder = self.finder, None
        if f is None:
            return
        f.stop()
        self._poll.stop()
        self._drain_quietly()
        self._set_running(False)
        self._show_state("off", "Stopped", "Press Find signal to start again.")

    def _drain_quietly(self):
        while not self._events.empty():
            try:
                self._events.get_nowait()
            except queue.Empty:
                break

    def _drain(self):
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                return
            self.handle_event(ev)

    def handle_event(self, ev: link.FinderEvent):
        if self.finder is None:
            return                          # stopped meanwhile
        stamp = time.strftime("%H:%M:%S")
        if ev.state != "heard":
            self.history.insertItem(0, f"{stamp}  {ev.text}")
            while self.history.count() > HISTORY:
                self.history.takeItem(self.history.count() - 1)
        level = STATE_LEVEL.get(ev.state, "wait")
        if ev.state == "locked":
            f, self.finder = self.finder, None
            f.stop()
            self._poll.stop()
            self._set_running(False)
            if ev.bitrate:
                self.bitrate.setCurrentIndex(max(self.bitrate.findData(ev.bitrate), 0))
                self.settings.setValue("finder/last_bitrate", ev.bitrate)
                self.settings.setValue("bitrate", ev.bitrate)
            self._show_state(level, "✔ LOCKED" + (f"  {ev.bitrate // 1000}k" if ev.bitrate else ""), ev.text)
            self.cue("found")
            self.locked.emit(ev.bitrate)
        elif ev.state == "failed":
            self.finder.stop()
            self.finder = None
            self._poll.stop()
            self._set_running(False)
            self._show_state(level, "STOPPED", ev.text + "\n\nIf it mentions listen-only, this adapter can't do "
                             "it; pick a bitrate by hand. If it mentions a password, set up passwordless "
                             "CAN access (Settings).")
        elif ev.state == "no_adapter":
            self._show_state(level, "NO ADAPTER", ev.text)
        elif ev.state == "heard":
            self._show_state("ok", "HEARD SOMETHING…", ev.text)
        else:
            self._show_state(level, "SEARCHING…", ev.text)

    def _set_running(self, on: bool):
        self.find_btn.setText("■  Stop finding" if on else "▶  Find signal")
        self.find_btn.setStyleSheet(STOP_STYLE if on else FIND_STYLE)
        for wdg in (self.source, self.iface, self.rescan_btn, self.bitrate, self.listen_only,
                    self.setup_btn, self.connect_btn):
            wdg.setEnabled(not on)

    def _show_finder_idle(self):
        self._set_running(False)
        if not self.finder_state.text():
            self._show_state("off", "Idle", "Pick the interface (and a bitrate, or Auto), then press Find signal.")

    def _show_state(self, level: str, title: str, detail: str):
        color = STATUS_COLORS.get(level, STATUS_COLORS["off"])
        self.finder_state.setText(title)
        self.finder_state.setStyleSheet(f"font-size: 18pt; font-weight: bold; padding: 10px; color: white; "
                                        f"background: {color}; border-radius: 6px;")
        self.finder_detail.setText(detail)
