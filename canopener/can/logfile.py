"""CAN log files: reading, writing (candump), and real-time replay.

Written logs are always candump ``-l`` format. Read as well: candump's screen formats, Vector ASC
(CANalyzer / CANoe text logs, hex) and PEAK TRC (PCAN-View, versions 1.x and 2.x). ASC times are
seconds from the start of the log; TRC times become wall-clock when the file gives its start time.
CAN FD and error frames in those files are skipped.
"""

import datetime
import re
import threading
import time

from .backends import CanBackend
from .frame import CanFrame

# candump -l:          (1700000000.123456) can0 18FECA00#0000FFFF00000000
LOG_RE = re.compile(r"\((\d+(?:\.\d+)?)\)\s+\S+\s+([0-9A-Fa-f]{3,8})#(R|[0-9A-Fa-f]*)")
# candump (screen):    can0  18FECA00   [8]  00 00 FF FF 00 00 00 00
# candump -ta:         (1700000000.123456)  can0  18FECA00   [8]  00 00 ...
SCREEN_RE = re.compile(
    r"^\s*(?:\((\d+(?:\.\d+)?)\)\s+)?\S+\s+([0-9A-Fa-f]{3,8})\s+\[(\d)\]\s*((?:[0-9A-Fa-f]{2}\s*)*)")

# Vector ASC:    1.234567 1  18FEF100x       Rx   d 8 FF FF FF 68 13 FF FF FF   (x = 29-bit ID)
ASC_RE = re.compile(r"^\s*(\d+\.\d+)\s+\d+\s+([0-9A-Fa-f]{1,8})(x?)\s+(?:Rx|Tx)\s+d\s+(\d)\s*((?:[0-9A-Fa-f]{2}\s*)*)",
                    re.IGNORECASE)
# PEAK TRC 1.x:      1)      1234.5  Rx         18FEF100  8  FF FF ...      (time in ms; Rx/Tx from 1.1)
TRC1_RE = re.compile(r"^\s*\d+\)\s+(\d+(?:\.\d+)?)\s+(?:(?:Rx|Tx)\s+)?([0-9A-Fa-f]{4,8})\s+(\d)\s+"
                     r"((?:[0-9A-Fa-f]{2}\s*)*)$")
# PEAK TRC 2.x:      1      1234.567 DT     18FEF100 Rx 8  FF ...   (2.1 adds a bus number and a "-")
TRC2_RE = re.compile(r"^\s*\d+\s+(\d+(?:\.\d+)?)\s+DT\s+(?:\d+\s+)?([0-9A-Fa-f]{4,8})\s+(?:Rx|Tx)\s+(?:-\s+)?"
                     r"(\d)\s*((?:[0-9A-Fa-f]{2}\s*)*)$")
# ;$STARTTIME=45678.5208333      days since 1899-12-30 (the spreadsheet date), local time
TRC_START_RE = re.compile(r"^;\s*\$STARTTIME=(\d+(?:\.\d+)?)")
_DAYS_TO_1970 = 25569

# Header/marker comment lines some field loggers write into the .log (lines starting with '#',
# which the frame parsers ignore). Times are ISO-8601 wall clock, e.g.
#   # === LIVE CAPTURE START: 2026-05-08T17:34:45.381308 ===
#   # === MARKER 2026-05-08T17:35:23.658860  ===  (optionally followed by a label)
#   # === LIVE CAPTURE END: 2026-05-08T17:35:33.134745 (47.8s, 67416 frames) ===
_ISO = r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
MARKER_RE = re.compile(r"#.*\bMARKER\b[:\s]*" + _ISO + r"\s*(.*?)\s*=*\s*$", re.IGNORECASE)
START_RE = re.compile(r"#.*\bSTART\b[:\s]*" + _ISO, re.IGNORECASE)
END_RE = re.compile(r"#.*\bEND\b[:\s]*" + _ISO, re.IGNORECASE)


def _iso_seconds(iso: str) -> float:
    """ISO-8601 time as naive seconds, used only for differences between two such times."""
    return datetime.datetime.fromisoformat(iso).timestamp()


def parse_line(line: str, line_no: int = 0) -> CanFrame | None:
    m = LOG_RE.search(line)
    if m:
        ts, ids, hx = m.groups()
        if hx == "R":
            return None
        return CanFrame(int(ids, 16), bytes.fromhex(hx)[:8], len(ids) > 3, float(ts))
    m = SCREEN_RE.search(line)
    if m:
        ts, ids, dlc, hx = m.groups()
        data = bytes.fromhex("".join(hx.split()))[:int(dlc)]
        t = float(ts) if ts else line_no * 0.001
        return CanFrame(int(ids, 16), data, len(ids) > 3, t)
    return _parse_other(line)


def _parse_other(line: str, trc_start: float = 0.0) -> CanFrame | None:
    """A frame from a Vector ASC or PEAK TRC line. ``trc_start`` is the TRC file's start time as
    epoch seconds (0 = unknown: times count from the start of the log)."""
    m = ASC_RE.match(line)
    if m:
        ts, ids, ext, dlc, hx = m.groups()
        return CanFrame(int(ids, 16), bytes.fromhex("".join(hx.split()))[:int(dlc)], bool(ext), float(ts))
    m = TRC2_RE.match(line) or TRC1_RE.match(line)
    if m:
        ms, ids, dlc, hx = m.groups()
        return CanFrame(int(ids, 16), bytes.fromhex("".join(hx.split()))[:int(dlc)], len(ids) > 4,
                        trc_start + float(ms) / 1000)
    return None


