"""A simulated ELM327 Bluetooth adapter plugged into a simulated vehicle.

It speaks the adapter's text protocol over one end of a socket pair (``ElmBackend`` talks to the
other end through ``SocketStream``) and puts real ISO-TP frames on a ``VirtualBus`` where
``sim.obd.ObdSimulator`` answers. So ``--sim`` and the tests run the real ``ElmBackend`` code,
the same text it would exchange with a dongle, with no Bluetooth.

Covers what the backend uses: ATZ / ATI / AT@1 / ATRV / ATDPN, echo, spaces, headers, linefeeds,
ATSP, ATSH (11- and 29-bit, with ATCP), ATCRA, ATAT, ATCAF, the response-count digit, 'NO DATA',
'SEARCHING...', automatic flow control for multi-frame answers, and waiting through a
'response pending' (7F xx 78) answer as a real ELM327 does. Unknown commands get '?', like the
real thing; STI (an OBDLink-only command) does too.

The vehicle (``VEHICLES``) can be a CAN car (11- or 29-bit), an older non-CAN car (J1850 PWM /
VPW, ISO 9141-2, KWP2000: the same ``ObdSimulator`` answers, turned into non-CAN messages with
``obd.noncan.to_noncan`` and printed with 3-byte headers and a check byte, as an ELM327 shows
them), or a J1939 machine (``sim.machine.MachineSimulator``) for protocol A: ATMA streams its
broadcasts, and requests sent raw (ATCAF0) reach it. The non-CAN shapes and the J1939 behaviour
follow our reading of SAE J1979 and the ELM327 data sheet, not captures of real vehicles
(docs/BLUETOOTH-OBD.md, "Gaps to fill").
"""

import select
import socket
import threading
import time

from .. import obd
from ..can.backends import CanBackend, VirtualBackend, VirtualBus
from ..can.frame import CanFrame
from ..obd import noncan
from .obd import ObdSimulator

# vehicle -> the protocol number (ATDPN) it answers on
VEHICLES = {"11": "6", "29": "7", "pwm": "1", "vpw": "2", "iso9141": "3", "kwp-slow": "4", "kwp": "5",
            "j1939": "A"}
NONCAN_HEADERS = {"1": (0x41, 0x6B), "2": (0x48, 0x6B), "3": (0x48, 0x6B)}     # KWP: format byte, F1
NONCAN_ADDRESSES = {"ECM": 0x10, "TCM": 0x18}
DP_TEXT = {"1": "SAE J1850 PWM", "2": "SAE J1850 VPW", "3": "ISO 9141-2", "4": "ISO 14230-4 (KWP 5BAUD)",
           "5": "ISO 14230-4 (KWP FAST)", "6": "ISO 15765-4 (CAN 11/500)", "7": "ISO 15765-4 (CAN 29/500)",
           "8": "ISO 15765-4 (CAN 11/250)", "9": "ISO 15765-4 (CAN 29/250)", "A": "SAE J1939 (CAN 29/250)"}

VERSION = "ELM327 v2.1"
DEVICE = "CANopener simulated OBD adapter"
VOLTAGE = "12.6V"
ANSWER_WINDOW = 0.08        # after the last answer frame, how long to wait for more (the adapter's timeout)
FIRST_ANSWER = 0.3          # nothing at all by then: NO DATA
PENDING_WAIT = 5.0          # after a 'response pending' (7F xx 78)


