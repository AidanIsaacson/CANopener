"""ELM327-compatible OBD adapters (Bluetooth, Wi-Fi or USB dongles) as a request/answer backend.

An ELM327 is not a CAN interface: it's a small scan tool spoken to in text. You send ``010C``,
it builds the CAN frames, handles ISO-TP and flow control, and prints the answer as hex. This
backend makes one look like a bus to the rest of the app:

* ``send()`` takes the single-frame OBD / UDS requests ``core.obd`` and ``core.uds`` build,
  sets the adapter's header and receive filter, sends the payload as text and reads the reply
  lines (headers on, so each line is the answering module's CAN ID and frame bytes);
* each reply line becomes a received ``CanFrame``, so the session, live data, captures and the
  Log tool see the answers exactly as they would on a wired interface.

What it can't do, it refuses rather than fakes: multi-frame requests (an ELM sends single
frames only), anything that isn't an ISO-TP request (J1939 requests, raw Transmit frames), and
listening to the whole bus (the adapter only reports answers to its own requests). Flow control
frames from the clients are dropped: the adapter sends its own.

Vehicles: the CAN OBD protocols (ISO 15765-4, 11- or 29-bit, 250 or 500 kbit/s), and the older
non-CAN ones (SAE J1850 PWM / VPW, ISO 9141-2, KWP2000) for 1996-2007 cars. On those, the answers
are rebuilt in CAN form by ``obd.noncan`` and delivered on stand-in response IDs (the engine on
0x7E8, the transmission on 0x7E9 ...); ``module_label`` names the real module address.

Timestamps are taken on this computer when a reply line arrives: after a Bluetooth hop, so a
few tens of milliseconds late and jittery. Fine for live data and logs, not for timing work.

Channels (``parse_channel``): a Bluetooth address ``AA:BB:CC:DD:EE:FF`` (RFCOMM channel 1, or
``AA:BB:CC:DD:EE:FF/2``), ``tcp:192.168.0.10:35000`` for a Wi-Fi dongle, or a serial device
``/dev/rfcomm0`` / ``/dev/ttyUSB0`` (``/dev/ttyUSB0@115200`` sets the baud rate). Standard
library only: Bluetooth uses Python's own ``socket.AF_BLUETOOTH`` (Linux, BlueZ).
"""

import os
import queue
import re
import select
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from ..obd import isotp, noncan
from .backends import BackendError, CanBackend
from .frame import CanFrame

PROMPT = b">"
AT_TIMEOUT = 2.0            # an AT command answers at once
RESET_TIMEOUT = 5.0         # ATZ prints its banner after up to ~1 s
REQUEST_TIMEOUT = 6.0       # an OBD request: the adapter's own timeout (and any 'response pending' waits) first
SEARCH_TIMEOUT = 20.0       # automatic protocol search tries every protocol in turn
CONNECT_TIMEOUT = 10.0
DEFAULT_RFCOMM_CHANNEL = 1
DEFAULT_SERIAL_BAUD = 38400

# ATSP / ATDPN protocol numbers (ELM327 data sheet)
PROTOCOLS = {
    "0": "Automatic",
    "1": "SAE J1850 PWM (41.6 kbaud)",
    "2": "SAE J1850 VPW (10.4 kbaud)",
    "3": "ISO 9141-2 (5 baud init)",
    "4": "ISO 14230-4 KWP (5 baud init)",
    "5": "ISO 14230-4 KWP (fast init)",
    "6": "ISO 15765-4 CAN (11 bit, 500 kbaud)",
    "7": "ISO 15765-4 CAN (29 bit, 500 kbaud)",
    "8": "ISO 15765-4 CAN (11 bit, 250 kbaud)",
    "9": "ISO 15765-4 CAN (29 bit, 250 kbaud)",
    "A": "SAE J1939 CAN (29 bit, 250 kbaud)",
    "B": "User CAN 1 (11 bit, 125 kbaud unless ATPB changes it)",
    "C": "User CAN 2 (11 bit, 50 kbaud unless ATPB changes it)",
}
# Protocols B / C are the data sheet's defaults; a bus setup's ATPB command can change their ID length
# and bitrate, which this table doesn't follow (docs/BLUETOOTH-OBD.md, "Gaps to fill").
CAN_PROTOCOLS = {"6": (False, 500000), "7": (True, 500000), "8": (False, 250000), "9": (True, 250000),
                 "B": (False, 125000), "C": (False, 50000)}
