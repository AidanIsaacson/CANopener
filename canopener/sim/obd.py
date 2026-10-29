"""A simulated OBD-II (SAE J1979) car, so the automotive side can be demonstrated and tested with no
vehicle. Two ECUs answer, like a real car: the engine (ECM, 0x7E0 -> 0x7E8) and the transmission
(TCM, 0x7E1 -> 0x7E9), with real ISO-TP framing both ways (multi-frame answers wait for the
tester's flow control; multi-frame requests get ours). ``addressing="29"`` puts them on 29-bit
IDs instead (ECM 0x18DAF110, TCM 0x18DAF118).

What it does: live data that moves, stored / pending / permanent codes, readiness monitors, a
freeze frame, Mode 06 test results (the catalyst test failing, to go with P0420), vehicle info,
Mode 04 clear (answered "response pending" first, as real ECUs often do, then cleared: codes,
freeze frame, monitors reset, MIL off), the Mode 08 EVAP leak test, and a small UDS responder.

Standard public PIDs/DTCs only. It mirrors ``sim/machine.py`` (a backend user on the same bus) and
uses the pure ``obd`` package for encoding, so the sim and the client agree by construction.
"""

import math
import threading
import time
from dataclasses import dataclass, field

from .. import obd
from ..can.backends import CanBackend
from ..can.frame import CanFrame

ECU_REQUEST_ID = 0x7E0       # the ECM's physical request id
ECU_RESPONSE_ID = 0x7E8      # 0x7E0 + 8

VIN = "1HGCM82633A004352"    # a well-formed sample VIN (not a real vehicle)
STORED_DTCS = ["P0133", "P0420"]      # O2 sensor slow response; catalyst efficiency
PENDING_DTCS = ["P0171"]              # system too lean (bank 1)
PERMANENT_DTCS = ["P0420"]            # MIL-on code: stays until the monitor passes again
TCM_STORED_DTCS = ["P0730"]           # incorrect gear ratio
UDS_DTCS = [(0x45, 0x61, 0x00, 0x08), (0x07, 0x30, 0x00, 0x08)]     # C0561-00, P0730-00, confirmed


@dataclass
class _Ecu:
    name: str
    request_id: int
    response_id: int
    stored: list[str]
    pending: list[str] = field(default_factory=list)
    permanent: list[str] = field(default_factory=list)
    calid: str = ""
    pids: tuple = ()
    tx_cf: list[bytes] = field(default_factory=list)            # waiting for the tester's flow control
    rx: obd.IsoTpReassembler = field(default_factory=obd.IsoTpReassembler)