class ElmSimulator:
    """The adapter. ``host_socket`` is the end the app connects to."""

    def __init__(self, backend: CanBackend, addressing: str = "11", vehicle: str | None = None,
                 car: ObdSimulator | None = None):
        self.backend = backend
        self.vehicle = vehicle or addressing
        if self.vehicle not in VEHICLES:
            raise ValueError(f"unknown simulated vehicle {self.vehicle!r}: {', '.join(VEHICLES)}")
        self.vehicle_extended = self.vehicle == "29"
        self.car = car                      # answers non-CAN requests directly
        self.host_socket, self._sock = socket.socketpair()
        self._running = False
        self._thread: threading.Thread | None = None
        self.commands: list[str] = []       # every command received (tests look at these)
        self._pending = b""                 # input that arrived right behind a monitor's stop character
        self._reset()

    def _reset(self):
        self.echo, self.spaces, self.headers, self.linefeeds = True, True, False, True
        self.protocol = "0"
        self.found = ""                     # the protocol found by searching
        self.header = None                  # (can_id, extended) from ATSH / ATCP
        self.priority = 0x18
        self.cra = ""
        self.caf = True                     # CAN automatic formatting (ATCAF)
        self.woke = False                   # a K-line car's computer has been woken (BUS INIT)

    # --- lifecycle ---
    def start(self):
        self.backend.open()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="elm-sim", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        for s in (self._sock, self.host_socket):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.backend.close()
        except OSError:
            pass

    def _loop(self):
        buf = b""
        while self._running:
            if self._pending:
                data, self._pending = self._pending, b""
            else:
                try:
                    data = self._sock.recv(256)
                except OSError:
                    return
            if not data:
                return
            buf += data
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                cmd = line.decode("ascii", "replace").strip()
                if cmd.replace(" ", "").upper() in ("ATMA", "STMA"):
                    self.commands.append(cmd.replace(" ", "").upper())
                    if not self._monitor(cmd, stop_now=bool(buf)):
                        return
                    buf = buf[1:]                   # the character that stopped it is used up
                    continue
                try:
                    reply = self.handle(cmd)
                except Exception as e:              # never leave the app waiting for a prompt
                    reply = [f"ERR {e}"]
                out = (cmd + "\r") if self.echo and cmd else ""
                nl = "\r\n" if self.linefeeds else "\r"
                out += "".join(r + nl for r in reply) + nl + ">"
                try:
                    self._sock.sendall(out.encode("ascii"))
                except OSError:
                    return

    # --- commands ---
    def handle(self, cmd: str) -> list[str]:
        c = cmd.replace(" ", "").upper()
        self.commands.append(c)
        if not c:
            return []
        if c.startswith("AT"):
            return self._at(c[2:])
        if c.startswith("ST"):
            return ["?"]
        if not all(ch in "0123456789ABCDEF" for ch in c):
            return ["?"]
        return self._request(c)

    def _at(self, a: str) -> list[str]:
        flags = {"E": "echo", "S": "spaces", "H": "headers", "L": "linefeeds"}
        if a in ("Z", "WS"):
            self._reset()
            return ["", VERSION]
        if a == "D":
            self._reset()
            return ["OK"]
        if a == "I":
            return [VERSION]
        if a == "@1":
            return [DEVICE]
        if a == "RV":
            return [VOLTAGE]
        if a == "DPN":
            if self.protocol == "0":
                return ["A" + (self.found or "0")]
            return [self.protocol]
        if a == "DP":
            proto = self.found if self.protocol == "0" else self.protocol
            return [("AUTO, " if self.protocol == "0" else "") + DP_TEXT.get(proto, "AUTOMATIC")]
        if len(a) == 2 and a[0] in flags and a[1] in "01":
            setattr(self, flags[a[0]], a[1] == "1")
            return ["OK"]
        if a.startswith("SP") and len(a) == 3:
            self.protocol = a[2]
            self.found = "" if a[2] == "0" else self.found
            return ["OK"]
        if a.startswith("SH") and len(a) in (5, 8):
            h = int(a[2:], 16)
            self.header = (h, False) if len(a) == 5 else ((self.priority << 24) | h, True)
            return ["OK"]
        if a.startswith("CP") and len(a) == 4:
            self.priority = int(a[2:], 16)
            if self.header and self.header[1]:
                self.header = ((self.priority << 24) | (self.header[0] & 0xFFFFFF), True)
            return ["OK"]
        if a in ("CAF0", "CAF1"):
            self.caf = a == "CAF1"
            return ["OK"]
        if a.startswith("CRA"):
            self.cra = a[3:]
            return ["OK"]
        if a == "AR":
            self.cra = ""
            return ["OK"]
        if a[:2] in ("AT", "ST") or a in ("CFC0", "CFC1", "M0", "M1", "CSM0", "CSM1", "JE", "JS",
                                         "JHF0", "JHF1") or (a.startswith("PB") and len(a) == 6):
            return ["OK"]
        return ["?"]

    # --- OBD requests ---
    def _vehicle_protocol(self) -> str:
        return VEHICLES[self.vehicle]

    def _request(self, c: str) -> list[str]:
        count = 0
        if len(c) % 2:                              # a trailing digit: how many answers to wait for
            count, c = int(c[-1], 16), c[:-1]
        payload = bytes.fromhex(c)
        if not 1 <= len(payload) <= 8:
            return ["?"]
        out = []
        vp = self._vehicle_protocol()
        if self.protocol == "0" and not self.found:
            out.append("SEARCHING...")
            if vp == "A":                           # the automatic search never tries J1939
                return out + ["UNABLE TO CONNECT"]
            self.found = vp
        proto = self.found if self.protocol == "0" else self.protocol
        if proto != vp:
            return out + (["UNABLE TO CONNECT"] if self.protocol == "0" else ["NO DATA"])
        if proto in "12345":
            return out + self._noncan_request(proto, payload)
        if proto == "A":
            return out + self._j1939_request(payload)
        if len(payload) > 7:
            return ["?"]
        extended = proto in ("7", "9")
        can_id = self._header_id(extended)
        while self._recv(0.0) is not None:          # stale traffic
            pass
        self.backend.send(CanFrame(can_id, bytes([len(payload)]) + payload + b"\x55" * (7 - len(payload)),
                                   extended=extended, timestamp=time.time()))
        frames = self._collect(extended, count)
        if not frames:
            return out + ["NO DATA"]
        for fid, data in frames:
            out.append(self._format(fid, data, extended))
        return out

    def _noncan_request(self, proto: str, payload: bytes) -> list[str]:
        """A non-CAN car: the OBD simulator's answers as J1850 / K-line messages."""
        out = []
        if proto in "345" and not self.woke:
            out.append("BUS INIT: ...OK")
            self.woke = True
        if self.car is None:
            return out + ["NO DATA"]
        if payload[0] >= 0x10 or payload[0] == 0x06:
            return out + ["NO DATA"]
        lines = []
        for ecu in self.car.ecus:
            src = NONCAN_ADDRESSES.get(ecu.name, 0x28)
            for body in self.car._response(ecu, payload, True):
                if body[:1] == b"\x7f" and body[2:3] == b"\x78":
                    continue                        # 'response pending': the K-line answer comes straight after
                for msg in noncan.to_noncan(body):
                    lines.append(self._noncan_line(proto, src, msg))
        return out + (lines or ["NO DATA"])

    def _noncan_line(self, proto: str, src: int, data: bytes) -> str:
        if proto in "45":
            head = bytes([0x80 | len(data), 0xF1, src])
        else:
            prio, target = NONCAN_HEADERS[proto]
            head = bytes([prio, target, src])
        msg = head + data
        msg += bytes([sum(msg) & 0xFF])             # (J1850 really uses a CRC; the app doesn't check it)
        sep = " " if self.spaces else ""
        if not self.headers:
            msg = data
        return sep.join(f"{b:02X}" for b in msg)

    def _j1939_request(self, payload: bytes) -> list[str]:
        """Protocol A with automatic formatting off: the bytes go out as they are, with the header."""
        can_id = self.header[0] if self.header and self.header[1] else 0x18EAFFF9
        while self._recv(0.0) is not None:
            pass
        self.backend.send(CanFrame(can_id, payload, extended=True, timestamp=time.time()))
        lines, end = [], time.time() + 0.2
        while time.time() < end:
            fr = self._recv(end - time.time())
            if fr is not None and fr.extended and not fr.error and (fr.can_id & 0xFF00) >> 8 in (0xF9, 0xFF):
                lines.append(self._format(fr.can_id, bytes(fr.data), True))
        return lines or ["NO DATA"]

    def _monitor(self, cmd: str, stop_now: bool = False) -> bool:
        """ATMA: print every frame on the bus until any character arrives (``stop_now``: one is
        already waiting). False if the link closed."""
        echo = (cmd + "\r") if self.echo else ""
        try:
            self._sock.sendall(echo.encode())
        except OSError:
            return False
        proto = self.found if self.protocol == "0" else self.protocol
        extended = proto in ("7", "9", "A")
        while self._running and not stop_now:
            r, _, _ = select.select([self._sock], [], [], 0.0)
            if r:
                try:
                    rest = self._sock.recv(256)
                except OSError:
                    return False
                if rest[1:].strip():                # a command right behind the stop character
                    self._pending = rest[1:]
                break
            fr = self._recv(0.05)
            if fr is None or fr.error or fr.extended != extended:
                continue
            try:
                self._sock.sendall((self._format(fr.can_id, bytes(fr.data), extended) + "\r").encode())
            except OSError:
                return False
        try:
            self._sock.sendall(b"\r>")
        except OSError:
            return False
        return True

    def _recv(self, timeout: float):
        """One frame off the bus within ``timeout`` (a VirtualBackend's queue, else recv())."""
        q = getattr(self.backend, "queue", None)
        if q is None:
            return self.backend.recv()
        try:
            return q.get(timeout=timeout) if timeout > 0 else q.get_nowait()
        except Exception:
            return None

    def _header_id(self, extended: bool) -> int:
        if self.header is None or self.header[1] != extended:
            return obd.TWENTY_NINE_BIT.functional_id if extended else obd.FUNCTIONAL_REQUEST_ID
        return self.header[0]

    def _accept(self, can_id: int, extended: bool) -> bool:
        if self.cra:
            return f"{can_id:08X}" == self.cra if extended else f"{can_id:03X}" == self.cra
        if extended:
            return (can_id & 0x1FFFFF00) == 0x18DAF100
        return 0x7E8 <= can_id <= 0x7EF

    def _collect(self, extended: bool, count: int) -> list[tuple[int, bytes]]:
        frames: list[tuple[int, bytes]] = []
        messages = 0
        expecting: dict[int, int] = {}              # id -> consecutive frames still to come
        deadline = time.time() + FIRST_ANSWER
        pending_until = 0.0
        while time.time() < deadline:
            fr = self._recv(deadline - time.time())
            if fr is None or fr.error or fr.extended != extended or not self._accept(fr.can_id, extended):
                continue
            data = bytes(fr.data)
            frames.append((fr.can_id, data))
            pci = data[0] >> 4
            if pci == 1:                            # first frame: our flow control, then the rest
                total = ((data[0] & 0x0F) << 8) | data[1]
                expecting[fr.can_id] = -(-(total - 6) // 7)
                fc_id = (fr.can_id - 8) if not extended else (0x18DA00F1 | ((fr.can_id & 0xFF) << 8))
                self.backend.send(CanFrame(fc_id, obd.flow_control_frame(), extended=extended,
                                           timestamp=time.time()))
            elif pci == 2 and expecting.get(fr.can_id):
                expecting[fr.can_id] -= 1
                if not expecting[fr.can_id]:
                    messages += 1
            elif pci == 0:
                if len(data) >= 4 and data[1] == 0x7F and data[3] == 0x78:
                    pending_until = deadline = time.time() + PENDING_WAIT     # response pending: keep listening
                    continue
                messages += 1
                pending_until = 0.0                 # the real answer came
            if count and messages >= count and not any(expecting.values()):
                break
            deadline = max(pending_until, time.time() + ANSWER_WINDOW)
        return frames

    def _format(self, can_id: int, data: bytes, extended: bool) -> str:
        if not self.headers:
            body = data[1:1 + (data[0] & 0x0F)] if data[0] >> 4 == 0 else data
            return (" " if self.spaces else "").join(f"{b:02X}" for b in body)
        sep = " " if self.spaces else ""
        if extended:
            head = sep.join(f"{(can_id >> s) & 0xFF:02X}" for s in (24, 16, 8, 0))
        else:
            head = f"{can_id:03X}"
        return head + sep + sep.join(f"{b:02X}" for b in data)


class SimulatedCar:
    """A simulated vehicle and the simulated adapter plugged into it, stopped together
    (``Connection.simulator``). ``vehicle`` is one of ``VEHICLES``: a car (``car`` is the
    ``ObdSimulator``: its codes, clear state ...) or ``"j1939"`` (``machine`` is the
    ``MachineSimulator``)."""

    def __init__(self, seed=None, addressing: str = "11", vehicle: str | None = None):
        vehicle = vehicle or addressing
        bus = VirtualBus()
        self.vehicle = vehicle
        self.machine = None
        if vehicle == "j1939":
            from .machine import MachineSimulator
            self.machine = MachineSimulator(VirtualBackend(bus, "sim-ecus"), seed=seed)
            self.car = None
        else:
            self.car = ObdSimulator(VirtualBackend(bus, "obd-ecu"), seed=seed,
                                    addressing="29" if vehicle == "29" else "11")
        self.adapter = ElmSimulator(VirtualBackend(bus, "elm"), vehicle=vehicle, car=self.car)

    def start(self):
        (self.machine or self.car).start()
        self.adapter.start()

    def stop(self):
        self.adapter.stop()
        (self.machine or self.car).stop()

    def __getattr__(self, name):        # tests reach the vehicle's state through the connection
        target = self.__dict__.get("car") or self.__dict__.get("machine")
        if target is None:
            raise AttributeError(name)
        return getattr(target, name)
