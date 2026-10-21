"""Main window: connection toolbar, lamp strip, tabs, status bar."""

import datetime
import os
import time

from .. import __version__
from ..core import config, connection, interfaces, profiles, scan, storage, transmit, units
from ..core.connection import load_log_session, open_connection
from .context import AppContext
from .export_dialog import ExportDialog
from .hotkeys import Hotkeys, KeybindDialog
from .log_panel import LogPanel
from .qt import QT_API, Qt, QtCore, QtGui, QtWidgets
from .request_dialog import RequestDialog
from .settings_dialog import UnitsDialog
from .tab_codes import CodesTab
from .tab_info import InfoTab
from .tab_live import LiveTab
from .tab_monitor import MonitorTab
from .tab_network import NetworkTab
from .tab_obd import ObdTab
from .tab_report import ReportTab
from .transmit_dialog import TransmitDialog
from .widgets import LampPanel, app_icon

SOURCES = [("SocketCAN adapter", connection.SOCKETCAN), ("Simulator", connection.SIMULATOR),
           ("Simulate the selected profile", connection.PROFILE_SIM),
           ("OBD-II simulator (car)", connection.OBD_SIM), ("Replay log file", connection.REPLAY)]
REFRESH_MS = 200
AUTO_BITRATE = 0


class PluginApi:
    """What a plugin's ``register_gui(api)`` gets to work with (see ``canopener.plugins``)."""

    def __init__(self, window: "MainWindow", plugin):
        self.window = window
        self.ctx = window.ctx
        self.settings = window.settings
        self.plugin = plugin

    def add_tab(self, widget: QtWidgets.QWidget, title: str):
        self.window.tabs.addTab(widget, title)
        self.window.tab_list.append(widget)

    def menu(self, title: str) -> QtWidgets.QMenu:
        """A top-level menu by title (created on first use)."""
        for a in self.window.menuBar().actions():
            if a.menu() is not None and a.text().replace("&", "") == title.replace("&", ""):
                return a.menu()
        return self.window.menuBar().addMenu(title)

    def add_action(self, menu_title: str, text: str, fn, shortcut: str | None = None):
        return self.window._action(self.menu(menu_title), text, fn, shortcut)

    def add_refresh(self, fn):
        """Call ``fn()`` on every UI tick (5 times a second)."""
        self.window._plugin_refresh.append(fn)

    def register_transmit_action(self, action):
        """Add a named transmit action (a ``core.transmit.TransmitAction``) to the Transmit a frame
        window. It appears behind the same confirm-first gate as the built-in actions: nothing is
        sent unless the user turns transmit mode on and confirms each frame. This is how a plugin
        contributes OEM/active commands without them living in this public repo."""
        transmit.register_action(action)

    def status(self, text: str, ms: int = 6000):
        self.window.statusBar().showMessage(text, ms)

    def confirm(self, title: str, text: str) -> bool:
        """Yes/No dialog, defaulting to No. Use before transmitting anything that changes a machine."""
        r = QtWidgets.QMessageBox.warning(self.window, title, text,
                                          QtWidgets.QMessageBox.StandardButton.Yes
                                          | QtWidgets.QMessageBox.StandardButton.No,
                                          QtWidgets.QMessageBox.StandardButton.No)
        return r == QtWidgets.QMessageBox.StandardButton.Yes


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctx: AppContext | None = None):
        super().__init__()
        self.ctx = ctx or AppContext(self)
        transmit.register_standard_actions()
        self.transmitter = transmit.Transmitter(None)   # rebound (and re-disabled) on each connect
        self.settings = QtCore.QSettings("CANopener", "CANopener")
        config.set_data_dir(self.settings.value("data_dir") or None)
        self.setWindowTitle(f"CANopener {__version__}")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(760, 460)       # floor so it always fits a small laptop / instrument screen
        self.resize(1300, 850)
        self._last_counts = (time.time(), 0, 0)
        self._bus_load = 0.0
        self._replay_path = ""

        self._build_toolbar()
        central = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(central)
        lay.setContentsMargins(6, 4, 6, 4)
        self.lamps = LampPanel()
        lay.addWidget(self.lamps)
        self.detect_bar = self._build_detect_bar()
        lay.addWidget(self.detect_bar)
        self.tabs = QtWidgets.QTabWidget()
        self.tab_list = [CodesTab(self.ctx), LiveTab(self.ctx), MonitorTab(self.ctx), NetworkTab(self.ctx),
                         InfoTab(self.ctx), ObdTab(self.ctx), ReportTab(self.ctx)]
        for t in self.tab_list:
            self.tabs.addTab(t, t.title)
        (self.codes, self.live, self.monitor, self.network, self.info, self.obd,
         self.report) = self.tab_list
        self.tabs.currentChanged.connect(lambda _: self._tick())
        lay.addWidget(self.tabs)
        self.setCentralWidget(central)

        self.hotkeys = Hotkeys(self.settings, lambda: self.log_panel.isVisible() and self.isActiveWindow(), self,
                               naming_box=lambda w: self.log_panel.owns_name_editor(w))
        self.log_panel = LogPanel(self.ctx, self.hotkeys, self.settings, self._capture_meta, self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.log_panel)
        self.log_panel.export_requested.connect(self.export_logs)
        self.log_panel.keys_requested.connect(self.edit_keys)
        self.log_panel.open_capture_log.connect(self.open_log)
        self.log_toggle.setDefaultAction(self.log_panel.toggleViewAction())
        self.log_panel.toggleViewAction().setText("📝 Log")
        self.log_panel.toggleViewAction().setToolTip("Show / hide the Log tool (its keys only work while it's open)")
        QtWidgets.QApplication.instance().installEventFilter(self.hotkeys)
        self._build_menu()
        self._plugin_refresh: list = []
        self._load_plugins()
        self._build_status()

        c = self.ctx
        c.session_changed.connect(self._session_changed)
        c.result_ready.connect(self._result_ready)
        c.scan_failed.connect(lambda msg: QtWidgets.QMessageBox.critical(self, "Read failed", msg))
        c.status_message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        c.show_signal.connect(lambda spn: self.tabs.setCurrentWidget(self.live))

        self._restore_settings()
        self._fit_to_screen()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_MS)
        self._session_changed(None)

    def _fit_to_screen(self):
        """Keep the window within the screen so it fits a small laptop and can still be moved,
        maximised and snapped. A window bigger than the work area can't be snapped on
        Cinnamon/GNOME. Safe to call repeatedly: it only ever *shrinks* an oversized window and
        nudges it back on-screen, so it never fights a user making the window smaller. It is
        called again after content loads (fault lamps, a long report) in case a widget's content
        has pushed the window past the screen edge."""
        if self.isMaximized() or self.isFullScreen():
            return
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        frame = self.frameGeometry().size() - self.size()      # title bar / borders
        max_w = avail.width() - frame.width()
        max_h = avail.height() - frame.height()
        if self.width() > max_w or self.height() > max_h:
            self.resize(min(self.width(), max_w), min(self.height(), max_h))
        # Nudge fully onto the screen if content growth or restored geometry put it partly off.
        g = self.frameGeometry()
        g.moveLeft(max(avail.left(), min(g.left(), avail.right() - g.width())))
        g.moveTop(max(avail.top(), min(g.top(), avail.bottom() - g.height())))
        self.move(g.topLeft())

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_toolbar(self):
        tb = QtWidgets.QToolBar("Connection")
        tb.setMovable(False)
        tb.setObjectName("connection")
        self.addToolBar(tb)
        self.source = QtWidgets.QComboBox()
        for label, kind in SOURCES:
            self.source.addItem(label, kind)
        self.source.currentIndexChanged.connect(self._source_changed)
        tb.addWidget(self.source)

        self.iface = QtWidgets.QComboBox()
        self.iface.setEditable(True)
        self.iface.setMinimumWidth(110)
        self.iface.setToolTip("SocketCAN interface (can0, vcan0, slcan0 ...)")
        tb.addWidget(self.iface)
        self.rescan_btn = QtWidgets.QToolButton()
        self.rescan_btn.setText("⟳")
        self.rescan_btn.setToolTip("Look for CAN interfaces again")
        self.rescan_btn.clicked.connect(self._refresh_interfaces)
        tb.addWidget(self.rescan_btn)

        self.bitrate_label = QtWidgets.QLabel(" Bitrate ")
        tb.addWidget(self.bitrate_label)
        self.bitrate = QtWidgets.QComboBox()
        for br in interfaces.BITRATES:
            self.bitrate.addItem(f"{br // 1000}k", br)
        self.bitrate.addItem("Auto", AUTO_BITRATE)
        self.bitrate.setItemData(self.bitrate.count() - 1, "Listen at each bitrate (listen-only, so nothing "
                                 "is transmitted) until valid traffic is seen", Qt.ItemDataRole.ToolTipRole)
        tb.addWidget(self.bitrate)
        self.listen_only = QtWidgets.QCheckBox("Listen-only")
        self.listen_only.setToolTip("Transmit nothing at all. DM1 and live data still work (they're broadcast).")
        self.listen_only.toggled.connect(self._listen_only_toggled)
        tb.addWidget(self.listen_only)
        self.setup_btn = QtWidgets.QPushButton("Set Up Interface")
        self.setup_btn.setToolTip("Configure bitrate / listen-only and bring the interface up (asks for your password)")
        self.setup_btn.clicked.connect(self.setup_interface)
        tb.addWidget(self.setup_btn)
        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.setMinimumWidth(100)
        self.connect_btn.clicked.connect(self.toggle_connect)
        tb.addWidget(self.connect_btn)

        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.log_toggle = QtWidgets.QToolButton()
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.log_toggle.setStyleSheet("font-weight: bold; padding: 3px 10px;")
        tb.addWidget(self.log_toggle)
        tb.addWidget(QtWidgets.QLabel(" Profile "))
        self.profile = QtWidgets.QComboBox()
        self.profile.setMinimumWidth(170)
        self.profile.currentIndexChanged.connect(self._profile_changed)
        tb.addWidget(self.profile)
        tb.addWidget(QtWidgets.QLabel(" Units "))
        self.units = QtWidgets.QComboBox()
        self.units.addItem("Metric", units.METRIC)
        self.units.addItem("Imperial", units.IMPERIAL)
        self.units.addItem("Custom", units.CUSTOM)
        self.units.setToolTip("Display units. Settings > Units... picks each one (e.g. psi with °C).")
        self.units.currentIndexChanged.connect(lambda _: self.ctx.set_units(self.units.currentData()))
        tb.addWidget(self.units)

    def _build_menu(self):
        m = self.menuBar()
        f = m.addMenu("&File")
        self._action(f, "&Open log file (decode)...", self.open_log, "Ctrl+O")
        self._action(f, "&Replay log file...", self.replay_log)
        f.addSeparator()
        self._action(f, "&Save report...", self.report.save, "Ctrl+S")
        self._action(f, "Export frames as &CSV...", self.export_csv)
        self._action(f, "&Export Logs (USB drive / folder)...", self.export_logs, "Ctrl+E")
        f.addSeparator()
        self._action(f, "&Quit", self.close, "Ctrl+Q")
        lg = m.addMenu("&Log")
        toggle = self.log_panel.toggleViewAction()
        toggle.setShortcut(QtGui.QKeySequence("F2"))
        lg.addAction(toggle)
        lg.addSeparator()
        self._action(lg, "Start / stop recording", lambda: self.log_panel.handle_action("start_stop"))
        self._action(lg, "Timestamp", lambda: self.log_panel.handle_action("timestamp"))
        self._action(lg, "Open saved capture...", lambda: self.log_panel.open_saved())
        lg.addSeparator()
        self._action(lg, "Export Logs...", self.export_logs)
        self._action(lg, "Change save folder...", self.log_panel._change_folder)
        self._action(lg, "Keyboard shortcuts...", self.edit_keys)
        d = m.addMenu("&Diagnostics")
        for preset, (title, _, _) in scan.PRESETS.items():
            self._action(d, title, lambda _=False, p=preset: self._run(p))
        d.addSeparator()
        self._action(d, "Request a PGN...", lambda: RequestDialog(self.ctx, self).exec())
        self._action(d, "Transmit a frame...", self._open_transmit)
        v = m.addMenu("&View")
        self._action(v, "&Full screen", self._toggle_fullscreen, "F11")
        for i, t in enumerate(self.tab_list):
            self._action(v, t.title, lambda _=False, w=t: self.tabs.setCurrentWidget(w), f"Ctrl+{i + 1}")
        st = m.addMenu("&Settings")
        self._action(st, "&Units...", self.edit_units)
        self._action(st, "Log tool &keys...", self.edit_keys)
        self._action(st, "&Save folder...", self.log_panel._change_folder)
        p = m.addMenu("&Profiles")
        self._action(p, "Reload profiles", self._load_profiles)
        self._action(p, "Open user profile folder", self._open_profile_dir)
        h = m.addMenu("&Help")
        self._action(h, "&About", self._about)

    def _action(self, menu, text, fn, shortcut=None):
        a = QtGui.QAction(text, self)
        if shortcut:
            a.setShortcut(QtGui.QKeySequence(shortcut))
        a.triggered.connect(fn)
        menu.addAction(a)
        return a

    def _load_plugins(self):
        from .. import plugins
        for p in plugins.discover():
            hook = p.hook("register_gui")
            if hook is None:
                continue
            try:
                hook(PluginApi(self, p))
            except Exception as e:
                p.error = f"register_gui failed: {type(e).__name__}: {e}"

    def _build_status(self):
        sb = self.statusBar()
        # Busy indicator: a marching progress bar + label, shown only while a scan is running.
        # Driven by ctx.busy_changed so it covers every preset and is visible on any tab.
        self.busy_label = QtWidgets.QLabel("Working...")
        self.busy_bar = QtWidgets.QProgressBar()
        self.busy_bar.setRange(0, 0)                    # 0..0 = indeterminate ("marching ants")
        self.busy_bar.setMaximumWidth(120)
        self.busy_bar.setTextVisible(False)
        self.busy_label.setVisible(False)
        self.busy_bar.setVisible(False)
        sb.addWidget(self.busy_label)
        sb.addWidget(self.busy_bar)
        self.conn_label = QtWidgets.QLabel("Not connected")
        self.traffic_label = QtWidgets.QLabel("")
        self.error_label = QtWidgets.QLabel("")
        self.rec_label = QtWidgets.QLabel("")
        for w in (self.conn_label, self.traffic_label, self.error_label, self.rec_label):
            sb.addPermanentWidget(w)
        self.ctx.busy_changed.connect(self._show_busy)

    def _show_busy(self, busy: bool):
        self.busy_label.setVisible(busy)
        self.busy_bar.setVisible(busy)
        if busy:
            QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        else:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _build_detect_bar(self) -> QtWidgets.QFrame:
        """'Looks like an X - use its profile?' banner, shown when the bus matches a profile."""
        bar = QtWidgets.QFrame()
        bar.setStyleSheet("QFrame { background: #fff3bf; border: 1px solid #fab005; border-radius: 4px; }"
                          "QLabel { border: none; }")
        hl = QtWidgets.QHBoxLayout(bar)
        hl.setContentsMargins(8, 3, 8, 3)
        self.detect_label = QtWidgets.QLabel()
        self.detect_label.setWordWrap(True)        # wrap long "Looks like ..." text instead of forcing width
        hl.addWidget(self.detect_label, 1)
        use = QtWidgets.QPushButton("Use this profile")
        use.clicked.connect(self._use_detected)
        hl.addWidget(use)
        dismiss = QtWidgets.QToolButton()
        dismiss.setText("✕")
        dismiss.setToolTip("Not this machine - don't ask again until reconnecting")
        dismiss.clicked.connect(self._dismiss_detected)
        hl.addWidget(dismiss)
        bar.hide()
        self._detected = None
        self._detect_dismissed: set[str] = set()
        self._last_detect = 0.0
        return bar

    def _check_detection(self, session):
        """Every couple of seconds while live: does the bus match a profile we're not using?"""
        now = time.time()
        if now - self._last_detect < 2.0:
            return
        self._last_detect = now
        current = self.profile.currentData() or ""
        matches = [m for m in profiles.detect(session.known_ecus(), self._profiles, profiles.present_pgns(session))
                   if m.profile.id != current and m.profile.id not in self._detect_dismissed]
        active = profiles.active()
        if active is not None and active.detect_addresses and matches \
                and matches[0].matched <= len(active.detect_addresses & set(session.known_ecus())):
            matches = []                # the chosen profile fits at least as well: leave it alone
        if not matches:
            self.detect_bar.hide()
            self._detected = None
            return
        self._detected = matches[0].profile
        self.detect_label.setText(f"<b>Looks like:</b> {matches[0].describe()}")
        self.detect_bar.show()
        QtCore.QTimer.singleShot(0, self._fit_to_screen)

    def _use_detected(self):
        if self._detected is not None:
            self.profile.setCurrentIndex(max(self.profile.findData(self._detected.id), 0))
            self.statusBar().showMessage(f"Profile: {self._detected.name}", 6000)
        self.detect_bar.hide()

    def _dismiss_detected(self):
        if self._detected is not None:
            self._detect_dismissed.add(self._detected.id)
        self.detect_bar.hide()

    def _run(self, preset):
        if not self.ctx.live:
            QtWidgets.QMessageBox.information(self, "Not connected", "Connect to a bus first.")
            return
        self.ctx.run_preset(preset)

    # ------------------------------------------------------------------
    # settings / profiles
    # ------------------------------------------------------------------
    def _restore_settings(self):
        s = self.settings
        kind = s.value("source", connection.SOCKETCAN)
        idx = self.source.findData(kind)
        self.source.setCurrentIndex(max(idx, 0))
        self._refresh_interfaces(preferred=s.value("iface", "can0"))
        idx = self.bitrate.findData(int(s.value("bitrate", 250000)))
        self.bitrate.setCurrentIndex(max(idx, 0))
        self.listen_only.setChecked(s.value("listen_only", "false") in (True, "true"))
        s.beginGroup("units_custom")
        units.set_custom({k: s.value(k) for k in s.childKeys()})
        s.endGroup()
        idx = self.units.findData(s.value("units", units.METRIC))
        self.units.setCurrentIndex(max(idx, 0))
        self._load_profiles(preferred=s.value("profile", "generic"))
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        state = s.value("window_state")
        if state is not None:
            self.restoreState(state)
        self._source_changed()

    def _save_settings(self):
        s = self.settings
        s.setValue("source", self.source.currentData())
        s.setValue("iface", self.iface.currentText())
        s.setValue("bitrate", self.bitrate.currentData())
        s.setValue("listen_only", self.listen_only.isChecked())
        s.setValue("units", self.units.currentData())
        s.setValue("profile", self.profile.currentData() or "")
        s.setValue("geometry", self.saveGeometry())
        s.setValue("window_state", self.saveState())

    def _load_profiles(self, preferred=None):
        current = preferred if isinstance(preferred, str) else self.profile.currentData()
        found, errors = profiles.available_profiles()
        self._profiles = found
        self.profile.blockSignals(True)
        self.profile.clear()
        self.profile.addItem("(none)", "")
        for p in found:
            self.profile.addItem(p.name + ("" if p.builtin else "  ★"), p.id)
            self.profile.setItemData(self.profile.count() - 1, p.description, Qt.ItemDataRole.ToolTipRole)
        idx = self.profile.findData(current)
        self.profile.setCurrentIndex(idx if idx >= 0 else 0)
        self.profile.blockSignals(False)
        self._profile_changed()
        if errors:
            QtWidgets.QMessageBox.warning(self, "Profiles",
                                          "Some profiles could not be loaded:\n\n" + "\n".join(errors))

    def _profile_changed(self, *_):
        pid = self.profile.currentData()
        profiles.apply(profiles.find(pid) if pid else None)
        self.ctx.profile_changed.emit()

    def _open_profile_dir(self):
        d = config.ensure_dir(profiles.user_dir())
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(d))

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    def _refresh_interfaces(self, *_, preferred=None):
        cur = preferred or self.iface.currentText() or "can0"
        self.iface.clear()
        found = interfaces.list_interfaces()
        for i in found:
            self.iface.addItem(i.name)
            self.iface.setItemData(self.iface.count() - 1, i.describe(), Qt.ItemDataRole.ToolTipRole)
        if self.iface.findText(cur) < 0:
            self.iface.addItem(cur)
        self.iface.setCurrentText(cur)
        match = next((i for i in found if i.name == cur), None)
        if match and match.bitrate:
            idx = self.bitrate.findData(match.bitrate)
            if idx >= 0:
                self.bitrate.setCurrentIndex(idx)

    def _source_changed(self, *_):
        kind = self.source.currentData()
        hw = kind == connection.SOCKETCAN
        for w in (self.iface, self.rescan_btn, self.bitrate, self.bitrate_label, self.setup_btn):
            w.setVisible(hw)
        self.listen_only.setVisible(kind != connection.REPLAY)

    def _listen_only_toggled(self, on):
        if self.ctx.session is not None and not self.ctx.session.offline:
            session = self.ctx.session
            session.listen_only = on or not session.backend.can_transmit
            self.conn_label.setText(self.ctx.connection.describe() + ("  [listen-only]" if session.listen_only else ""))
            self.network.claim_btn.setEnabled(not session.listen_only)

    def setup_interface(self):
        name = self.iface.currentText().strip()
        if not interfaces.valid_name(name):
            QtWidgets.QMessageBox.critical(self, "Set up interface", f"Bad interface name {name!r}")
            return
        was = self.ctx.connection is not None
        if was:
            self.disconnect()
        bitrate = self.bitrate.currentData()
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if bitrate == AUTO_BITRATE:
                bitrate = self._detect_bitrate(name)
                if bitrate is None:
                    return
            steps = interfaces.setup_steps(name, bitrate, self.listen_only.isChecked())
            ok, msg = interfaces.run_privileged(steps)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if not ok:
            QtWidgets.QMessageBox.critical(self, "Set up interface", f"Failed:\n{msg}\n\nIf it complains about "
                                           "listen-only, untick it and retry (not every adapter supports it).")
            return
        self.statusBar().showMessage(f"{name} is up at {bitrate // 1000}k", 5000)
        self._refresh_interfaces(preferred=name)
        if was:
            self.connect()

    def _detect_bitrate(self, name: str):
        def progress(br):
            self.statusBar().showMessage(f"Listening on {name} at {br // 1000}k...")
            QtWidgets.QApplication.processEvents()

        found, log = interfaces.detect_bitrate(name, progress=progress)
        if found is None:
            QtWidgets.QMessageBox.warning(
                self, "Auto bitrate", "Couldn't detect the bitrate:\n\n" + "\n".join(log)
                + "\n\nIs the key on and the harness connected? You can also pick a bitrate by hand.")
            return None
        idx = self.bitrate.findData(found)
        if idx >= 0:
            self.bitrate.setCurrentIndex(idx)
        return found

    def toggle_connect(self):
        if self.ctx.connection is not None or (self.ctx.session and self.ctx.session.offline):
            self.disconnect()
        else:
            self.connect()

    def connect(self, kind=None):
        kind = kind or self.source.currentData()
        channel = self.iface.currentText().strip() or "can0"
        if kind == connection.REPLAY:
            if not self._replay_path:
                path, _ = QtWidgets.QFileDialog.getOpenFileName(
                    self, "Replay candump log", config.data_dir(), "CAN logs (*.log *.txt);;All files (*)")
                if not path:
                    return
                self._replay_path = path
            channel = self._replay_path
        try:
            conn = open_connection(kind, channel, listen_only=self.listen_only.isChecked(),
                                   profile=profiles.active())
        except Exception as e:
            hint = ("\n\nIs the adapter plugged in? Try 'Set Up Interface' first, or pick 'Simulator' "
                    "to try the app without hardware." if kind == connection.SOCKETCAN else "")
            QtWidgets.QMessageBox.critical(self, "Connect", f"Could not open {channel}: {e}{hint}")
            self._replay_path = ""
            return
        self.ctx.bitrate = (self.bitrate.currentData() or 250000) if kind == connection.SOCKETCAN else 250000
        self.ctx.result = None
        self.ctx.set_connection(conn)
        self._replay_path = ""

    def disconnect(self):
        self.ctx.set_connection(None)       # the Log tool stops a running capture by itself

    def replay_log(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Replay candump log", config.data_dir(), "CAN logs (*.log *.txt);;All files (*)")
        if not path:
            return
        self.disconnect()
        self.source.setCurrentIndex(self.source.findData(connection.REPLAY))
        self._replay_path = path
        self.connect(connection.REPLAY)

    def open_log(self, path=None):
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Open candump log", config.data_dir(), "CAN logs (*.log *.txt);;All files (*)")
            if not path:
                return
        self.disconnect()
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            session, n, markers = load_log_session(path)
            result = scan.offline_result(session)
            result.source = f"{path} ({n} frames)"
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Open log", str(e))
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._offline_label = f"Log: {os.path.basename(path)} ({n} frames)"
        self.ctx.set_offline_session(session, result)
        if markers:
            t0 = session.raw[0].timestamp if session.raw else markers[0][0]
            lines = "\n".join(f"  +{t - t0:7.2f}s   {label}" for t, label in markers)
            QtWidgets.QMessageBox.information(
                self, "Markers in log",
                f"Found {len(markers)} timestamp marker(s) written by the capture tool:\n\n{lines}")

    def export_csv(self):
        s = self.ctx.session
        if s is None or not s.raw:
            QtWidgets.QMessageBox.information(self, "Export CSV", "No frames to export yet.")
            return
        d = config.ensure_dir(config.logs_dir())
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export frames as CSV",
                                                        os.path.join(d, f"frames_{stamp}.csv"), "CSV (*.csv)")
        if not path:
            return
        with s.lock:
            frames = list(s.raw)
        n = storage.write_csv(path, frames)
        if path not in self.ctx.saved_paths:
            self.ctx.saved_paths.append(path)
        self.statusBar().showMessage(f"Wrote {n} frames to {path}", 8000)

    def export_logs(self):
        ExportDialog(self.ctx, self).exec()

    def edit_units(self):
        start = units.prefs(self.units.currentData())
        dlg = UnitsDialog(start, self)
        if not dlg.exec():
            return
        units.set_custom(dlg.choice())
        self.settings.beginGroup("units_custom")
        for q, u in units.custom().items():
            self.settings.setValue(q, u)
        self.settings.endGroup()
        if self.units.currentData() != units.CUSTOM:
            self.units.setCurrentIndex(self.units.findData(units.CUSTOM))    # emits units_changed
        else:
            self.ctx.units_changed.emit(units.CUSTOM)
        self.statusBar().showMessage(f"Units: {units.describe(units.CUSTOM)}", 6000)

    def edit_keys(self):
        if KeybindDialog(self.hotkeys, self).exec():
            self.log_panel._sync()

    def _capture_meta(self) -> dict:
        s = self.ctx.session
        meta = {"source": self.ctx.connection.describe() if self.ctx.connection else "offline",
                "profile": self.profile.currentData() or "none", "units": self.ctx.units}
        if self.ctx.connection is not None and self.ctx.connection.kind == connection.SOCKETCAN:
            meta.update(interface=self.iface.currentText(), bitrate=self.ctx.bitrate)
        if s is not None:
            meta["listen_only"] = s.listen_only
        return meta

    # ------------------------------------------------------------------
    # periodic updates
    # ------------------------------------------------------------------
    def _open_transmit(self):
        TransmitDialog(self.ctx, self.transmitter, self).exec()

    def _session_changed(self, session):
        # Fresh transmitter per connection: transmit mode is always off again on a new bus.
        self.transmitter = transmit.Transmitter(session)
        live = self.ctx.live
        offline = session is not None and session.offline
        self.connect_btn.setText("Disconnect" if (live or offline) else "Connect")
        for w in (self.source, self.iface, self.rescan_btn, self.bitrate):
            w.setEnabled(not live)
        if live:
            self.conn_label.setText(self.ctx.connection.describe()
                                    + ("  [listen-only]" if session.listen_only else ""))
        elif offline:
            self.conn_label.setText(getattr(self, "_offline_label", "Log file"))
        else:
            self.conn_label.setText("Not connected")
            self.lamps.set_lamps({})
        self.detect_bar.hide()
        self._detect_dismissed.clear()
        self._last_counts = (time.time(), session.rx_count if session else 0, session.bytes_count if session else 0)
        self._tick()

    def _result_ready(self, result):
        if result.offline:
            self.tabs.setCurrentWidget(self.codes)
        msg = f"{result.title}: {result.code_count} code(s)"
        if result.listen_only and not result.offline:
            msg += " (listen-only: only broadcast data)"
        self.statusBar().showMessage(msg, 8000)
        # Results populate the lamp strip and tables; make sure that growth didn't push the
        # window off-screen (belt-and-suspenders on top of the wrapping layouts).
        QtCore.QTimer.singleShot(0, self._fit_to_screen)

    def _tick(self):
        s = self.ctx.session
        self.live.sample()
        self.log_panel.refresh()
        for fn in self._plugin_refresh:
            try:
                fn()
            except Exception as e:
                self.statusBar().showMessage(f"Plugin error: {e}", 5000)
        cur = self.tabs.currentWidget()
        self.monitor.refresh()              # always: it consumes the frame stream
        for t in (self.codes, self.live, self.network):
            if t is cur:
                t.refresh()
        if s is None:
            self.traffic_label.setText("")
            self.error_label.setText("")
            self.rec_label.setText("")
            return
        self.lamps.set_lamps(s.current_lamps())
        if not s.offline:
            self._check_detection(s)
            now = time.time()
            t0, n0, b0 = self._last_counts
            if now - t0 >= 1.0:
                dn, db = s.rx_count - n0, s.bytes_count - b0
                # ~67 overhead bits per extended frame + data, plus ~10% bit stuffing
                self._bus_load = ((dn * 67 + db * 8) * 1.1) / (now - t0) / self.ctx.bitrate * 100
                self._fps = dn / (now - t0)
                self._last_counts = (now, s.rx_count, s.bytes_count)
            self.traffic_label.setText(f"{getattr(self, '_fps', 0):.0f} frames/s  ~{self._bus_load:.0f}% load  "
                                       f"{len(s.ecus)} ECU(s)")
            backend = s.backend
            if getattr(backend, "finished", False):
                self.traffic_label.setText("Replay finished")
        else:
            self.traffic_label.setText(f"{s.rx_count} frames  {len(s.ecus)} ECU(s)")
        h = s.health
        recent = h.last_error if h.last_error and time.time() - h.last_error_t < 10 else None
        err = h.link_error or h.tx_error or recent
        self.error_label.setText(f"⚠ {err}" if err else "")
        self.error_label.setStyleSheet("color: #e03131" if err else "")
        c = self.log_panel.capture
        rec = self.log_panel.recording
        self.rec_label.setText(f"● REC {c.elapsed():.0f} s, {c.recorded_frames:,} frames" if rec else "")
        self.rec_label.setStyleSheet("color: #e03131; font-weight: bold" if rec else "")

    # ------------------------------------------------------------------
    def _toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _about(self):
        QtWidgets.QMessageBox.about(
            self, "About CANopener",
            f"<h3>CANopener {__version__}</h3><p>J1939 diagnostics for equipment and vehicles.</p>"
            "<p><b>Read-only:</b> the app only sends J1939 Requests (PGN 59904) and transport-protocol "
            "flow control. It never clears codes or writes to an ECU. Tick Listen-only to send nothing.</p>"
            f"<p>Qt binding: {QT_API}</p>" + self._plugins_html())

    def _plugins_html(self) -> str:
        from .. import plugins
        found = plugins.discover()
        if not found:
            return ""
        rows = "".join(f"<li>{p.name or p.id} {p.version}" + (f" - <i>{p.error}</i>" if p.error else "") + "</li>"
                       for p in found)
        return f"<p><b>Plugins:</b><ul>{rows}</ul></p>"

    def showEvent(self, ev):
        super().showEvent(ev)
        if not getattr(self, "_checked_pending", False):
            self._checked_pending = True
            QtCore.QTimer.singleShot(300, self.log_panel.check_pending)

    def closeEvent(self, ev):
        if not self.log_panel.confirm_unsaved():
            ev.ignore()
            return
        QtWidgets.QApplication.instance().removeEventFilter(self.hotkeys)
        self._save_settings()
        self.timer.stop()
        if self.ctx.connection is not None:
            self.ctx.connection.close()
        super().closeEvent(ev)