NONCAN_PROTOCOLS = {"1", "2", "3", "4", "5"}     # J1850 PWM / VPW, ISO 9141-2, KWP2000: obd.noncan
NONCAN_TIMEOUT = 10.0       # a K-line car's first request includes waking its computer (2-3 s)
J1939_PROTOCOL = "A"        # SAE J1939 at 250k: listened to with ATMA, J1939 Requests sent raw (ATCAF0)
PF_REQUEST, PF_TPCM = 0xEA, 0xEC
TP_FLOW_CONTROL = {0x11, 0x13, 0xFF}        # TP.CM CTS, end-of-message ACK, abort: ours to send
# Requests modules often answer 'response pending' (7F xx 78) to before the real answer: clear codes,
# on-board tests, UDS clear / reset / routines. The adapter's timeout goes to its maximum (~1 s)
# for these so it keeps listening.
SLOW_SERVICES = {0x04, 0x08, 0x11, 0x14, 0x31}
SEARCH_RETRY = 5.0          # after a failed protocol search (key off), don't search again this soon
TRACE_ENV = "CANOPENER_ELM_TRACE"   # a file: every command and reply line goes there (for debugging)

NO_ANSWER = ("NO DATA", "UNABLE TO CONNECT", "STOPPED")
# Reply lines that mean the adapter saw trouble on the vehicle's bus: reported as error frames.
BUS_TROUBLE = ("CAN ERROR", "BUS ERROR", "BUS BUSY", "BUFFER FULL", "DATA ERROR", "<DATA ERROR",
               "<RX ERROR", "RX ERROR", "FB ERROR", "LP ALERT", "LV RESET")
# Commands that keep printing until any character is sent (monitoring the bus).
MONITOR_COMMANDS = ("ATMA", "ATMR", "ATMT", "ATMP", "ATDM1", "STMA", "STM ", "STMR", "STMT")
# Adapter commands the console refuses: they rewrite the adapter's stored settings or its serial
# speed (an adapter you can't reach any more), or transmit frames (those go through the
# confirm-first Transmit path instead).
CONSOLE_BLOCKED = {
    "ATPP": "rewrites the adapter's stored settings (programmable parameters)",
    "ATBRD": "changes the adapter's serial speed: the link could be lost",
    "ATBRT": "changes the adapter's serial speed: the link could be lost",
    "STSBR": "changes the adapter's serial speed: the link could be lost",
    "STBR": "changes the adapter's serial speed: the link could be lost",
    "STWBR": "writes the adapter's serial speed: the link could be lost",
    "STPX": "transmits a frame: use Transmit, which shows it and asks first",
}
_HEX = re.compile(r"^[0-9A-F]+$")
_BT_ADDR = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


@dataclass
class BusSetup:
    """How to put an adapter onto another bus: a plugin registers these (``core.adapter_buses``).
    ``commands`` are adapter commands (AT / ST only: ``console_refusal`` applies) run after the
    normal set-up, then ``protocol`` is selected; ``undo`` runs when the connection closes."""

    id: str
    label: str
    protocol: str = "0"
    commands: list = field(default_factory=list)
    undo: list = field(default_factory=list)
    adapter: str = ""           # which adapter these commands are for ("vLinker MC+")
    note: str = ""
    verified: str = ""          # how it was proven ("" = not yet)
    source: str = ""            # where it came from (a file)


class ElmError(BackendError, OSError):
    """The adapter refused or couldn't do something. An OSError too, so ``Session.send_frame``
    reports it (``health.tx_error``) like a bus that refused a frame."""


# ----------------------------------------------------------------------
# channels and byte streams
# ----------------------------------------------------------------------
def is_bt_address(text: str) -> bool:
    return bool(_BT_ADDR.match(text.strip()))


def parse_channel(spec: str) -> tuple:
    """``("bt", address, rfcomm_channel)``, ``("tcp", host, port)`` or ``("serial", path, baud)``.
    Raises ValueError for anything else."""
    spec = (spec or "").strip()
    if spec.lower().startswith("tcp:"):
        host, _, port = spec[4:].rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"expected tcp:HOST:PORT, got {spec!r}")
        return ("tcp", host, int(port))
    if spec.startswith("/dev/"):
        path, _, baud = spec.partition("@")
        if baud and not baud.isdigit():
            raise ValueError(f"bad baud rate in {spec!r}")
        return ("serial", path, int(baud) if baud else DEFAULT_SERIAL_BAUD)
    addr, _, chan = spec.partition("/")
    if is_bt_address(addr):
        if chan and not chan.isdigit():
            raise ValueError(f"bad RFCOMM channel in {spec!r}")
        return ("bt", addr.upper(), int(chan) if chan else DEFAULT_RFCOMM_CHANNEL)
    raise ValueError(f"not a Bluetooth address (AA:BB:CC:DD:EE:FF), tcp:HOST:PORT or /dev/... path: {spec!r}")


def bluetooth_supported() -> bool:
    """Does this Python have Bluetooth (RFCOMM) sockets? Linux builds with BlueZ headers do;
    Raspberry Pi OS / Debian's python3 does."""
    return hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_RFCOMM")


NO_BT_SOCKETS = ("This Python has no Bluetooth sockets. Bind the adapter to a serial device instead "
                 "(sudo rfcomm bind 0 AA:BB:CC:DD:EE:FF) and connect to /dev/rfcomm0.")


