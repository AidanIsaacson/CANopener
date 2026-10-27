"""Main window: toolbar, lamp strip, tabs, Connection and Log sidebars, status bar with link lamps."""

import datetime
import os
import time

from .. import __version__
from ..can import pythoncan
from ..core import config, connection, interfaces, link, profiles, report, scan, storage, transmit, units
from ..core.connection import load_log_session, open_connection
from .connection_panel import AUTO_BITRATE, ConnectionPanel
from .context import AppContext
from .export_dialog import ExportDialog
from .hotkeys import Hotkeys, KeybindDialog
from .log_panel import LogPanel
from .qt import QT_API, Qt, QtCore, QtGui, QtWidgets, Signal
from .request_dialog import RequestDialog
from .settings_dialog import SoundDialog, UnitsDialog
from .tab_codes import CodesTab
from .tab_info import InfoTab
from .tab_live import LiveTab
from .tab_monitor import MonitorTab
from .tab_network import NetworkTab
from .tab_obd import ObdTab
from .tab_report import ReportTab
from .transmit_dialog import TransmitDialog
from .widgets import LampPanel, StatusLamp, app_icon

REFRESH_MS = 200
LINK_CHECK_SECS = 1.0       # how often the adapter lamp re-checks for the interface


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


PYTHON_CAN_SETUP = ("{what} works with Linux CAN interfaces like can0. A python-can adapter ({name}) is "
                    "set up when you press Connect, at the bitrate picked here.")


