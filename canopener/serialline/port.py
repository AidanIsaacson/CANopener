"""Serial ports through pyserial: list them, open one, and read it on a thread.

pyserial is the optional ``serial`` extra (the installers include it). It's imported only here,
and only when a port is listed or opened, like python-can in ``can/pythoncan.py``. It works the
same on Linux, Windows and macOS, takes any baud rate and parity the adapter can do, and also
opens pyserial URLs (``loop://`` for tests, ``rfc2217://host:port`` for a serial server).

A ``Tap`` reads one port on its own thread and stamps each chunk of bytes with ``time.time()`` on
arrival: the wall clock the CAN frames and meter readings use, so a capture lines them all up.
The stamp is when the USB adapter handed the bytes over, not when each byte crossed the wire:
USB adapters batch bytes (an FTDI waits up to its latency timer, 16 ms by default, before passing
a part-filled buffer on). ``latency_ms`` reads that setting on Linux; 1 ms keeps messages apart.

A ``Sniffer`` is a set of taps (one per direction of a link) feeding the same listeners.

Writing is off unless the tap was opened with ``allow_write=True``; front-ends confirm each
send with the user first (CLAUDE.md), and nothing here decides what to send.
"""

import glob
import importlib.util
import os
import threading
import time
from dataclasses import dataclass

from .framing import Framing

INSTALL_HINT = ("pyserial isn't installed. Run the installer again to add it "
                "(~/CANopener/deploy/linux/install.sh), or: pip install pyserial")


def available() -> bool:
    return importlib.util.find_spec("serial") is not None


class SerialError(Exception):
    pass


@dataclass
class Chunk:
    t: float            # wall-clock arrival time (time.time())
    line: str           # which line it came in on ("A", "console", ...)
    data: bytes


@dataclass
class PortInfo:
    device: str
    description: str = ""
    hwid: str = ""
    serial_number: str = ""


def list_ports() -> list[PortInfo]:
    """Serial ports on this computer (USB adapters, built-in UARTs). Empty without pyserial."""
    if not available():
        return []
    from serial.tools import list_ports as lp
    out = []
    for p in sorted(lp.comports(), key=lambda p: p.device):
        desc = "" if p.description in ("n/a", p.device) else p.description
        out.append(PortInfo(p.device, desc or "", p.hwid or "", getattr(p, "serial_number", "") or ""))
    return out


def by_id(device: str) -> str:
    """The stable /dev/serial/by-id name of a Linux port, when it has one (ttyUSB numbers change
    with plug-in order; the by-id name follows the adapter)."""
    real = os.path.realpath(device)
    for p in glob.glob("/dev/serial/by-id/*"):
        if os.path.realpath(p) == real:
            return p
    return device


def latency_ms(device: str) -> int | None:
    """An FTDI-type adapter's latency timer on Linux (ms), or None when there isn't one to read.
    Only looks: changing it is ``echo 1 > /sys/bus/usb-serial/devices/ttyUSB0/latency_timer``."""
    name = os.path.basename(os.path.realpath(device))
    try:
        with open(f"/sys/bus/usb-serial/devices/{name}/latency_timer") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def open_port(device: str, framing: Framing, timeout: float = 0.05):
    """Open ``device`` (a port name or a pyserial URL) with ``framing``. Raises SerialError."""
    if not available():
        raise SerialError(INSTALL_HINT)
    import serial
    try:
        return serial.serial_for_url(device, baudrate=framing.baud, bytesize=framing.bytesize,
                                     parity=framing.parity, stopbits=framing.stopbits, timeout=timeout)
    except (serial.SerialException, ValueError, OSError) as e:
        raise SerialError(f"can't open {device}: {e}") from e


class Tap:
    """Reads one serial port on a thread; every chunk goes to ``on_chunk(Chunk)``.

    ``port`` is anything with pyserial's ``read`` / ``in_waiting`` / ``close`` (a test passes a
    fake). Errors stop the thread and land in ``error``; ``on_error(tap, message)`` hears them.
    """

    def __init__(self, line: str, port, *, device: str = "", framing: Framing | None = None,
                 label: str = "", allow_write: bool = False, on_chunk=None, on_error=None):
        self.line = line
        self.port = port
        self.device = device
        self.framing = framing
        self.label = label
        self.allow_write = allow_write
        self.on_chunk = on_chunk
        self.on_error = on_error
        self.error = ""
        self.bytes_in = 0
        self.bytes_out = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def open(cls, line: str, device: str, framing: Framing, **kw) -> "Tap":
        return cls(line, open_port(device, framing), device=device, framing=framing, **kw)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"serial-{self.line}", daemon=True)
            self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        while not self._stop.is_set():
            try:
                waiting = getattr(self.port, "in_waiting", 0) or 1
                data = self.port.read(waiting)
            except Exception as e:      # noqa: BLE001 - an unplugged adapter raises all sorts
                if not self._stop.is_set():
                    self.error = f"{self.device or self.line}: {e}"
                    if self.on_error:
                        self.on_error(self, self.error)
                return
            if data:
                t = time.time()
                self.bytes_in += len(data)
                if self.on_chunk:
                    self.on_chunk(Chunk(t, self.line, bytes(data)))

    def write(self, data: bytes) -> int:
        """Send ``data``. Refused unless the tap was opened to write; the caller confirms first."""
        if not self.allow_write:
            raise SerialError(f"line {self.line} is listen-only")
        n = self.port.write(bytes(data)) or 0
        self.bytes_out += n
        return n

    def close(self):
        self._stop.set()
        try:
            cancel = getattr(self.port, "cancel_read", None)
            if cancel:
                cancel()
        except Exception:               # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            self.port.close()
        except Exception:               # noqa: BLE001
            pass

    def describe(self) -> dict:
        d = {"line": self.line, "port": self.device, "framing": str(self.framing) if self.framing else ""}
        if self.label:
            d["label"] = self.label
        return d


class Sniffer:
    """Several taps (one per direction) feeding the same listeners: ``fn(Chunk)``, called on the
    taps' threads. ``lines()`` describes them for a capture's ``capture.json``."""

    def __init__(self, taps: list[Tap] | None = None):
        self.taps: list[Tap] = []
        self._listeners: list = []
        self._lock = threading.Lock()
        for tap in taps or []:
            self.add(tap)

    @classmethod
    def open(cls, lines: list[tuple[str, str]], framing: Framing, labels: dict | None = None,
             allow_write: bool = False) -> "Sniffer":
        """[(line name, port)] all at one framing. Closes what it opened if one port fails."""
        s = cls()
        try:
            for name, device in lines:
                s.add(Tap.open(name, device, framing, label=(labels or {}).get(name, ""),
                               allow_write=allow_write))
        except SerialError:
            s.close()
            raise
        return s

    def add(self, tap: Tap):
        tap.on_chunk = self._chunk
        self.taps.append(tap)

    def tap(self, line: str) -> Tap | None:
        return next((t for t in self.taps if t.line == line), None)

    def add_listener(self, fn):
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn):
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _chunk(self, c: Chunk):
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            fn(c)

    def start(self):
        for t in self.taps:
            t.start()

    def close(self):
        for t in self.taps:
            t.close()

    def errors(self) -> list[str]:
        return [t.error for t in self.taps if t.error]

    def lines(self) -> list[dict]:
        return [t.describe() for t in self.taps]
