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
frames from the clients are dropped: the adapter sends its own. Only the CAN OBD protocols
(ISO 15765-4, 11- or 29-bit, 250 or 500 kbit/s) are supported; that's every car sold in the US
since 2008 and in the EU since about 2004 (diesel 2007).

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
}
CAN_PROTOCOLS = {"6": (False, 500000), "7": (True, 500000), "8": (False, 250000), "9": (True, 250000)}
# Requests modules often answer 'response pending' (7F xx 78) to before the real answer: clear codes,
# on-board tests, UDS clear / reset / routines. The adapter's timeout goes to its maximum (~1 s)
# for these so it keeps listening.
SLOW_SERVICES = {0x04, 0x08, 0x11, 0x14, 0x31}
SEARCH_RETRY = 5.0          # after a failed protocol search (key off), don't search again this soon

NO_ANSWER = ("NO DATA", "UNABLE TO CONNECT", "STOPPED")
# Reply lines that mean the adapter saw trouble on the vehicle's bus: reported as error frames.
BUS_TROUBLE = ("CAN ERROR", "BUS ERROR", "BUS BUSY", "BUFFER FULL", "DATA ERROR", "<DATA ERROR",
               "<RX ERROR", "RX ERROR", "FB ERROR", "LP ALERT", "LV RESET")
_HEX = re.compile(r"^[0-9A-F]+$")
_BT_ADDR = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


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

    def __init__(self, channel: str, protocol: str = "0", stream: Stream | None = None):
        super().__init__(channel)
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
        self._at(f"ATSP{self.protocol_choice}")
        self._header, self._filter = None, None
        if self.protocol_choice == "0":
            self._search()                          # the vehicle may be off: retried on the first request
        else:
            self._learn_protocol()

    def _search(self):
        """Let the adapter find the vehicle's protocol with the standard 'supported PIDs'
        request (a read). The answers are delivered like any other."""
        lines = self.command("0100", SEARCH_TIMEOUT)
        self._learn_protocol()
        if self.protocol:
            self._deliver(lines)
            self._search_failed = 0.0
        else:
            self.last_message = classify(lines) or "no answer"
            self._search_failed = time.time()
        self._header, self._filter = None, None

    def _learn_protocol(self):
        dpn = "".join(self.command("ATDPN", AT_TIMEOUT)).strip().upper()
        num = dpn[1:] if dpn.startswith("A") and len(dpn) == 2 else dpn
        if num in CAN_PROTOCOLS:
            self.protocol = num
            self.extended, self.bitrate = CAN_PROTOCOLS[num]
        elif num in PROTOCOLS and num != "0":
            self.protocol = ""
            raise ElmError(f"The vehicle speaks {PROTOCOLS[num]}. CANopener talks to CAN vehicles only "
                           "(ISO 15765-4: most cars from 2008 on).")
        else:
            self.protocol = ""

    @property
    def protocol_name(self) -> str:
        return PROTOCOLS.get(self.protocol, "")

    def read_voltage(self) -> str:
        """The battery voltage at the OBD port (ATRV), e.g. '12.6V'. '' if the adapter can't say."""
        with self._lock:
            v = " ".join(self.command("ATRV", AT_TIMEOUT)).strip()
            self.voltage = v if v and "?" not in v else ""
            return self.voltage

    def describe(self) -> str:
        what = self.stn or self.version or "ELM327"
        return f"OBD adapter {self.channel} ({what})"

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
        with self._lock:
            if not self.is_open or self.stream is None:
                raise OSError(self.link_error or "the OBD adapter isn't connected")
            try:
                self._drain()
                self.stream.write(cmd.encode("ascii") + b"\r")
                return self._read_until_prompt(timeout, cmd)
            except OSError as e:
                self.link_error = str(e) or "connection to the OBD adapter lost"
                self.is_open = False
                raise OSError(self.link_error) from e

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

    def _deliver(self, lines: list[str]):
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