def read_log(path: str) -> list[CanFrame]:
    return read_log_with_markers(path)[0]


def read_log_with_markers(path: str) -> tuple[list[CanFrame], list[tuple[float, str]]]:
    """Frames plus any ``# === MARKER ... ===`` comment lines, as ``(epoch, label)``.

    Field loggers timestamp frames with the epoch clock but write their comment lines in
    local wall-clock ISO, so the two can't be compared directly. The marker, START and END
    ISO times do share one clock, though, so we anchor that comment timeline onto the frame
    epochs: END ISO lines up with the last frame (most accurate), else START ISO with the
    first. Differences between ISO times are timezone-independent, so markers land on the
    right frame wherever the log is opened. Logs without these comments return no markers.
    """
    frames: list[CanFrame] = []
    markers_iso: list[tuple[str, str]] = []
    start_iso = end_iso = None
    trc_start = 0.0
    with open(path, errors="replace") as f:
        for i, line in enumerate(f):
            fr = parse_line(line, i)
            if fr:
                if trc_start and fr.timestamp < 1e8:        # a TRC frame: put it on the wall clock
                    fr = CanFrame(fr.can_id, fr.data, fr.extended, fr.timestamp + trc_start)
                frames.append(fr)
                continue
            if (m := TRC_START_RE.match(line)):
                trc_start = (float(m.group(1)) - _DAYS_TO_1970) * 86400
                continue
            if (m := MARKER_RE.search(line)):
                markers_iso.append((m.group(1), m.group(2).strip().strip("=").strip()))
            elif (m := START_RE.search(line)):
                start_iso = m.group(1)
            elif (m := END_RE.search(line)):
                end_iso = m.group(1)

    markers: list[tuple[float, str]] = []
    if frames and markers_iso and (end_iso or start_iso):
        if end_iso:
            anchor_iso, anchor_epoch = end_iso, frames[-1].timestamp
        else:
            anchor_iso, anchor_epoch = start_iso, frames[0].timestamp
        base = _iso_seconds(anchor_iso)
        for n, (iso, label) in enumerate(markers_iso, 1):
            markers.append((anchor_epoch + (_iso_seconds(iso) - base), label or f"Marker {n}"))
    return frames, markers


def format_line(frame: CanFrame, channel: str = "can0") -> str:
    return f"({frame.timestamp:.6f}) {channel} {frame.id_str()}#{frame.data.hex().upper()}"


class LogWriter:
    """Thread-safe candump -l writer."""

    def __init__(self, path: str, channel: str = "can0"):
        self.path = path
        self.channel = channel or "can0"
        self._f = open(path, "w", buffering=1 << 16)
        self._lock = threading.Lock()
        self.count = 0
        self._last_flush = time.monotonic()

    def write(self, frame: CanFrame):
        if frame.error:
            return
        with self._lock:
            if self._f is None:
                return
            self._f.write(format_line(frame, self.channel) + "\n")
            self.count += 1
            now = time.monotonic()
            if now - self._last_flush > 1.0:
                self._f.flush()
                self._last_flush = now

    def close(self):
        with self._lock:
            if self._f:
                self._f.close()
                self._f = None


def write_log(path: str, frames, channel: str = "can0") -> int:
    n = 0
    with open(path, "w") as f:
        for fr in frames:
            if fr.error:
                continue
            f.write(format_line(fr, channel) + "\n")
            n += 1
    return n


class LogReplayBackend(CanBackend):
    """Plays a log back with its original timing (scaled by ``speed``). Receive-only."""

    kind = "replay"
    can_transmit = False

    def __init__(self, path: str, speed: float = 1.0, loop: bool = False):
        super().__init__(path)
        self.path, self.speed, self.loop = path, max(speed, 0.01), loop
        self.frames: list[CanFrame] = []
        self.finished = False
        self._i = 0
        self._t0 = 0.0
        self._log_t0 = 0.0

    def open(self):
        self.frames = read_log(self.path)
        if not self.frames:
            raise OSError(f"No CAN frames found in {self.path}")
        self._restart()

    def _restart(self):
        self._i = 0
        self._t0 = time.time()
        self._log_t0 = self.frames[0].timestamp

    def close(self):
        self.frames = []

    def send(self, frame):
        raise OSError("log replay is receive-only")

    @property
    def progress(self) -> float:
        return self._i / len(self.frames) if self.frames else 1.0

    def recv(self):
        if self._i >= len(self.frames):
            if not self.loop:
                self.finished = True
                time.sleep(0.2)
                return None
            self._restart()
        fr = self.frames[self._i]
        due = self._t0 + (fr.timestamp - self._log_t0) / self.speed
        wait = due - time.time()
        if wait > 0.2:
            time.sleep(0.2)
            return None
        if wait > 0:
            time.sleep(wait)
        self._i += 1
        return CanFrame(fr.can_id, fr.data, fr.extended, time.time())
