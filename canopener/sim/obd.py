"""A simulated OBD-II (SAE J1979) ECU, so the automotive side can be demonstrated and tested
with no car. It answers a request on the functional ID 0x7DF (or its physical ID 0x7E0) and
replies on 0x7E8 with real ISO-TP framing: single frames for short answers, and a First
Frame + Flow-Control + Consecutive Frames handshake for the multi-frame VIN.

Standard public PIDs/DTCs only; values wander a little so the live view has something to show.
It mirrors ``sim/machine.py`` (a backend user on the same bus) and uses the pure ``obd`` package
for encoding so the sim and the client agree by construction.
"""

import math
import threading
import time

from .. import obd
from ..can.backends import CanBackend
from ..can.frame import CanFrame

ECU_REQUEST_ID = 0x7E0       # the physical request id this ECU also listens on
ECU_RESPONSE_ID = 0x7E8      # 0x7E0 + 8

VIN = "1HGCM82633A004352"    # a well-formed sample VIN (not a real vehicle)
STORED_DTCS = ["P0133", "P0420"]      # O2 sensor slow response; catalyst efficiency
PENDING_DTCS = ["P0171"]              # system too lean (bank 1)


class ObdSimulator:
    def __init__(self, backend: CanBackend, seed: float | None = None):
        self.backend = backend
        self.t0 = seed if seed is not None else time.time()
        self._running = False
        self._thread: threading.Thread | None = None
        # Pending multi-frame send, waiting for the tester's flow control: list of CAN frames.
        self._pending_cf: list[bytes] = []

    # --- live values (wander with time so the display isn't static) ---
    def _live(self, pid: int) -> bytes | None:
        t = time.time() - self.t0
        if pid == 0x05:      # coolant temp: warm up 40 -> ~88 C
            return bytes([min(88, 40 + int(t * 2)) + 40])
        if pid == 0x0C:      # rpm ~ 800 +/- idle wander, scaled *4
            rpm = 800 + 40 * math.sin(t / 3)
            v = int(rpm * 4)
            return bytes([(v >> 8) & 0xFF, v & 0xFF])
        if pid == 0x0D:      # vehicle speed: 0 (parked)
            return bytes([0])
        if pid == 0x11:      # throttle ~ 14%
            return bytes([int(0.14 * 255)])
        if pid == 0x42:      # control module voltage ~ 14.1 V, /1000
            v = 14100
            return bytes([(v >> 8) & 0xFF, v & 0xFF])
        return None

    _SUPPORTED = [0x05, 0x0C, 0x0D, 0x11, 0x42]

    def _supported_bitmask(self, base: int) -> bytes:
        bits = 0
        for p in self._SUPPORTED:
            if base < p <= base + 0x20:
                bits |= 1 << (31 - (p - base - 1))
        if any(p > base + 0x20 for p in self._SUPPORTED):   # set the "next range supported" bit (PID base+0x20)
            bits |= 1 << (31 - (0x20 - 1))
        return bits.to_bytes(4, "big")

    # --- request handling ---
    def _response_body(self, mode: int, pid: int | None) -> bytes | None:
        if mode == 0x01:
            if pid in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
                return bytes([0x41, pid]) + self._supported_bitmask(pid)
            data = self._live(pid)
            return bytes([0x41, pid]) + data if data is not None else None
        if mode == 0x03:
            return self._dtc_body(0x43, STORED_DTCS)
        if mode == 0x07:
            return self._dtc_body(0x47, PENDING_DTCS)
        if mode == 0x0A:
            return self._dtc_body(0x4A, [])
        if mode == 0x09 and pid == 0x02:
            return bytes([0x49, 0x02, 0x01]) + VIN.encode("ascii")
        return None

    @staticmethod
    def _dtc_body(service: int, codes: list[str]) -> bytes:
        body = bytes([service, len(codes)])
        for c in codes:
            body += obd.encode_dtc(c)
        return body

    def _send_message(self, body: bytes):
        """Send a response body as ISO-TP: single frame if it fits, else FF then wait for FC."""
        if len(body) <= 7:
            frame = bytes([len(body)]) + body
            self._tx(frame.ljust(8, b"\x00"))
            return
        # First Frame: 0x1 + 12-bit length, then first 6 bytes.
        length = len(body)
        self._tx(bytes([0x10 | (length >> 8), length & 0xFF]) + body[:6])
        # Queue consecutive frames; they go out when the tester's flow-control arrives.
        rest, seq, cfs = body[6:], 1, []
        while rest:
            chunk, rest = rest[:7], rest[7:]
            cfs.append((bytes([0x20 | (seq & 0x0F)]) + chunk).ljust(8, b"\x00"))
            seq += 1
        self._pending_cf = cfs

    def _tx(self, data: bytes):
        self.backend.send(CanFrame(ECU_RESPONSE_ID, bytes(data), extended=False, timestamp=time.time()))

    def _on_request(self, data: bytes):
        if not data:
            return
        pci = data[0] >> 4
        if pci == 0x3:                      # Flow Control from the tester: release queued CFs
            for cf in self._pending_cf:
                self._tx(cf)
            self._pending_cf = []
            return
        if pci != 0x0:                      # we only accept single-frame requests
            return
        n = data[0] & 0x0F
        body = data[1:1 + n]
        if not body:
            return
        mode = body[0]
        pid = body[1] if len(body) > 1 else None
        resp = self._response_body(mode, pid)
        if resp is not None:
            self._send_message(resp)

    # --- lifecycle (mirrors MachineSimulator) ---
    def start(self):
        self.backend.open()
        self._running = True
        self._thread = threading.Thread(target=self._rx_loop, name="obd-sim-rx", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.backend.close()
        except OSError:
            pass

    @property
    def running(self) -> bool:
        return self._running

    def _rx_loop(self):
        while self._running:
            try:
                fr = self.backend.recv()
            except OSError:
                time.sleep(0.2)
                continue
            if fr is None or fr.extended or fr.error:
                continue
            if fr.can_id in (obd.FUNCTIONAL_REQUEST_ID, ECU_REQUEST_ID):
                self._on_request(bytes(fr.data))
