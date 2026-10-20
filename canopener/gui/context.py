"""Shared state for the GUI: the current session, the last scan result, display settings."""

import queue
import threading
import traceback

from ..core import scan, units
from ..core.connection import Connection
from ..core.scan import ScanResult
from ..core.session import Session
from .qt import QtCore, Signal


class AppContext(QtCore.QObject):
    session_changed = Signal(object)        # Session | None
    result_ready = Signal(object)           # ScanResult
    scan_failed = Signal(str)
    busy_changed = Signal(bool)
    units_changed = Signal(str)
    profile_changed = Signal()
    show_signal = Signal(int)               # SPN -> jump to it in Live Data
    status_message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.connection: Connection | None = None
        self.session: Session | None = None
        self.result: ScanResult | None = None
        self.units = units.METRIC
        self.busy = False
        self.bitrate = 250000
        self.result_ready.connect(self._store_result)
        # Worker threads never touch Qt: they post here and a main-thread timer emits the signals.
        self._done: queue.Queue = queue.Queue()
        self._poll = QtCore.QTimer(self)
        self._poll.timeout.connect(self._drain)
        self._poll.start(50)

    def _drain(self):
        while True:
            try:
                kind, payload = self._done.get_nowait()
            except queue.Empty:
                return
            self.busy = False
            if kind == "result":
                self.result_ready.emit(payload)
            else:
                self.scan_failed.emit(payload)
            self.busy_changed.emit(False)

    def _store_result(self, result):
        self.result = result

    # --- session lifecycle ---
    def set_connection(self, conn: Connection | None):
        """Switch to a new live connection (None = disconnect), closing the old one."""
        if self.connection is not None:
            self.connection.close()
        self.connection = conn
        self.session = conn.session if conn else None
        self.session_changed.emit(self.session)

    def set_offline_session(self, session: Session, result: ScanResult):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.session = session
        self.session_changed.emit(session)
        self.result_ready.emit(result)

    def set_units(self, system: str):
        if system != self.units:
            self.units = system
            self.units_changed.emit(system)

    @property
    def live(self) -> bool:
        return self.session is not None and not self.session.offline

    # --- scans ---
    def run_preset(self, preset: str):
        if self.busy or not self.live:
            return False
        session = self.session
        self.busy = True
        self.busy_changed.emit(True)
        self.status_message.emit(f"{scan.PRESETS[preset][0]}: requesting...")

        def work():
            try:
                self._done.put(("result", scan.run_preset(session, preset)))
            except Exception:
                self._done.put(("error", traceback.format_exc(limit=3)))

        threading.Thread(target=work, name="scan", daemon=True).start()
        return True

    def request_pgn(self, pgn: int, da: int = 0xFF) -> bool:
        return bool(self.live and self.session.request(pgn, da))