class Stream:
    """A byte stream to the adapter: a socket or a serial device."""

    def __init__(self):
        self._fd_owner = None

    def open(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def fileno(self) -> int:
        raise NotImplementedError

    def _read(self, n: int) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes):
        raise NotImplementedError

    def read_some(self, timeout: float) -> bytes | None:
        """Up to a few hundred bytes, waiting at most ``timeout``. ``b""`` on timeout; raises
        OSError when the link is gone (end of stream)."""
        try:
            r, _, _ = select.select([self.fileno()], [], [], max(0.0, timeout))
            if not r:
                return b""
            data = self._read(512)
        except ValueError as e:                     # the descriptor was closed under us
            raise OSError("the connection to the adapter is closed") from e
        if not data:
            raise OSError("the adapter closed the connection")
        return data


class SocketStream(Stream):
    """An already connected socket (the simulated adapter's socketpair, or a subclass's)."""

    def __init__(self, sock: socket.socket | None = None):
        super().__init__()
        self.sock = sock

    def open(self):
        if self.sock is None:
            raise OSError("no socket")

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def fileno(self) -> int:
        if self.sock is None:
            raise OSError("not connected")
        return self.sock.fileno()

    def _read(self, n: int) -> bytes:
        return self.sock.recv(n)

    def write(self, data: bytes):
        if self.sock is None or self.sock.fileno() < 0:
            raise OSError("not connected")
        self.sock.sendall(data)


class RfcommStream(SocketStream):
    """Classic Bluetooth serial (SPP) over an RFCOMM socket. The adapter must be paired first."""

    def __init__(self, address: str, channel: int = DEFAULT_RFCOMM_CHANNEL):
        super().__init__()
        self.address, self.channel = address, channel

    def open(self):
        if not bluetooth_supported():
            raise ElmError(NO_BT_SOCKETS)
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        s.settimeout(CONNECT_TIMEOUT)
        try:
            s.connect((self.address, self.channel))
        except OSError as e:
            s.close()
            raise OSError(e.errno, _rfcomm_problem(e, self.address)) from e
        s.settimeout(None)
        self.sock = s


def _rfcomm_problem(e: OSError, address: str) -> str:
    import errno
    if isinstance(e, TimeoutError) or e.errno in (errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ETIMEDOUT):
        return (f"{address} isn't answering: out of range, not powered (plugged into the vehicle?), or "
                "connected to another phone or tablet")
    if e.errno == errno.ECONNREFUSED:
        return f"{address} refused the connection: not paired with this computer, or not a serial (SPP) adapter"
    if e.errno in (errno.EBUSY, errno.EADDRINUSE):
        return f"{address} is busy: another program on this computer is connected to it"
    if e.errno in (errno.EACCES, errno.EPERM):
        return "No permission to use Bluetooth (is this user in the 'bluetooth' group?)"
    return f"{address}: {e.strerror or e}"


class TcpStream(SocketStream):
    """A Wi-Fi dongle (usually 192.168.0.10:35000)."""

    def __init__(self, host: str, port: int):
        super().__init__()
        self.host, self.port = host, port

    def open(self):
        s = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(None)
        self.sock = s


class SerialStream(Stream):
    """A serial device: a USB dongle, or a Bluetooth adapter bound to /dev/rfcommN."""

    def __init__(self, path: str, baud: int = DEFAULT_SERIAL_BAUD):
        super().__init__()
        self.path, self.baud = path, baud
        self.fd: int | None = None

    def open(self):
        import termios  # POSIX only; imported here so the module loads anywhere
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY)
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0                                    # iflag: no input processing
            attrs[1] = 0                                    # oflag: raw output
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0                                    # lflag: no echo, no line editing
            speed = getattr(termios, f"B{self.baud}", None)
            if speed is not None:
                attrs[4] = attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except (OSError, termios.error) as e:
            os.close(fd)
            raise OSError(f"couldn't set up {self.path}: {e}") from e
        self.fd = fd

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def fileno(self) -> int:
        if self.fd is None:
            raise OSError("not open")
        return self.fd

    def _read(self, n: int) -> bytes:
        return os.read(self.fd, n)

    def write(self, data: bytes):
        if self.fd is None:
            raise OSError("not open")
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view):]


def open_stream(spec: str) -> Stream:
    kind, a, b = parse_channel(spec)
    if kind == "bt":
        return RfcommStream(a, b)
    if kind == "tcp":
        return TcpStream(a, b)
    return SerialStream(a, b)