class MainWindow(QtWidgets.QMainWindow):
    _update_found = Signal(object)          # an UpdateStatus from the startup check's thread

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
        self._link_checked = 0.0
        self._adapter = link.Status(link.OFF, "Adapter")
        self._bus_level = link.OFF
        self._bus_session = None
        self._bus_was_ok = False
        self._protocol = None               # the profile protocol whose screens are shown
        self.switch_to: str | None = None   # another front-end to start once this window has closed
        self.was_fullscreen = False

        self._build_conn_panel()
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
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.conn_panel)
        self.log_panel.export_requested.connect(self.export_logs)
        self.log_panel.keys_requested.connect(self.edit_keys)
        self.log_panel.open_capture_log.connect(self.open_log)
        # A car only talks when asked: while recording on the OBD-II profile, the OBD-II tab polls.
        self.log_panel.recording_changed.connect(
            lambda on: self.obd.recording_changed(on) if self._protocol == profiles.OBD2 else None)
        self.log_panel.status_extra = lambda: self.obd.log_status() if self._protocol == profiles.OBD2 else ""
        self.log_panel.extra_reports = self._obd_reports
        self.log_toggle.setDefaultAction(self.log_panel.toggleViewAction())
        self.log_panel.toggleViewAction().setText("📝 Log")
        self.log_panel.toggleViewAction().setToolTip("Show / hide the Log tool (its keys only work while it's open)")
        self.conn_toggle.setDefaultAction(self.conn_panel.toggleViewAction())
        self.conn_panel.toggleViewAction().setText("🔌 Connection")
        self.conn_panel.toggleViewAction().setToolTip("Show / hide Connection setup and the signal finder")
        QtWidgets.QApplication.instance().installEventFilter(self.hotkeys)
        self._build_menu()
        self._plugin_refresh: list = []
        self._update_status = None
        self._update_found.connect(self._show_update_found)
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
        if (self.autoconnect_action.isChecked()
                and self.source.currentData() == connection.SOCKETCAN):
            QtCore.QTimer.singleShot(0, self._connect_on_startup)   # after the window is shown

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
    def _build_conn_panel(self):
        """The Connection sidebar. Its controls stay reachable as window attributes (``source``,
        ``iface``, ``bitrate`` ...) since connecting is still done here."""
        p = self.conn_panel = ConnectionPanel(self.settings, self._iface_list_opening, self)
        self.source, self.iface, self.rescan_btn = p.source, p.iface, p.rescan_btn
        self.bitrate, self.bitrate_label, self.listen_only = p.bitrate, p.bitrate_label, p.listen_only
        self.setup_btn, self.connect_btn = p.setup_btn, p.connect_btn
        self.source.currentIndexChanged.connect(self._source_changed)
        self.rescan_btn.clicked.connect(self._refresh_interfaces)
        self.listen_only.toggled.connect(self._listen_only_toggled)
        self.setup_btn.clicked.connect(self.setup_interface)
        self.connect_btn.clicked.connect(self.toggle_connect)
        p.finder_starting.connect(self.disconnect)
        p.sudoers_requested.connect(self._install_sudoers)
        p.locked.connect(self._finder_locked)

    def _build_toolbar(self):
        tb = QtWidgets.QToolBar("Main")
        tb.setMovable(False)
        tb.setObjectName("main_toolbar")
        self.addToolBar(tb)
        self.conn_toggle = QtWidgets.QToolButton()
        self.conn_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.conn_toggle.setStyleSheet("font-weight: bold; padding: 3px 10px;")
        tb.addWidget(self.conn_toggle)

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
        cn = m.addMenu("&Connection")
        toggle = self.conn_panel.toggleViewAction()
        toggle.setShortcut(QtGui.QKeySequence("F3"))
        cn.addAction(toggle)
        cn.addSeparator()
        self._action(cn, "Connect / disconnect", self.toggle_connect, "Ctrl+K")
        self._action(cn, "Find signal (start / stop)", self._toggle_finder, "F4")
        self._action(cn, "Set up interface", self.setup_interface)
        self._action(cn, "Find other adapters (python-can)…", self.find_python_can)
        cn.addSeparator()
        from ..core.settings import Settings
        self.simulators_action = QtGui.QAction("Show simulators", self, checkable=True)
        self.simulators_action.setToolTip("List the simulated machine, car and profile under Source, to try "
                                          "the app without hardware.")
        self.simulators_action.setChecked(bool(Settings().get("show_simulators")))
        self.simulators_action.toggled.connect(self.show_simulators)
        cn.addAction(self.simulators_action)
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
        self._j1939_actions = [self._action(d, title, lambda _=False, p=preset: self._run(p))
                               for preset, (title, _, _) in scan.PRESETS.items()]
        d.addSeparator()
        self._action(d, "Request a PGN...", lambda: RequestDialog(self.ctx, self).exec())
        self._action(d, "Read a module (UDS)...", self._open_uds)
        self._action(d, "Transmit a frame...", self._open_transmit)
        v = m.addMenu("&View")
        self._action(v, "&Full screen", self._toggle_fullscreen, "F11")
        self._view_actions = {t: self._action(v, t.title, lambda _=False, w=t: self.show_tab(w),
                                              f"Ctrl+{i + 1}") for i, t in enumerate(self.tab_list)}
        st = m.addMenu("&Settings")
        self._action(st, "&Units...", self.edit_units)
        self._action(st, "Log tool &keys...", self.edit_keys)
        self._action(st, "Sound &cues (speaker / buzzer)...", self.edit_sound)
        self._action(st, "&Save folder...", self.log_panel._change_folder)
        self._action(st, "Set up &passwordless CAN access...", self._install_sudoers)
        self.autoconnect_action = QtGui.QAction("&Connect on startup", self, checkable=True)
        self.autoconnect_action.setToolTip("When the app opens, connect straight to the SocketCAN "
                                           "adapter (brings the interface up for you).")
        self.autoconnect_action.toggled.connect(
            lambda on: self.settings.setValue("connect_on_startup", on))
        st.addAction(self.autoconnect_action)
        st.addSeparator()
        self.handheld_action = QtGui.QAction("Handheld (Pi) screen", self, checkable=True)
        self.handheld_action.setToolTip("Switch to the Raspberry Pi handheld's touch UI in a window, to try it "
                                        "here. Its System > Desktop app instead switches back.")
        self.handheld_action.toggled.connect(self._switch_to_handheld)
        st.addAction(self.handheld_action)
        p = m.addMenu("&Profiles")
        self._action(p, "&Manage profiles…", self._manage_profiles)
        self._action(p, "Reload profiles", self._load_profiles)
        self._action(p, "Open user profile folder", self._open_profile_dir)
        h = m.addMenu("&Help")
        self.update_action = self._action(h, "Check for &updates…", self.check_updates)
        self.update_on_start = QtGui.QAction("Check for updates at &startup", self, checkable=True)
        self.update_on_start.setToolTip("When the app opens, quietly check GitHub; says something only if "
                                        "there's an update. Off by default.")
        self.update_on_start.setChecked(self.settings.value("update/check_on_start", "false") in (True, "true"))
        self.update_on_start.toggled.connect(lambda on: self.settings.setValue("update/check_on_start", on))
        h.addAction(self.update_on_start)
        h.addSeparator()
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
        # The two link lamps: is the adapter plugged in, and is it hearing the machine?
        # Click either to open Connection setup.
        self.adapter_lamp = StatusLamp("Adapter")
        self.bus_lamp = StatusLamp("Bus")
        for lamp in (self.adapter_lamp, self.bus_lamp):
            lamp.clicked.connect(self._show_conn_panel)
            sb.addPermanentWidget(lamp)
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
        self.autoconnect_action.setChecked(s.value("connect_on_startup", "false") in (True, "true"))
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
        self._all_profiles = found
        self._profiles = profiles.arrange(found)        # enabled ones, in the user's order (Manage profiles)
        self.profile.blockSignals(True)
        self.profile.clear()
        self.profile.addItem("(none)", "")
        for p in self._profiles:
            self.profile.addItem(p.name + ("" if p.builtin else "  ★"), p.id)
            self.profile.setItemData(self.profile.count() - 1, p.description, Qt.ItemDataRole.ToolTipRole)
        idx = self.profile.findData(current)
        self.profile.setCurrentIndex(idx if idx >= 0 else 0)
        self.profile.blockSignals(False)
        self._profile_changed()
        if errors:
            QtWidgets.QMessageBox.warning(self, "Profiles",
                                          "Some profiles could not be loaded:\n\n" + "\n".join(errors))

    def _manage_profiles(self):
        from .profiles_dialog import ProfilesDialog
        if ProfilesDialog(self._all_profiles, self.profile.currentData() or "", self).exec():
            self._load_profiles()

    def _profile_changed(self, *_):
        pid = self.profile.currentData()
        profiles.apply(profiles.find(pid) if pid else None)
        self._show_protocol(profiles.protocol())
        self.ctx.profile_changed.emit()

    def _show_protocol(self, protocol: str):
        """Only the screens for the profile's protocol: the OBD-II reader for a car, the J1939
        tabs, lamp strip and scans for everything else. Bus Monitor (and plugin tabs) always."""
        obd = protocol == profiles.OBD2
        self._place_monitor(inside_obd=obd)
        j1939_only = (self.codes, self.live, self.network, self.info, self.report)
        for t in self.tab_list:
            visible = (t is self.obd) == obd if (t is self.obd or t in j1939_only) else True
            if self.tabs.indexOf(t) >= 0:
                self.tabs.setTabVisible(self.tabs.indexOf(t), visible)
            if t in self._view_actions:
                self._view_actions[t].setEnabled(visible)
        for a in self._j1939_actions:
            a.setEnabled(not obd)
        self.lamps.setVisible(not obd)
        if protocol != self._protocol or not self.tabs.isTabVisible(self.tabs.currentIndex()):
            self.tabs.setCurrentWidget(self.obd if obd else self.codes)     # the new protocol's main screen
        # One visible tab (the OBD-II scan tool, with its own page row): no top row of tabs at all.
        self.tabs.tabBar().setVisible(sum(self.tabs.isTabVisible(i) for i in range(self.tabs.count())) > 1)
        if protocol != self._protocol and not obd:
            self.obd.stop_stream()
        self._protocol = protocol

    def _place_monitor(self, inside_obd: bool):
        """Bus Monitor is a top-level tab for J1939, and the last OBD-II page for a car."""
        if inside_obd and self.obd.pages.indexOf(self.monitor) < 0:
            self.obd.pages.addTab(self.monitor, self.monitor.title)
        elif not inside_obd and self.tabs.indexOf(self.monitor) < 0:
            self.tabs.insertTab(self.tab_list.index(self.monitor), self.monitor, self.monitor.title)

    def show_tab(self, w):
        """Bring a screen forward, wherever it lives (Bus Monitor may be an OBD-II page)."""
        if self.obd.pages.indexOf(w) >= 0:
            self.tabs.setCurrentWidget(self.obd)
            self.obd.pages.setCurrentWidget(w)
        else:
            self.tabs.setCurrentWidget(w)

    def _use_protocol(self, protocol: str):
        """Switch to a profile for this protocol if the current one is for the other (e.g. the
        OBD-II simulator picked while on a J1939 profile)."""
        if profiles.protocol() == protocol:
            return
        pid = "obd2" if protocol == profiles.OBD2 else "generic"
        idx = self.profile.findData(pid)
        if idx >= 0:
            self.profile.setCurrentIndex(idx)

    def _open_profile_dir(self):
        d = config.ensure_dir(profiles.user_dir())
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(d))

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    def _refresh_interfaces(self, *_, preferred=None, sync_bitrate=True):
        """Fill the interface list with the CAN interfaces that exist now. The text keeps the
        preferred / current name (default can0) even when it isn't connected, so Connect can say
        why. ``sync_bitrate`` matches the bitrate box to an interface that's already up; the
        re-check when the list opens skips that, so it doesn't undo a bitrate just picked."""
        cur = preferred or self.iface.currentText().strip() or "can0"
        found = interfaces.list_interfaces()
        if found and cur not in [i.name for i in found] and not pythoncan.is_spec(cur):
            # The adapter came up under another name (can1, or can0 after using vcan0): use it.
            cur = next((i.name for i in found if not i.virtual), cur)
        self.iface.blockSignals(True)
        self.iface.clear()
        for i in found:
            self.iface.addItem(i.name)
            self.iface.setItemData(self.iface.count() - 1, i.describe(), Qt.ItemDataRole.ToolTipRole)
        for spec in getattr(self, "_python_can_found", []):     # Connection > Find other adapters
            self.iface.addItem(spec)
            self.iface.setItemData(self.iface.count() - 1, "Opened through python-can",
                                   Qt.ItemDataRole.ToolTipRole)
        self.iface.setCurrentText(cur)
        self.iface.blockSignals(False)
        self.iface.setToolTip("CAN interfaces connected now: " + ", ".join(i.describe() for i in found)
                              if found else "No CAN adapter connected. Plug it in, then open this list "
                              "or press ⟳ to look again.")
        match = next((i for i in found if i.name == cur), None)
        if sync_bitrate and match and match.bitrate:
            idx = self.bitrate.findData(match.bitrate)
            if idx >= 0:
                self.bitrate.setCurrentIndex(idx)

    def _iface_list_opening(self):
        self._refresh_interfaces(sync_bitrate=False)
        if self.iface.count() == 0:
            self.statusBar().showMessage(interfaces.no_adapter_hint().split("\n")[0], 10000)

    def _source_changed(self, *_):
        kind = self.source.currentData()
        hw = kind == connection.SOCKETCAN
        for w in (self.iface, self.conn_panel.iface_label, self.rescan_btn, self.bitrate, self.bitrate_label,
                  self.setup_btn, self.conn_panel.finder_box):
            w.setVisible(hw)
        self.listen_only.setVisible(kind != connection.REPLAY)
        self._link_checked = 0.0            # re-check the adapter lamp on the next tick

    def _listen_only_toggled(self, on):
        if self.ctx.session is not None and not self.ctx.session.offline:
            session = self.ctx.session
            session.listen_only = on or not session.backend.can_transmit
            self.conn_label.setText(self.ctx.connection.describe() + ("  [listen-only]" if session.listen_only else ""))
            self.network.claim_btn.setEnabled(not session.listen_only)

    def setup_interface(self):
        self.conn_panel.stop_finder()       # it owns the interface while running
        name = self.iface.currentText().strip()
        if pythoncan.is_spec(name):
            QtWidgets.QMessageBox.information(self, "Set up interface", PYTHON_CAN_SETUP.format(
                what="Set up", name=name))
            return
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
            ok, msg = interfaces.bring_up(name, bitrate, self.listen_only.isChecked())
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

    def show_simulators(self, on: bool, save: bool = True):
        """Connection > Show simulators (off by default, to keep Source short)."""
        if save:
            from ..core.settings import Settings
            Settings().set("show_simulators", on)
        self.simulators_action.blockSignals(True)
        self.simulators_action.setChecked(on)
        self.simulators_action.blockSignals(False)
        self.conn_panel.set_simulators(on)

    def find_python_can(self):
        """Connection > Find other adapters: what python-can can open (PEAK, Kvaser, gs_usb ...)."""
        if not pythoncan.available():
            QtWidgets.QMessageBox.information(self, "Find other adapters", pythoncan.INSTALL_HINT)
            return
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._python_can_found = pythoncan.detect()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        found = self._python_can_found
        if found:
            self._refresh_interfaces(preferred=found[0])
            self.source.setCurrentIndex(self.source.findData(connection.SOCKETCAN))
        QtWidgets.QMessageBox.information(
            self, "Find other adapters",
            ("Found: " + ", ".join(found) + ".\n\nThe first is picked under Interface: choose the bitrate and "
             "Connect.") if found else
            "python-can found no adapters. Is the adapter plugged in, and its vendor driver installed (PEAK, "
            "Kvaser, Vector ...)? You can also type one under Interface as interface:channel, e.g. "
            "pcan:PCAN_USBBUS1 or slcan:/dev/ttyACM0.")

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

    def _show_conn_panel(self):
        self.conn_panel.show()
        self.conn_panel.raise_()

    def _toggle_finder(self):
        self._show_conn_panel()
        self.conn_panel.toggle_finder()

    def _finder_locked(self, bitrate):
        """The signal finder heard steady traffic (it set the bitrate box and saved it already)."""
        where = f"{bitrate // 1000}k" if bitrate else self.iface.currentText()
        self.statusBar().showMessage(f"Signal found at {where}", 8000)
        self._refresh_interfaces(preferred=self.iface.currentText(), sync_bitrate=False)
        if self.conn_panel.connect_when_found.isChecked():
            self.connect()

    def toggle_connect(self):
        if self.ctx.connection is not None or (self.ctx.session and self.ctx.session.offline):
            self.disconnect()
        else:
            self.connect()

    def _connect_on_startup(self):
        """'Connect on startup' without an error dialog on every launch when the adapter isn't
        plugged in: just say so in the status bar."""
        self._refresh_interfaces()
        name = self.iface.currentText()
        if not pythoncan.is_spec(name) and not any(i.name == name for i in interfaces.list_interfaces()):
            self.statusBar().showMessage("Not connected: " + interfaces.no_adapter_hint().split("\n")[0], 15000)
            return
        self.connect()

    def _ensure_up(self, name: str) -> bool:
        """Bring a real SocketCAN interface up at the selected bitrate if it's down, so Connect
        works in one click without a trip to the terminal. A USB adapter like the CANable comes
        up 'down' on every plug-in; this configures it (using the passwordless rule if installed,
        else one graphical prompt). Returns False only if bring-up was needed and failed."""
        if name.startswith("vcan"):
            return True
        info = next((i for i in interfaces.list_interfaces() if i.name == name), None)
        if info is None or info.state == "up":
            return True                     # missing: let open_connection give the 'plugged in?' error
        bitrate = self.bitrate.currentData()
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if bitrate == AUTO_BITRATE:
                bitrate = self._detect_bitrate(name)
                if bitrate is None:
                    return False
            ok, msg = interfaces.bring_up(name, bitrate, self.listen_only.isChecked())
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if not ok:
            QtWidgets.QMessageBox.critical(
                self, "Connect", f"Couldn't bring {name} up:\n{msg}\n\nIf it mentions a password, set up "
                "Settings → passwordless CAN access once. If it mentions listen-only, untick it.")
            return False
        self.statusBar().showMessage(f"{name} is up at {bitrate // 1000}k", 4000)
        self._refresh_interfaces(preferred=name)
        return True

    def connect(self, kind=None):
        self.conn_panel.stop_finder()       # (from the menu / shortcut while it runs)
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
        if kind == connection.OBD_SIM:
            self._use_protocol(profiles.OBD2)
        elif kind in (connection.SIMULATOR, connection.PROFILE_SIM):
            self._use_protocol(profiles.J1939)
        if kind == connection.SOCKETCAN:
            if channel not in [i.name for i in interfaces.list_interfaces()]:
                self._refresh_interfaces()          # plugged in since the list was filled?
                channel = self.iface.currentText().strip() or "can0"
            if not self._ensure_up(channel):
                return
        try:
            conn = open_connection(kind, channel, listen_only=self.listen_only.isChecked(),
                                   profile=profiles.active(), bitrate=self.bitrate.currentData())
        except Exception as e:
            missing = kind == connection.SOCKETCAN and not pythoncan.is_spec(channel) and not any(
                i.name == channel for i in interfaces.list_interfaces())
            hint = ("\n\n" + interfaces.no_adapter_hint() + "\n\nA different kind of adapter (PEAK, Kvaser, "
                    "Vector ...)? Try Connection → Find other adapters.") if missing else ""
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

    def _install_sudoers(self):
        if interfaces.sudoers_installed():
            QtWidgets.QMessageBox.information(self, "Passwordless CAN access",
                                              "Already set up — bringing up a CAN interface won't ask "
                                              "for a password.")
            return
        import getpass
        user = getpass.getuser()
        if QtWidgets.QMessageBox.question(
                self, "Set up passwordless CAN access",
                f"This adds a system rule so your user ({user}) can bring CAN interfaces up without "
                "a password.\n\nIt only permits 'ip link set canN ...' as root — nothing else — and "
                "stores no password anywhere. You'll be asked for your password once now to install "
                "it.\n\nProceed?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Cancel) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, msg = interfaces.install_sudoers(user)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        box = QtWidgets.QMessageBox.information if ok else QtWidgets.QMessageBox.warning
        box(self, "Passwordless CAN access", msg)

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

    def edit_sound(self):
        s = self.settings
        dlg = SoundDialog(s.value("sound/speaker", "true") in (True, "true"),
                          str(s.value("sound/buzzer", "auto")), self)
        if dlg.exec():
            s.setValue("sound/speaker", dlg.speaker.isChecked())
            s.setValue("sound/buzzer", dlg.buzzer())

    def edit_keys(self):
        if KeybindDialog(self.hotkeys, self).exec():
            self.log_panel._sync()

    def _obd_reports(self) -> dict:
        sn = self.obd.model.sniffer
        if self._protocol != profiles.OBD2 or not (sn.dtcs or sn.values or sn.info):
            return {}
        return {"obd_report.txt": report.build_obd_report(sn, system=self.ctx.units)}

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

    def _open_uds(self):
        from .uds_dialog import UdsDialog
        UdsDialog(self.ctx, self).exec()

    def _session_changed(self, session):
        # Fresh transmitter per connection: transmit mode is always off again on a new bus.
        self.transmitter = transmit.Transmitter(session)
        live = self.ctx.live
        offline = session is not None and session.offline
        self.connect_btn.setText("Disconnect" if (live or offline) else "Connect")
        self._bus_session, self._bus_level, self._bus_was_ok = session, link.OFF, False
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
        if self._protocol == profiles.OBD2:
            self.obd.tick()
        else:
            self.live.sample()
        self.log_panel.refresh()
        for fn in self._plugin_refresh:
            try:
                fn()
            except Exception as e:
                self.statusBar().showMessage(f"Plugin error: {e}", 5000)
        self._update_link_lamps(s)
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
            who = f"{len(self.obd.model.sniffer.modules())} module(s)" if self._protocol == profiles.OBD2 \
                else f"{len(s.ecus)} ECU(s)"
            self.traffic_label.setText(f"{getattr(self, '_fps', 0):.0f} frames/s  ~{self._bus_load:.0f}% load  {who}")
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

    def _update_link_lamps(self, session):
        """Adapter lamp (re-checked once a second) and bus lamp, in the status bar and the
        Connection sidebar. While connected, a chime when traffic drops out or comes back, so
        someone moving probes hears it without looking."""
        now = time.time()
        kind = self.source.currentData()
        if self.ctx.connection is not None:
            kind = self.ctx.connection.kind
        if now - self._link_checked >= LINK_CHECK_SECS:
            self._link_checked = now
            self._adapter = link.adapter_status(kind, self.iface.currentText().strip())
        adapter = self._adapter
        if self.conn_panel.finding:
            bus = link.Status(link.WAIT, "Bus: finding signal…", "The signal finder is listening; see Connection.")
        elif self._protocol == profiles.OBD2 and session is not None and not session.offline:
            bus = link.bus_status(session, now, last_answer=self.obd.model.sniffer.last_answer_t,
                                  polling=self.obd.polling)
        else:
            bus = link.bus_status(session, now)
        self.adapter_lamp.set_status(adapter.level, adapter.text, adapter.detail)
        self.bus_lamp.set_status(bus.level, bus.text, bus.detail)
        self.conn_panel.show_status(adapter, bus)
        if session is None or session is not self._bus_session or not self.ctx.live:
            return
        if bus.level != link.OK and now - session.started_at < link.QUIET_SECS:
            return                  # just connected: the first frames haven't had time to arrive
        was, self._bus_level = self._bus_level, bus.level
        if bus.level == link.IDLE or was == link.IDLE:
            pass                    # a gatewayed car going quiet between requests (or asked again): fine
        elif was == link.OK and bus.level != link.OK:
            self.conn_panel.cue("lost", link_status=True)
        elif was in (link.WAIT, link.BAD) and bus.level == link.OK:
            # Connected first, probes moved after: the first good traffic is "found", later ones "back".
            self.conn_panel.cue("back" if self._bus_was_ok else "found", link_status=True)
        if bus.level == link.OK:
            self._bus_was_ok = True

    # ------------------------------------------------------------------
    def _toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _about(self):
        QtWidgets.QMessageBox.about(
            self, "About CANopener",
            f"<h3>CANopener {__version__}</h3><p>J1939 diagnostics for equipment and vehicles.</p>"
            "<p><b>Reads are free; commands ask first.</b> Reading sends standard requests (J1939, OBD-II, "
            "UDS) and transport-protocol flow control. Clearing codes, on-board tests and module resets "
            "each ask before sending. Tick Listen-only to send nothing.</p>"
            "<p>GPL-2.0-or-later. Generic OBD-II code descriptions from python-OBD.</p>"
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
            if self.update_on_start.isChecked():
                QtCore.QTimer.singleShot(3000, self._startup_update_check)

    # ------------------------------------------------------------------
    # updates
    # ------------------------------------------------------------------
    def check_updates(self):
        """Help > Check for updates (also where the startup check's result goes)."""
        from .update_dialog import UpdateDialog
        UpdateDialog(self.restart_app, self, status=self._update_status).exec()
        self._update_status = None
        self.update_action.setText("Check for &updates…")

    def _startup_update_check(self):
        """Opt-in (Help > Check for updates at startup), in the background. Only speaks up when
        there is an update: offline in the field is normal, so a failed check says nothing."""
        import threading

        from ..core import updates

        def work():
            s = updates.check()
            if s.available:
                self._update_found.emit(s)
        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _show_update_found(self, status):
        self._update_status = status
        self.update_action.setText(f"Update available ({status.latest or 'new'}): install…")
        self.statusBar().showMessage(f"{status.message} Help > Update available to install it.", 30000)

    def _switch_to_handheld(self, on: bool):
        """Settings > Handheld (Pi) screen: save the choice and become the Pygame UI."""
        if not on:
            return
        from .. import frontends
        from ..core.settings import Settings
        ok, why = frontends.available(frontends.DEVICE)
        if not ok:
            QtWidgets.QMessageBox.information(self, "Handheld (Pi) screen", why)
        elif QtWidgets.QMessageBox.question(
                self, "Handheld (Pi) screen",
                "Switch to the handheld's touch UI? CANopener closes and reopens as the Raspberry Pi "
                "screen, in a window here (the mouse is your finger, the keyboard the buttons: F1-F5, "
                "arrows, Enter, mouse wheel for the encoder).\n\nIt opens that way every time until you "
                "pick System > Desktop app instead on the handheld.") == QtWidgets.QMessageBox.StandardButton.Yes:
            settings = Settings()
            settings.set("ui", frontends.DEVICE)
            self.switch_to, self.was_fullscreen = frontends.DEVICE, self.isFullScreen()
            if self.close():
                QtWidgets.QApplication.instance().quit()
                return
            settings.set("ui", frontends.DESKTOP)           # closing was cancelled (unsaved capture)
            self.switch_to = None
        self.handheld_action.blockSignals(True)
        self.handheld_action.setChecked(False)
        self.handheld_action.blockSignals(False)

    def restart_app(self) -> bool:
        """Close (asking about an unsaved capture as usual), then start the new version: under the
        Pi's systemd service by exiting with the code it restarts on, else a fresh process."""
        from ..core import updates
        if not self.close():
            return False
        if updates.under_systemd():
            QtWidgets.QApplication.instance().exit(updates.RESTART_EXIT_CODE)
        else:
            cmd = updates.restart_command()
            from ..core import config  # (start it from the app's folder: see config.install_dir)
            QtCore.QProcess.startDetached(cmd[0], cmd[1:], config.install_dir())
            QtWidgets.QApplication.instance().quit()
        return True

    def closeEvent(self, ev):
        if not self.log_panel.confirm_unsaved():
            ev.ignore()
            return
        QtWidgets.QApplication.instance().removeEventFilter(self.hotkeys)
        self.conn_panel.stop_finder()
        self._save_settings()
        self.timer.stop()
        if self.ctx.connection is not None:
            self.ctx.connection.close()
        super().closeEvent(ev)
