"""A simulated ELM327 Bluetooth adapter plugged into the simulated OBD-II car.

It speaks the adapter's text protocol over one end of a socket pair (``ElmBackend`` talks to the
other end through ``SocketStream``) and puts real ISO-TP frames on a ``VirtualBus`` where
``sim.obd.ObdSimulator`` answers. So ``--sim`` and the tests run the real ``ElmBackend`` code,
the same text it would exchange with a dongle, with no Bluetooth.

Covers what the backend uses: ATZ / ATI / AT@1 / ATRV / ATDPN, echo, spaces, headers, linefeeds,
ATSP, ATSH (11- and 29-bit, with ATCP), ATCRA, ATAT, ATCAF, the response-count digit, 'NO DATA',
'SEARCHING...', automatic flow control for multi-frame answers, and waiting through a
'response pending' (7F xx 78) answer as a real ELM327 does. Unknown commands get '?', like the
real thing; STI (an OBDLink-only command) does too.
"""

import socket
import threading
import time

from .. import obd
from ..can.backends import CanBackend, VirtualBackend, VirtualBus
from ..can.frame import CanFrame
from .obd import ObdSimulator

VERSION = "ELM327 v2.1"
DEVICE = "CANopener simulated OBD adapter"
VOLTAGE = "12.6V"
ANSWER_WINDOW = 0.08        # after the last answer frame, how long to wait for more (the adapter's timeout)
FIRST_ANSWER = 0.3          # nothing at all by then: NO DATA
PENDING_WAIT = 5.0          # after a 'response pending' (7F xx 78)


class ElmSimulator:
    """The adapter. ``host_socket`` is the end the app connects to."""

    def __init__(self, backend: CanBackend, addressing: str = "11"):
        self.backend = backend
        self.vehicle_extended = addressing == "29"
        self.host_socket, self._sock = socket.socketpair()
        self._running = False
        self._thread: threading.Thread | None = None
        self.commands: list[str] = []       # every command received (tests look at these)
        self._reset()

    def _reset(self):
        self.echo, self.spaces, self.headers, self.linefeeds = True, True, False, True
        self.protocol = "0"
        self.found = ""                     # the protocol found by searching
        self.header = None                  # (can_id, extended) from ATSH / ATCP
        self.priority = 0x18
        self.cra = ""

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
            return ["AUTO, ISO 15765-4 (CAN 11/500)" if self.protocol == "0" else "ISO 15765-4 (CAN 11/500)"]
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
        if a.startswith("CRA"):
            self.cra = a[3:]
            return ["OK"]
        if a == "AR":
            self.cra = ""
            return ["OK"]
        if a[:2] in ("AT", "ST") or a in ("CAF0", "CAF1", "CFC0", "CFC1", "M0", "M1"):
            return ["OK"]
        return ["?"]

    # --- OBD requests ---
    def _vehicle_protocol(self) -> str:
        return "7" if self.vehicle_extended else "6"

    def _request(self, c: str) -> list[str]:
        count = 0
        if len(c) % 2:                              # a trailing digit: how many answers to wait for
            count, c = int(c[-1], 16), c[:-1]
        payload = bytes.fromhex(c)
        if not 1 <= len(payload) <= 7:
            return ["?"]
        out = []
        if self.protocol == "0" and not self.found:
            out.append("SEARCHING...")
            self.found = self._vehicle_protocol()
        proto = self.found if self.protocol == "0" else self.protocol
        extended = proto in ("7", "9")
        if proto != self._vehicle_protocol():
            return out + (["UNABLE TO CONNECT"] if self.protocol == "0" else ["NO DATA"])
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
    """The OBD-II car simulator and the simulated adapter plugged into it, stopped together
    (``Connection.simulator``). ``car`` is the ``ObdSimulator`` (its codes, clear state ...)."""

    def __init__(self, seed=None, addressing: str = "11"):
        bus = VirtualBus()
        self.car = ObdSimulator(VirtualBackend(bus, "obd-ecu"), seed=seed, addressing=addressing)
        self.adapter = ElmSimulator(VirtualBackend(bus, "elm"), addressing=addressing)

    def start(self):
        self.car.start()
        self.adapter.start()

    def stop(self):
        self.adapter.stop()
        self.car.stop()

    def __getattr__(self, name):        # tests reach the car's state through the connection
        return getattr(self.car, name)