# ----------------------------------------------------------------------
# reply parsing (pure)
# ----------------------------------------------------------------------
def split_reply(raw: bytes, command: str = "") -> list[str]:
    """The text an adapter printed before its ``>`` prompt, as stripped non-empty lines, without
    the echoed command and the 'SEARCHING...' / 'BUS INIT' progress lines."""
    text = raw.decode("ascii", "replace").replace("\x00", "")
    out = []
    echo = command.replace(" ", "").upper()
    for line in re.split(r"[\r\n]+", text):
        line = line.strip()
        if not line or line == ">":
            continue
        up = line.upper()
        if echo and up.replace(" ", "") == echo:
            continue
        if up.startswith("SEARCHING") or up.startswith("BUS INIT"):
            continue
        out.append(line)
    return out


def parse_frame_line(line: str, extended: bool) -> tuple[int, bytes] | None:
    """``7E8064100BE3EB811`` (headers on) -> (0x7E8, bytes). Spaces are ignored. None for a line
    that isn't a frame (a message, or too short)."""
    s = line.replace(" ", "").upper()
    head = 8 if extended else 3
    if len(s) < head + 2 or not _HEX.match(s):
        return None
    body = s[head:]
    if len(body) % 2:
        return None
    data = bytes.fromhex(body)
    if not 1 <= len(data) <= 8:
        return None
    return int(s[:head], 16), data


def classify(lines: list[str]) -> str | None:
    """The adapter's message among the reply lines: 'NO DATA', 'CAN ERROR', '?' ..., or None."""
    for line in lines:
        up = line.upper().strip()
        if up == "?":
            return "?"
        for msg in NO_ANSWER + BUS_TROUBLE:
            if up.startswith(msg):
                return msg
        if up.startswith("ERR"):
            return up
    return None


def console_refusal(cmd: str) -> str:
    """'' if ``cmd`` may be sent from the adapter console, else why not. Only adapter commands
    (AT..., ST...) are allowed: requests to the vehicle go through the OBD / UDS paths, which ask
    before anything that changes it."""
    c = cmd.replace(" ", "").upper()
    if not c:
        return "empty"
    if not (c.startswith("AT") or c.startswith("ST")):
        return ("Only adapter commands (AT..., ST...) here. Requests to the vehicle go through obd / uds, "
                "which ask before anything that changes it.")
    for prefix, why in CONSOLE_BLOCKED.items():
        if c.startswith(prefix):
            return f"{prefix} {why}."
    return ""


def is_monitor_command(cmd: str) -> bool:
    c = cmd.upper().strip()
    return any(c.replace(" ", "").startswith(m.strip()) for m in MONITOR_COMMANDS)


def is_physical(can_id: int, extended: bool) -> bool:
    """A request to one module (not the functional 'everyone' address)?"""
    if extended:
        return (can_id & 0xFFFF00FF) == 0x18DA00F1
    return can_id != 0x7DF


