"""
Log manager: CSV logging with session folder management.
All frames are logged; SA is tagged for post-filtering.
"""

import csv
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from .settings import get_session_dir, new_log_filename


LOG_HEADER = ["timestamp", "sa_hex", "arb_id_hex", "pgn_hex",
              "sa", "da", "priority", "dlc", "data_hex", "pgn_name"]


class LogManager:
    def __init__(self, settings: dict):
        self.settings   = settings
        self._file      = None
        self._writer    = None
        self._path      = None
        self.logging    = False
        self._session_dir: Optional[Path] = None
        self._last_file:   Optional[Path] = None
        self._new_log_requested        = False
        self._first_log_this_session   = True

    def _ensure_session_dir(self) -> Path:
        if self._session_dir is None:
            self._session_dir = get_session_dir(self.settings.get("log_path"))
        return self._session_dir

    def start(self, new_file: bool = False):
        """Start logging. new_file=True forces a new file."""
        if self.logging:
            return

        session = self._ensure_session_dir()

        if new_file or self._first_log_this_session or self._last_file is None:
            fname = new_log_filename()
            self._path = session / fname
            self._first_log_this_session = False
        else:
            # Append to last file
            self._path = self._last_file

        append = self._path.exists()
        self._file   = open(self._path, "a", newline="")
        self._writer = csv.writer(self._file)
        if not append:
            self._writer.writerow(LOG_HEADER)
        self.logging = True

    def stop(self):
        if not self.logging:
            return
        self.logging = False
        self._last_file = self._path
        if self._file:
            self._file.flush()
            self._file.close()
            self._file   = None
            self._writer = None

    def write_frame(self, frame, pgn: int, sa: int, da: int,
                    priority: int, pgn_name: str):
        if not self.logging or self._writer is None:
            return
        ts = datetime.fromtimestamp(frame.timestamp).strftime(
            "%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._writer.writerow([
            ts,
            f"0x{sa:02X}",
            f"0x{frame.arb_id:08X}",
            f"0x{pgn:04X}",
            sa, da, priority,
            len(frame.data),
            " ".join(f"{b:02X}" for b in frame.data),
            pgn_name,
        ])

    @property
    def current_path(self) -> str:
        return str(self._path) if self._path else "—"

    def new_log(self):
        """Force next start() to create a new file."""
        was_logging = self.logging
        if was_logging:
            self.stop()
        self._last_file = None
        if was_logging:
            self.start(new_file=True)