class ObdSimulator:
    def __init__(self, backend: CanBackend, seed: float | None = None, addressing: str = "11"):
        self.backend = backend
        self.t0 = seed if seed is not None else time.time()
        self.extended = addressing == "29"
        self.functional_id = obd.TWENTY_NINE_BIT.functional_id if self.extended else obd.FUNCTIONAL_REQUEST_ID
        self.ecm = _Ecu("ECM", *self._ids(0x10, 0x7E0), stored=list(STORED_DTCS), pending=list(PENDING_DTCS),
                        permanent=list(PERMANENT_DTCS), calid="SIMCAL-01A",
                        pids=(0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0F, 0x10, 0x11, 0x13, 0x15,
                              0x1C, 0x1F, 0x21, 0x2F, 0x30, 0x31, 0x33, 0x41, 0x42, 0x46, 0x4D, 0x4E, 0x51, 0x5C,
                              0x5E, 0xA6))
        self.tcm = _Ecu("TCM", *self._ids(0x18, 0x7E1), stored=list(TCM_STORED_DTCS), calid="SIMTCM-02B",
                        pids=(0x01,))
        self.ecus = [self.ecm, self.tcm]
        self.cleared = False
        self.evap_test_running = False
        self.uds_dtcs = list(UDS_DTCS)
        self.uds_data: dict[int, bytes] = {0xF190: VIN.encode("ascii")}
        self.resets = 0
        self._running = False
        self._thread: threading.Thread | None = None

    def _ids(self, address: int, request_11: int) -> tuple[int, int]:
        if self.extended:
            return 0x18DA00F1 | (address << 8), 0x18DAF100 | address
        return request_11, request_11 + obd.RESPONSE_OFFSET

    # --- live values (wander with time so the display isn't static) ---
    def _live(self, ecu: _Ecu, pid: int) -> bytes | None:
        t = time.time() - self.t0
        # Saturates like a real ECU (run time, PID 1F, stops at 65535 s). Unclamped, a seeded sim
        # (t0 = seed, so t is about the epoch) or one running over 18 h raised in its rx thread
        # and went silent.
        u16 = lambda v: max(0, min(0xFFFF, int(v))).to_bytes(2, "big")    # noqa: E731
        if ecu is self.tcm:
            return self._status(ecu) if pid == 0x01 else None
        values = {
            0x01: lambda: self._status(ecu),
            0x03: lambda: bytes([0x02, 0x00]),                  # closed loop
            0x04: lambda: bytes([int((22 + 3 * math.sin(t / 2)) * 255 / 100)]),
            0x05: lambda: bytes([min(88, 40 + int(t * 2)) + 40]),                     # warm up to 88 C
            0x06: lambda: bytes([128 + int(4 * math.sin(t))]),
            0x07: lambda: bytes([128 + 10]),                    # +7.8 %: leaning out (P0171 pending)
            0x0B: lambda: bytes([33]),
            0x0C: lambda: u16((800 + 40 * math.sin(t / 3)) * 4),
            0x0D: lambda: bytes([0]),                           # parked
            0x0F: lambda: bytes([25 + 40]),
            0x10: lambda: u16(350 + 20 * math.sin(t / 2)),     # ~3.5 g/s
            0x11: lambda: bytes([int(0.14 * 255)]),
            0x13: lambda: bytes([0b0000_0011]),                 # B1S1, B1S2
            0x15: lambda: bytes([int(90 + 70 * math.sin(t * 2)), 0xFF]),
            0x1C: lambda: bytes([0x01]),                        # OBD-II (CARB)
            0x1F: lambda: u16(t),
            0x21: lambda: u16(0 if self.cleared else 42),
            0x2F: lambda: bytes([int(0.62 * 255)]),
            0x30: lambda: bytes([0 if self.cleared else 12]),
            0x31: lambda: u16(0 if self.cleared else 1234),
            0x33: lambda: bytes([101]),
            0x41: lambda: self._status(ecu),
            0x42: lambda: u16(14100),                           # 14.1 V
            0x46: lambda: bytes([20 + 40]),
            0x4D: lambda: u16(0 if self.cleared else 95),
            0x4E: lambda: u16(int(t / 60) if self.cleared else 3000),
            0x51: lambda: bytes([0x01]),                        # gasoline
            0x5C: lambda: bytes([min(95, 40 + int(t * 2)) + 40]),
            0x5E: lambda: u16(int(0.9 * 20)),                   # 0.9 L/h at idle
            0xA6: lambda: (1234567).to_bytes(4, "big"),         # 123456.7 km
        }
        fn = values.get(pid)
        return fn() if fn is not None and pid in ecu.pids else None

    def _status(self, ecu: _Ecu) -> bytes:
        """Monitor status (PID 01): MIL with the stored count; spark ignition; evap incomplete. After
        a clear every monitor is incomplete again and the MIL is off."""
        if ecu is self.tcm:
            return bytes([0x00, 0b0000_0100, 0x00, 0x00])      # components only, complete
        mil = 0 if self.cleared or not ecu.stored else 0x80 | len(ecu.stored)
        if self.cleared:
            return bytes([mil, 0b0111_0111, 0b1110_0101, 0b1110_0101])
        return bytes([mil, 0b0000_0111, 0b1110_0101, 0b0000_0100])

    @classmethod
    def _support(cls, head: bytes, pids, base: int) -> list[bytes]:
        """A 'supported' answer for ``base`` (0x00, 0x20 ...): only for ranges the ECU reaches."""
        if base == 0 or any(p > base for p in pids):
            return [head + cls._bitmask(pids, base)]
        return []

    @staticmethod
    def _bitmask(pids, base: int) -> bytes:
        bits = 0
        for p in pids:
            if base < p <= base + 0x20:
                bits |= 1 << (31 - (p - base - 1))
        if any(p > base + 0x20 for p in pids):                 # the "next range supported" bit
            bits |= 1
        return bits.to_bytes(4, "big")

    # --- request handling ---
    _FREEZE = {0x02: lambda: obd.encode_dtc("P0133"), 0x03: lambda: bytes([0x02, 0x00]),
               0x04: lambda: bytes([int(0.31 * 255)]), 0x05: lambda: bytes([86 + 40]), 0x06: lambda: bytes([140]),
               0x07: lambda: bytes([137]), 0x0B: lambda: bytes([45]), 0x0C: lambda: (2150 * 4).to_bytes(2, "big"),
               0x0D: lambda: bytes([72]), 0x0F: lambda: bytes([31 + 40]), 0x11: lambda: bytes([int(0.21 * 255)])}
    _MODE06 = {   # MID -> records (TID, UASID, value, min, max), raw
        0x01: [(0x05, 0x10, 72, 0, 150), (0x06, 0x10, 64, 0, 150)],     # O2 B1S1 switch times, ms
        0x21: [(0x80, 0x20, 220, 0, 192)],                              # catalyst B1: ratio 0.86 > 0.75: FAIL
        0x3B: [(0x80, 0x2F, 1150, 0, 4000)],                            # EVAP 0.040": 11.5 % of limit
    }
    _INFO_PIDS = (0x02, 0x04, 0x06, 0x08, 0x0A)

    def _response(self, ecu: _Ecu, body: bytes, functional: bool) -> list[bytes]:
        """The answer bodies (usually one; Mode 04 sends 'pending' first) for a request body."""
        mode, pid = body[0], body[1] if len(body) > 1 else None
        ecm = ecu is self.ecm
        if mode >= 0x10:
            return [r] if ecm and (r := self._uds_response(body)) is not None else []
        if mode == 0x01 and pid is not None:
            if pid in obd.SUPPORT_PIDS:
                return self._support(bytes([0x41, pid]), ecu.pids, pid)
            data = self._live(ecu, pid)
            return [bytes([0x41, pid]) + data] if data is not None else []
        if mode == 0x02 and pid is not None and ecm and not self.cleared:
            frame = body[2] if len(body) > 2 else 0
            if frame != 0:
                return []
            if pid in obd.SUPPORT_PIDS:
                return self._support(bytes([0x42, pid, 0]), self._FREEZE, pid)
            fn = self._FREEZE.get(pid)
            return [bytes([0x42, pid, 0]) + fn()] if fn else []
        if mode in (0x03, 0x07, 0x0A):
            codes = {0x03: ecu.stored, 0x07: ecu.pending, 0x0A: ecu.permanent}[mode]
            return [bytes([mode + 0x40, len(codes)]) + b"".join(obd.encode_dtc(c) for c in codes)]
        if mode == 0x04:
            self._clear()
            return ([bytes([0x7F, 0x04, 0x78])] if ecm else []) + [bytes([0x44])]
        if mode == 0x06 and pid is not None and ecm:
            if pid in obd.SUPPORT_PIDS:
                return self._support(bytes([0x46, pid]), self._MODE06, pid)
            recs = [] if self.cleared else self._MODE06.get(pid)
            if recs is None:
                return []
            return [bytes([0x46]) + b"".join(bytes([pid, tid, uas]) + b"".join(v.to_bytes(2, "big") for v in vals)
                                             for tid, uas, *vals in recs)]
        if mode == 0x08 and pid is not None and ecm:
            if pid == 0x00:
                return [bytes([0x48, 0x00]) + self._bitmask([0x01], 0)]
            if pid == 0x01:
                if self.evap_test_running:
                    return [bytes([0x7F, 0x08, 0x22])]          # already running: conditions not correct
                self.evap_test_running = True
                return [bytes([0x48, 0x01])]
            return [bytes([0x7F, 0x08, 0x12])]
        if mode == 0x09 and pid is not None:
            return self._info(ecu, pid)
        return [bytes([0x7F, mode, 0x11])] if not functional else []

    def _info(self, ecu: _Ecu, pid: int) -> list[bytes]:
        pids = self._INFO_PIDS if ecu is self.ecm else (0x04, 0x06, 0x0A)
        if pid in (0x00, 0x20, 0x40):
            return self._support(bytes([0x49, pid]), pids, pid)
        if pid not in pids:
            return []
        if pid == 0x02:
            return [bytes([0x49, 0x02, 0x01]) + VIN.encode("ascii")]
        if pid == 0x04:
            return [bytes([0x49, 0x04, 0x01]) + ecu.calid.encode("ascii").ljust(16, b"\x00")]
        if pid == 0x06:
            return [bytes([0x49, 0x06, 0x01]) + (bytes([0x12, 0x34, 0x56, 0x78]) if ecu is self.ecm else
                                                 bytes([0x9A, 0xBC, 0xDE, 0xF0]))]
        if pid == 0x08:
            counts = [120, 450, 98, 120, 0, 0, 101, 120, 0, 0, 87, 120, 0, 0, 61, 120, 99, 120, 0, 0]
            return [bytes([0x49, 0x08, len(counts)]) + b"".join(c.to_bytes(2, "big") for c in counts)]
        name = b"ECM\x00-EngineControl" if ecu is self.ecm else b"TCM\x00-TransmissionCtl"
        return [bytes([0x49, 0x0A, 0x01]) + name.ljust(20, b"\x00")[:20]]

    def _clear(self):
        self.cleared = True
        self.evap_test_running = False
        for ecu in self.ecus:
            ecu.stored, ecu.pending = [], []        # permanent codes stay until the monitor passes

    def _uds_response(self, body: bytes) -> bytes | None:
        """A small UDS (ISO 14229) responder: sessions, tester present, DTCs by status (0x19 0x02),
        clear (0x14), ECU reset (0x11), read / write data by identifier (0x22 / 0x2E)."""
        sid = body[0]
        if sid == 0x10:                                    # DiagnosticSessionControl -> positive
            return bytes([0x50, body[1] if len(body) > 1 else 0x01, 0x00, 0x32, 0x01, 0xF4])
        if sid == 0x3E:                                    # TesterPresent
            return bytes([0x7E, 0x00])
        if sid == 0x19 and len(body) >= 2 and body[1] == 0x02:   # ReadDTCInformation by status mask
            return bytes([0x59, 0x02, 0xFF]) + b"".join(bytes(d) for d in self.uds_dtcs)
        if sid == 0x14 and len(body) == 4:                 # ClearDiagnosticInformation
            self.uds_dtcs = []
            return bytes([0x54])
        if sid == 0x11 and len(body) == 2:                 # ECUReset
            self.resets += 1
            return bytes([0x51, body[1]])
        if sid == 0x22 and len(body) >= 3:                 # ReadDataByIdentifier
            did = (body[1] << 8) | body[2]
            if did in self.uds_data:
                return bytes([0x62, body[1], body[2]]) + self.uds_data[did]
            return bytes([0x7F, 0x22, 0x31])               # requestOutOfRange for unknown DIDs
        if sid == 0x2E and len(body) >= 4:                 # WriteDataByIdentifier
            self.uds_data[(body[1] << 8) | body[2]] = bytes(body[3:])
            return bytes([0x6E, body[1], body[2]])
        return bytes([0x7F, sid, 0x11])                    # serviceNotSupported

    # --- ISO-TP ---
    def _send_message(self, ecu: _Ecu, body: bytes):
        """Send a response as ISO-TP: single frame if it fits, else FF then wait for flow control."""
        frames = obd.segment(body, pad=0x00)
        self._tx(ecu, frames[0])
        ecu.tx_cf = frames[1:]

    def _tx(self, ecu: _Ecu, data: bytes):
        self.backend.send(CanFrame(ecu.response_id, bytes(data), extended=self.extended, timestamp=time.time()))

    def _on_frame(self, can_id: int, data: bytes):
        if not data:
            return
        functional = can_id == self.functional_id
        for ecu in self.ecus:
            if not functional and can_id != ecu.request_id:
                continue
            pci = data[0] >> 4
            if pci == 0x3 and not functional:              # flow control: release queued frames
                for cf in ecu.tx_cf:
                    self._tx(ecu, cf)
                ecu.tx_cf = []
                continue
            done = ecu.rx.feed(can_id, data)
            if ecu.rx.needs_flow_control:                  # a long request: clear to send
                ecu.rx.needs_flow_control.clear()
                self._tx(ecu, obd.flow_control_frame())
            if done is None or not done[1]:
                continue
            for resp in self._response(ecu, done[1], functional):
                self._send_message(ecu, resp)

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
        ids = {self.functional_id} | {e.request_id for e in self.ecus}
        while self._running:
            try:
                fr = self.backend.recv()
            except OSError:
                time.sleep(0.2)
                continue
            if fr is None or fr.error or fr.extended != self.extended or fr.can_id not in ids:
                continue
            self._on_frame(fr.can_id, bytes(fr.data))
