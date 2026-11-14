"""A simulated "thin" K-line engine computer, for the Bluetooth OBD simulator (``sim:thin-kline``).

Modelled on how the first real one we met behaved (docs/BLUETOOTH-OBD.md, "Thin K-line
computers"), so the deep probe, ``can.kwp`` and the front-ends can be exercised against the
awkward cases without a machine:

* **Generic OBD-II (ISO 9141-2) is a shell**: the 'supported' requests answer with zeros, the code
  read says none even though a code is stored, clearing (Mode 04) is accepted, the rest is NO DATA.
* **The real diagnostics are a KWP2000 session**: fast init only, only to module 0x10 **from
  tester 0xFC**. In it, identifiers read as ``22 hh ll 04 01`` and data blocks as ``21 nn 04 01``;
  without the ending every number is refused the same way (``7F xx 12``), and an unknown
  identifier in the right shape is ``7F 22 10``. ``21 nn 04`` (no count) answers 256 times over.
  One identification read takes several 'response pending' answers and is refused with the
  engine running.
* **One identifier page is a trap**: page 0x20 answers every number in a row, and reading
  ``22 20 44`` makes the computer go silent for a while and **stops the engine** (the battery
  voltage drops from charging to resting). This is what a sweep of an undescribed page did.

The identifier numbers, scalings and ID text here are made up for the simulator. They are not
any maker's.
"""

import time

ECU, TESTER = 0x10, 0xFC
HEADER = f"81{ECU:02X}{TESTER:02X}"
KEY_BYTES = "1:5D 2:8F"
TRAP_PAGE, TRAP_AT = 0x20, 0x44
RUNNING_VOLTS, RESTING_VOLTS = "14.3V", "12.4V"
ECU_ID = b"SIMTHIN"


class ThinKline:
    def __init__(self):
        self.engine_running = True
        self.rpm = 720
        self.coolant = 85               # deg C
        self.code = b"\x03\x35"         # stored, but only the KWP side shows it
        self.session = ""               # "" / "obd" / "kwp"
        self.silent_seconds = 20.0
        self.silent_until = 0.0
        self.stalls = 0

    # --- what the adapter asks ---
    def voltage(self) -> str:
        return RUNNING_VOLTS if self.engine_running else RESTING_VOLTS

    @property
    def silent(self) -> bool:
        return time.time() < self.silent_until

    def close(self):
        self.session = ""

    def wake(self, protocol: str, header: str, fast: bool) -> list[str]:
        """ATFI / ATSI."""
        if self.silent or not (fast and protocol == "5" and header == HEADER):
            return ["BUS INIT: ERROR" if fast else "BUS INIT: ...ERROR"]
        self.session = "kwp"
        return ["BUS INIT: OK"]

    def request(self, protocol: str, header: str, payload: bytes) -> list[str]:
        if self.silent:
            return ["NO DATA"]
        if self.session == "kwp":
            return self._kwp(payload)
        if protocol == "3" and not header:
            first = [] if self.session == "obd" else ["BUS INIT: ...OK"]
            self.session = "obd"
            return first + self._obd(payload)
        if protocol == "5" and header == HEADER:        # a request wakes it too
            self.session = "kwp"
            return ["BUS INIT: OK"] + self._kwp(payload)
        return ["BUS INIT: ERROR" if protocol == "5" else "BUS INIT: ...ERROR"]

    # --- generic OBD-II: a shell ---
    def _obd(self, payload: bytes) -> list[str]:
        answers = {b"\x01\x00": "410000000000", b"\x01\x20": "412000000000", b"\x02\x00\x00": "42000000000000",
                   b"\x09\x00": "49000100000000", b"\x03": "43000000000000"}
        if payload == b"\x04":
            self.code = b"\x00\x00"
            return [self._line(bytes([0x48, 0x6B, ECU]), b"\x44")]
        body = answers.get(bytes(payload))
        return [self._line(bytes([0x48, 0x6B, ECU]), bytes.fromhex(body))] if body else ["NO DATA"]

    # --- the KWP2000 session ---
    def _kwp(self, p: bytes) -> list[str]:
        sid = p[0]

        def out(data: bytes) -> str:
            return self._line(bytes([0x80 | len(data), TESTER, ECU]), data)

        def no(code: int) -> list[str]:
            return [out(bytes([0x7F, sid, code]))]
        if sid == 0x3E:
            return [out(b"\x7e")]
        if sid == 0x1A and len(p) == 2:
            if p[1] == 0x80:
                return [out(b"\x5a\x00\x00")]
            if p[1] == 0x81:
                return [out(b"\x5a" + ECU_ID)]
            if p[1] == 0x82:
                return [out(b"\x5a" + ECU_ID + b"10" + b"\xff" * 4)]
            if p[1] == 0x83:
                if self.engine_running:
                    return no(0x94)
                return no(0x78) * 3 + [out(b"\x5a\x01\x02\x03\x04")]
            return no(0x10)
        if sid == 0x21 and len(p) >= 2:
            block = out(bytes([0x61, p[1], 0x00, p[1], 0x00, 0x00]))
            if p[1] > 5 or len(p) == 2 or p[2:] not in (b"\x04", b"\x04\x01"):
                return no(0x12)
            return [block] * (1 if len(p) == 4 else 256)
        if sid == 0x22:
            if len(p) not in (4, 5) or p[3:] not in (b"\x04", b"\x04\x01"):
                return no(0x12)
            data = self._identifier(p[1], p[2])
            if data is None:
                return ["NO DATA"] if self.silent else no(0x10)
            return [out(bytes([0x62, p[1], p[2]]) + data)]
        if sid == 0x12:
            return no(0x12)
        return no(0x11)

    def _identifier(self, hi: int, lo: int) -> bytes | None:
        cid = (hi << 8) | lo
        if hi == TRAP_PAGE:
            if lo >= TRAP_AT:                           # the trap: silent, and the engine stops
                self.engine_running, self.rpm = False, 0
                self.silent_until = time.time() + self.silent_seconds
                self.session = ""
                self.stalls += 1
                return None
            return b"\x00"
        values = {0x1100: b"\x00\x00\x00\x01", 0x1101: b"\x01", 0x1110: bytes([self.coolant + 40]),
                  0x1111: bytes([round(float(self.voltage().rstrip("V")) * 10)]),
                  0x1200: b"\x00\x00\x00\x01", 0x1201: b"\x00\x00",
                  0x1210: (self.rpm if self.engine_running else 0).to_bytes(2, "big"), 0x1220: self.code,
                  0x1300: b"\x00\x00\x00\x01", 0x1301: b"\x00\x01", 0x1310: b"\x10\x08"}
        return values.get(cid)

    @staticmethod
    def _line(head: bytes, data: bytes) -> str:
        msg = head + data
        return (msg + bytes([sum(msg) & 0xFF])).hex().upper()