# ----------------------------------------------------------------------
# the backend
# ----------------------------------------------------------------------
class ElmBackend(CanBackend):
    """An ELM327-compatible adapter. ``protocol`` is an ATSP number: "0" searches automatically
    (sending the standard 'supported PIDs' request), "6" (CAN 11-bit 500k) and the like don't
    search: safer on a bus that isn't a car's (a truck's 250k J1939 network, say)."""

    kind = "elm327"
    can_transmit = True
    answers_only = True         # hears only answers to its own requests (core.link words the lamp for it)

    def __init__(self, channel: str, protocol: str = "0", stream: Stream | None = None,
                 bus: BusSetup | None = None):
        super().__init__(channel)
        self.bus = bus
        if bus is not None:
            protocol = bus.protocol or "0"
        self.protocol_choice = (protocol or "0").upper()
        if self.protocol_choice not in PROTOCOLS:
            raise ValueError(f"unknown ELM protocol {protocol!r}")
        self.stream = stream
        self._queue: queue.Queue[CanFrame] = queue.Queue()
        self._lock = threading.RLock()
        self.is_open = False
        self.link_error: str | None = None
        # what the adapter told us
        self.version = ""           # ATI: "ELM327 v2.2"
        self.device = ""            # AT@1: maker's description
        self.stn = ""               # STI on an OBDLink / STN chip ("" for others)
        self.voltage = ""           # ATRV: "12.6V" at the OBD port
        self.protocol = ""          # ATDPN once the vehicle answered: "6" ...
        self.extended = False       # 29-bit IDs (from the protocol)
        self.bitrate = 0
        self.last_message = ""      # the adapter's last message ('NO DATA', 'CAN ERROR' ...)
        self.requests = 0
        self.answers = 0
        self.last_round_trip = 0.0  # seconds, the last request's
        self.fast_single = True     # append the 'expect one answer' digit to single-PID requests
        self._header: tuple | None = None   # (can_id, extended) currently set with ATSH
        self._filter: str | None = None     # ATCRA argument currently set ("" = automatic)
        self._search_failed = 0.0           # when the last protocol search found no vehicle
        self.noncan = False                 # J1850 / ISO 9141 / KWP: answers go through obd.noncan
        self.j1939 = False                  # protocol A: listening (ATMA) with requests in between
        self.monitor_restarts = 0           # J1939: times monitoring stopped by itself (BUFFER FULL ...)
        self._mon_thread: threading.Thread | None = None
        self._mon_idle = threading.Event()  # set while the adapter isn't streaming (ATMA)
        self._mon_idle.set()
        self._pause_depth = 0
        self._pause_guard = threading.Lock()
        self._stop_mon = False
        self.ids = noncan.IdMap()           # non-CAN module address <-> stand-in response ID

    # --- lifecycle ---
    def open(self):
        with self._lock:
            if self.stream is None:
                try:
                    self.stream = open_stream(self.channel)
                except ValueError as e:
                    raise ElmError(str(e)) from e
            try:
                self.stream.open()
                self.is_open = True
                self._init()
            except Exception:
                self.is_open = False
                try:
                    self.stream.close()
                except OSError:
                    pass
                raise
            self.link_error = None

    def close(self):
        self._stop_mon = True
        if self._mon_thread is not None and self._mon_thread is not threading.current_thread():
            self._mon_thread.join(timeout=3.0)
        self._mon_thread = None
        if self.bus is not None and self.bus.undo and self.is_open:
            for cmd in self.bus.undo:               # back to the normal bus, best effort
                if not console_refusal(cmd):
                    try:
                        self.command(cmd)
                    except OSError:
                        break
        with self._lock:
            self.is_open = False
            if self.stream is not None:
                try:
                    self.stream.close()
                except OSError:
                    pass

    def _init(self):
        self._drain()
        self.stream.write(b"\r")                    # finish any half-typed command
        self._read_until_prompt(1.0, "")
        banner = self.command("ATZ", RESET_TIMEOUT)
        if not any("ELM" in line.upper() or "STN" in line.upper() for line in banner):
            # Some clones answer ATZ with nothing useful but work after ATWS / ATI.
            if not self.command("ATI", AT_TIMEOUT):
                raise ElmError("No answer from the adapter. Is it an ELM327-compatible OBD adapter, "
                               "plugged into the vehicle and powered?")
        for cmd in ("ATE0", "ATL0", "ATS0", "ATH1", "ATAT1", "ATCAF1"):
            self._at(cmd)
        self.version = " ".join(self.command("ATI", AT_TIMEOUT)) or "ELM327"
        self.device = " ".join(self.command("AT@1", AT_TIMEOUT))
        if self.device.strip() == "?":
            self.device = ""
        sti = " ".join(self.command("STI", AT_TIMEOUT))
        self.stn = "" if not sti or "?" in sti else sti
        self.read_voltage()
        if self.bus is not None:
            self._apply_bus(self.bus)
        self._at(f"ATSP{self.protocol_choice}")
        self._header, self._filter = None, None
        if self.protocol_choice == J1939_PROTOCOL:
            self._init_j1939()
        elif self.protocol_choice == "0":
            self._search()                          # the vehicle may be off: retried on the first request
        else:
            self._learn_protocol()

    def _apply_bus(self, bus: BusSetup):
        if not bus.commands:
            raise ElmError(f"{bus.label}: its adapter commands haven't been filled in yet"
                           + (f" ({bus.source})" if bus.source else "") + ". See the plugin's notes.")
        for cmd in bus.commands:
            why = console_refusal(cmd)
            if why:
                raise ElmError(f"{bus.label}: {cmd!r} refused: {why}")
            if classify(self.command(cmd)) == "?":
                raise ElmError(f"{bus.label}: the adapter doesn't understand {cmd!r}"
                               + (f" (these commands are for {bus.adapter})" if bus.adapter else "") + ".")

    def _init_j1939(self):
        """J1939 (protocol A, 250k): frames shown raw with their 29-bit ID, requests sent raw, the
        adapter listening silently where it can. Never reached by the automatic search."""
        for cmd in ("ATJHF0", "ATCSM1"):            # optional: older or clone adapters may refuse them
            self.command(cmd)
        self._at("ATCAF0")
        self.protocol, self.extended, self.bitrate = J1939_PROTOCOL, True, 250000
        self.j1939, self.noncan = True, False
        self.answers_only = False                   # it hears the broadcasts: lamps as for a wired bus
        self._stop_mon = False
        self._mon_thread = threading.Thread(target=self._monitor_loop, name="elm-j1939", daemon=True)
        self._mon_thread.start()

    @contextmanager
    def _paused(self):
        """Hold the adapter for a command: J1939 monitoring stops first and resumes after."""
        with self._pause_guard:
            self._pause_depth += 1
        try:
            if threading.current_thread() is not self._mon_thread:
                self._mon_idle.wait(timeout=3.0)
            with self._lock:
                yield
        finally:
            with self._pause_guard:
                self._pause_depth -= 1

    def _monitor_loop(self):
        while self.is_open and not self._stop_mon:
            if self._pause_depth:
                time.sleep(0.02)
                continue
            with self._lock:
                if self._pause_depth or not self.is_open or self._stop_mon:
                    continue
                self._mon_idle.clear()
                try:
                    self._drain()
                    self.stream.write(b"ATMA\r")
                    buf = b""
                    ended = False
                    while not self._pause_depth and not self._stop_mon and not ended:
                        buf += self.stream.read_some(0.1)
                        *done, buf = buf.replace(b"\n", b"\r").split(b"\r")
                        for raw in done:
                            ended |= self._monitor_line(raw)
                        ended |= PROMPT in buf
                    if ended:
                        self.monitor_restarts += 1          # it stopped by itself (BUFFER FULL ...)
                    else:
                        self.stream.write(b"\r")             # stop it
                        self._read_raw_until_prompt(2.0)
                except OSError as e:
                    self.link_error = str(e) or "connection to the OBD adapter lost"
                    self.is_open = False
                finally:
                    self._mon_idle.set()
            time.sleep(0.01)

    def _monitor_line(self, raw: bytes) -> bool:
        """One line from ATMA. True if it ends the monitoring (the prompt came back)."""
        text = raw.decode("ascii", "replace").strip()
        if not text:
            return False
        if PROMPT.decode() in text:
            return True
        parsed = parse_frame_line(text, True)
        if parsed is not None:
            self._queue.put(CanFrame(parsed[0], parsed[1], True, time.time()))
            return False
        msg = classify([text])
        if msg in BUS_TROUBLE:
            self._queue.put(CanFrame(0, b"", False, time.time(), error=f"OBD adapter: {msg}"))
        return False

    def _send_j1939(self, frame: CanFrame):
        """A J1939 Request (or transport-protocol flow control) through the adapter: monitoring
        pauses, the frame goes out raw with its header, whatever answers straight away is
        delivered, and monitoring resumes (broadcast answers arrive through it)."""
        data = bytes(frame.data)
        pf = (frame.can_id >> 16) & 0xFF
        ok = frame.extended and data and (
            (pf == PF_REQUEST and len(data) in (3, 8)) or (pf == PF_TPCM and data[0] in TP_FLOW_CONTROL))
        if not ok:
            raise ElmError("Through a Bluetooth OBD adapter, J1939 only sends Requests (and transport "
                           "flow control), not other frames.")
        with self._paused():
            if (frame.can_id, True) != self._header:
                self._at(f"ATCP{(frame.can_id >> 24) & 0x1F:02X}")
                self._at(f"ATSH{frame.can_id & 0xFFFFFF:06X}")
                self._header = (frame.can_id, True)
            t0 = time.time()
            lines = self.command(data.hex().upper(), AT_TIMEOUT)
            self.last_round_trip = time.time() - t0
            self.requests += 1
            now = time.time()
            for line in lines:
                parsed = parse_frame_line(line, True)
                if parsed is not None:
                    self._queue.put(CanFrame(parsed[0], parsed[1], True, now))

    def _search(self):
        """Let the adapter find the vehicle's protocol with the standard 'supported PIDs'
        request (a read). The answers are delivered like any other."""
        lines = self.command("0100", SEARCH_TIMEOUT)
        self._learn_protocol()
        if self.protocol:
            self._deliver(lines, bytes([0x01, 0x00]))
            self._search_failed = 0.0
        else:
            self.last_message = classify(lines) or "no answer"
            self._search_failed = time.time()
        self._header, self._filter = None, None

    def _learn_protocol(self):
        dpn = "".join(self.command("ATDPN", AT_TIMEOUT)).strip().upper()
        num = dpn[1:] if dpn.startswith("A") and len(dpn) == 2 else dpn
        self.noncan = num in NONCAN_PROTOCOLS
        if num in CAN_PROTOCOLS:
            self.protocol = num
            self.extended, self.bitrate = CAN_PROTOCOLS[num]
        elif self.noncan:
            self.protocol, self.extended, self.bitrate = num, False, 0
        else:
            self.protocol = ""

    def module_label(self, response_id: int) -> str:
        """On a non-CAN car, the module behind a stand-in response ID: 'engine (0x10)'."""
        return self.ids.label(response_id) if self.noncan else ""

    @property
    def protocol_name(self) -> str:
        return PROTOCOLS.get(self.protocol, "")

    def read_voltage(self) -> str:
        """The battery voltage at the OBD port (ATRV), e.g. '12.6V'. '' if the adapter can't say."""
        with self._paused():
            v = " ".join(self.command("ATRV", AT_TIMEOUT)).strip()
            self.voltage = v if v and "?" not in v else ""
            return self.voltage

    def describe(self) -> str:
        what = self.stn or self.version or "ELM327"
        return f"OBD adapter {self.channel} ({what}{', J1939' if self.j1939 else ''})"

    # --- text I/O ---
    def _drain(self):
        while self.stream.read_some(0.0):
            pass

    def _read_until_prompt(self, timeout: float, command: str) -> list[str]:
        buf = b""
        deadline = time.time() + timeout
        while PROMPT not in buf:
            left = deadline - time.time()
            if left <= 0:
                break
            buf += self.stream.read_some(min(left, 0.25))
        return split_reply(buf.split(PROMPT)[0], command)

    def command(self, cmd: str, timeout: float = AT_TIMEOUT) -> list[str]:
        """Send one command and return the reply lines (empty on timeout). Raises OSError (and
        marks the link lost) if the connection is gone."""
        with self._paused():
            if not self.is_open or self.stream is None:
                raise OSError(self.link_error or "the OBD adapter isn't connected")
            try:
                self._drain()
                self.stream.write(cmd.encode("ascii") + b"\r")
                lines = self._read_until_prompt(timeout, cmd)
                self._trace(cmd, lines)
                return lines
            except OSError as e:
                self.link_error = str(e) or "connection to the OBD adapter lost"
                self.is_open = False
                raise OSError(self.link_error) from e

    def _trace(self, cmd: str, lines: list[str]):
        """With CANOPENER_ELM_TRACE=FILE set, append each command and its reply lines, timestamped:
        the adapter's own text, for checking against what the code expects (docs/BLUETOOTH-OBD.md)."""
        path = os.environ.get(TRACE_ENV)
        if not path:
            return
        stamp = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        try:
            with open(path, "a") as f:
                f.write(f"{stamp} > {cmd}\n" + "".join(f"{stamp} < {line}\n" for line in lines))
        except OSError:
            pass

    def monitor(self, cmd: str, seconds: float, max_lines: int = 5000) -> list[str]:
        """Run a monitoring command (ATMA, ATMP ..., ATDM1) for ``seconds``, then stop it (any
        character stops an ELM327) and return the lines it printed."""
        with self._paused():
            if not self.is_open or self.stream is None:
                raise OSError(self.link_error or "the OBD adapter isn't connected")
            try:
                self._drain()
                self.stream.write(cmd.encode("ascii") + b"\r")
                buf = b""
                end = time.time() + seconds
                while time.time() < end and buf.count(b"\r") < max_lines and PROMPT not in buf:
                    buf += self.stream.read_some(min(0.2, max(0.0, end - time.time())))
                if PROMPT not in buf:
                    self.stream.write(b"\r")              # stop it
                    buf += b"\r" + self._read_raw_until_prompt(2.0)
            except OSError as e:
                self.link_error = str(e) or "connection to the OBD adapter lost"
                self.is_open = False
                raise OSError(self.link_error) from e
            lines = split_reply(buf.split(PROMPT)[0], cmd)
            self._trace(cmd, lines)
            return lines

    def _read_raw_until_prompt(self, timeout: float) -> bytes:
        buf = b""
        deadline = time.time() + timeout
        while PROMPT not in buf and time.time() < deadline:
            buf += self.stream.read_some(min(0.25, max(0.0, deadline - time.time())))
        return buf.split(PROMPT)[0]

    def _at(self, cmd: str):
        lines = self.command(cmd, AT_TIMEOUT)
        if classify(lines) == "?":
            raise ElmError(f"The adapter doesn't understand {cmd}: not ELM327-compatible enough.")

    # --- header / filter ---
    def _set_header(self, can_id: int, extended: bool):
        if self._header == (can_id, extended):
            return
        if extended:
            self._at(f"ATCP{(can_id >> 24) & 0x1F:02X}")
            self._at(f"ATSH{can_id & 0xFFFFFF:06X}")
            want = f"18DAF1{(can_id >> 8) & 0xFF:02X}" if is_physical(can_id, True) else ""
        else:
            self._at(f"ATSH{can_id & 0x7FF:03X}")
            # Answers to the standard OBD IDs pass the adapter's automatic filter; another module
            # (UDS to 0x726, say) answers on its request ID + 8, which has to be let through.
            want = "" if can_id == 0x7DF or 0x7E0 <= can_id <= 0x7E7 else f"{(can_id + 8) & 0x7FF:03X}"
        if want != self._filter:
            self._at(f"ATCRA{want}" if want else "ATAR")    # AR: back to the automatic filter
            self._filter = want
        self._header = (can_id, extended)

    # --- the backend interface ---
    def send(self, frame: CanFrame):
        if self.j1939:
            return self._send_j1939(frame)
        data = bytes(frame.data)
        if not data:
            raise ElmError("empty frame")
        pci = data[0] >> 4
        if pci == 3:
            return                                  # flow control: the adapter sends its own
        if pci == 1:
            raise ElmError("A Bluetooth OBD adapter sends single-frame requests only (up to 7 bytes).")
        n = data[0] & 0x0F
        if pci != 0 or not 1 <= n <= 7 or len(data) < 1 + n:
            raise ElmError("A Bluetooth OBD adapter only sends OBD-II / UDS requests, not raw frames.")
        payload = data[1:1 + n]
        with self._lock:
            if not self.is_open:
                self._reconnect()
            if not self.protocol:
                if time.time() - self._search_failed >= SEARCH_RETRY:
                    self._search()
                if not self.protocol:
                    raise ElmError("No vehicle answered the adapter"
                                   + (f" (it says {self.last_message})" if self.last_message else "")
                                   + ". Key on? Plugged into the OBD port?")
            if self.noncan:
                return self._send_noncan(frame, payload)
            if frame.extended != self.extended:
                raise ElmError(f"This vehicle uses {'29' if self.extended else '11'}-bit CAN IDs.")
            self._set_header(frame.can_id, frame.extended)
            cmd = payload.hex().upper()
            single = (self.fast_single and payload[0] == 0x01 and len(payload) == 2
                      and is_physical(frame.can_id, frame.extended))
            slow = payload[0] in SLOW_SERVICES
            if slow:
                self._at("ATSTFF")
            t0 = time.time()
            try:
                lines = self.command(cmd + ("1" if single else ""), REQUEST_TIMEOUT)
                if single and classify(lines) == "?":
                    self.fast_single = False        # an adapter without the response-count digit
                    lines = self.command(cmd, REQUEST_TIMEOUT)
            finally:
                if slow and self.is_open:
                    self._at("ATST32")              # back to the default (adaptive timing tunes it)
            self.last_round_trip = time.time() - t0
            self.requests += 1
            self._deliver(lines)

    def _send_noncan(self, frame: CanFrame, payload: bytes):
        """A request on J1850 / ISO 9141 / KWP: the adapter's standard header, and the answers
        rebuilt in CAN form (obd.noncan). Requests to one module keep only that module's answer."""
        service = payload[0]
        if not 0x01 <= service <= 0x0A or frame.extended:
            raise ElmError(f"This vehicle uses {self.protocol_name}: OBD-II requests only (UDS and other "
                           "services need a CAN vehicle).")
        if service == 0x06:
            raise ElmError("On-board test results (Mode 06) use an older layout on this protocol, which "
                           "CANopener doesn't decode yet.")
        t0 = time.time()
        lines = self.command(payload.hex().upper(), NONCAN_TIMEOUT)
        self.last_round_trip = time.time() - t0
        self.requests += 1
        only = frame.can_id + isotp.RESPONSE_OFFSET if 0x7E0 <= frame.can_id <= 0x7E7 else None
        self._deliver(lines, payload, only)

    def _deliver(self, lines: list[str], request: bytes = b"", only: int | None = None):
        if self.noncan:
            return self._deliver_noncan(lines, request, only)
        msg = classify(lines)
        self.last_message = msg or ""
        now = time.time()
        got = False
        for line in lines:
            parsed = parse_frame_line(line, self.extended)
            if parsed is None:
                continue
            got = True
            data = parsed[1]
            if data[0] >> 4 > 3 and len(data) <= 7:
                data = bytes([len(data)]) + data    # a clone that left the length byte out: put it back
            self._queue.put(CanFrame(parsed[0], data, self.extended, now))
        if got:
            self.answers += 1
        if msg in BUS_TROUBLE:
            self._queue.put(CanFrame(0, b"", False, now, error=f"OBD adapter: {msg}"))

    def _deliver_noncan(self, lines: list[str], request: bytes, only: int | None):
        msg = classify(lines)
        self.last_message = msg or ""
        now = time.time()
        messages = [m for m in (noncan.parse_message(line, self.protocol) for line in lines) if m]
        answers = noncan.normalize(request, messages)
        for src, body in answers.items():
            rid = self.ids.response_id(src)
            if only is not None and rid != only:
                continue
            for data in isotp.segment(body, pad=0x00):
                self._queue.put(CanFrame(rid, data, False, now))
        if answers:
            self.answers += 1
        if msg in BUS_TROUBLE:
            self._queue.put(CanFrame(0, b"", False, now, error=f"OBD adapter: {msg}"))

    def _reconnect(self):
        """The link dropped (Bluetooth out of range, adapter unplugged): try once to open it again."""
        try:
            if self.stream is not None:
                self.stream.close()
        except OSError:
            pass
        try:
            self.stream.open()
            self.is_open = True
            self._init()
            self.link_error = None
        except Exception as e:
            self.is_open = False
            self.link_error = str(e) or "can't reach the OBD adapter"
            raise OSError(self.link_error) from e

    def recv(self) -> CanFrame | None:
        if self.link_error and not self.is_open:
            time.sleep(0.2)
            raise OSError(self.link_error)
        try:
            return self._queue.get(timeout=0.2)
        except queue.Empty:
            return None
