"""Interpret a reassembled OBD-II response (the bytes from ``IsoTpReassembler.feed``).

A positive response echoes the request mode + 0x40 (Mode 01 -> 0x41, 03 -> 0x43, ...). A negative
response is ``0x7F <mode> <code>``. These helpers turn a response body into something displayable
without knowing how it arrived (single- or multi-frame) or which ECU sent it.

Modes (SAE J1979): 01 current data, 02 freeze frame, 03 stored codes, 04 clear codes, 06 on-board
test results, 07 pending codes, 08 control of an on-board system (tests), 09 vehicle info,
0A permanent codes.
"""

from dataclasses import dataclass, field

from .dtc import decode_dtc_list
from .monitors import SUPPORT_MIDS, TestResult, decode_results
from .pids import PIDS, SUPPORT_PIDS, supported_pids

MODE_CURRENT_DATA = 0x01
MODE_FREEZE_FRAME = 0x02
MODE_STORED_DTCS = 0x03
MODE_CLEAR_DTCS = 0x04
MODE_TEST_RESULTS = 0x06
MODE_PENDING_DTCS = 0x07
MODE_CONTROL = 0x08
MODE_VEHICLE_INFO = 0x09
MODE_PERMANENT_DTCS = 0x0A

POSITIVE_OFFSET = 0x40
NEGATIVE = 0x7F
RESPONSE_PENDING = 0x78

NRC = {0x10: "general reject", 0x11: "service not supported", 0x12: "sub-function not supported",
       0x21: "busy, repeat request", 0x22: "conditions not correct", 0x31: "request out of range",
       0x78: "response pending"}

# Mode 09 info types. Those with a leading "number of data items" byte on CAN are in _COUNTED.
VEHICLE_INFO = {0x02: "VIN", 0x04: "Calibration ID", 0x06: "Calibration verification (CVN)",
                0x08: "In-use performance (spark)", 0x0A: "ECU name", 0x0B: "In-use performance (compression)",
                0x0D: "Engine serial number", 0x0F: "Exhaust regulation / type approval number"}
_COUNTED = {0x02, 0x04, 0x06, 0x08, 0x0A, 0x0B, 0x0D, 0x0F}
_ASCII = {0x02, 0x0D, 0x0F}

# In-use performance tracking counters (SAE J1979), in transmitted order.
IPT_SPARK = ("OBD monitoring conditions", "Ignition cycles", "Catalyst B1 completions", "Catalyst B1 conditions",
             "Catalyst B2 completions", "Catalyst B2 conditions", "O2 sensor B1 completions",
             "O2 sensor B1 conditions", "O2 sensor B2 completions", "O2 sensor B2 conditions", "EGR/VVT completions",
             "EGR/VVT conditions", "Secondary air completions", "Secondary air conditions", "EVAP completions",
             "EVAP conditions", "Secondary O2 B1 completions", "Secondary O2 B1 conditions",
             "Secondary O2 B2 completions", "Secondary O2 B2 conditions")
IPT_COMPRESSION = ("OBD monitoring conditions", "Ignition cycles", "NMHC catalyst completions",
                   "NMHC catalyst conditions", "NOx catalyst completions", "NOx catalyst conditions",
                   "NOx adsorber completions", "NOx adsorber conditions", "PM filter completions",
                   "PM filter conditions",
                   "Exhaust gas sensor completions", "Exhaust gas sensor conditions", "EGR/VVT completions",
                   "EGR/VVT conditions", "Boost pressure completions", "Boost pressure conditions",
                   "Fuel system completions", "Fuel system conditions")

# Mode 08 test IDs. Only TID 0x01 is defined the same for every maker; others are OEM-defined.
CONTROL_TESTS = {0x01: "Evaporative system leak test"}


@dataclass
class ObdResponse:
    mode: int
    ok: bool = True
    pid: int | None = None
    value: float | None = None          # the first numeric value (single-value PIDs)
    unit: str = ""
    name: str = ""
    values: list = field(default_factory=list)     # every (name, value, unit) in the answer
    dtcs: list[str] = field(default_factory=list)
    text: str = ""                      # VIN / cal id and similar ASCII
    supported: list[int] = field(default_factory=list)
    results: list[TestResult] = field(default_factory=list)     # Mode 06
    frame: int | None = None            # Mode 02 freeze frame number
    nrc: int | None = None
    error: str = ""
    raw: bytes = b""

    @property
    def pending(self) -> bool:
        """A 'response pending' negative answer: the real one follows."""
        return not self.ok and self.nrc == RESPONSE_PENDING


def _pid_values(r: ObdResponse, data: bytes):
    if r.pid in SUPPORT_PIDS:
        r.name = f"Supported PIDs {r.pid + 1:#04x}-{r.pid + 0x20:#04x}"
        r.supported = supported_pids(r.pid, data)
        return
    pid = PIDS.get(r.pid)
    if pid is None:
        r.name = f"PID 0x{r.pid:02X}"
        r.values = [(r.name, data.hex(" ").upper(), "raw")]
        return
    r.name = pid.name
    r.values = pid.values(data)
    numeric = [v for v in r.values if isinstance(v[1], (int, float))]
    if numeric:
        r.value, r.unit = numeric[0][1], numeric[0][2]
    elif r.values:
        r.text = str(r.values[0][1])


def _vehicle_info(r: ObdResponse, body: bytes):
    if r.pid in (0x00, 0x20, 0x40, 0x60):
        r.name = "Supported info types"
        r.supported = supported_pids(r.pid, body)
        return
    r.name = VEHICLE_INFO.get(r.pid, f"Info 0x{r.pid:02X}")
    if r.pid in _COUNTED and body:
        body = body[1:]                 # number of data items
    if r.pid == 0x04:                   # calibration IDs: 16 ASCII bytes each
        r.text = ", ".join(_ascii(body[i:i + 16]) for i in range(0, len(body), 16) if _ascii(body[i:i + 16]))
    elif r.pid == 0x06:                 # CVNs: 4 binary bytes each
        r.text = " ".join(body[i:i + 4].hex().upper() for i in range(0, len(body), 4) if body[i:i + 4])
    elif r.pid in (0x08, 0x0B):
        names = IPT_SPARK if r.pid == 0x08 else IPT_COMPRESSION
        counts = [int.from_bytes(body[i:i + 2], "big") for i in range(0, len(body) - 1, 2)]
        r.values = [(names[i] if i < len(names) else f"Counter {i + 1}", float(c), "") for i, c in enumerate(counts)]
        r.text = f"{len(counts)} counters"
    elif r.pid == 0x0A:                 # ECU name: "ECM" NUL-padded, then "-EngineControl"
        r.text = _ascii(body.replace(b"\x00", b""))
    else:
        r.text = _ascii(body) if r.pid in _ASCII or body.isascii() else body.hex(" ").upper()


def _ascii(b: bytes) -> str:
    return bytes(c for c in b if 0x20 <= c < 0x7F).decode("ascii").strip()


def decode(payload: bytes) -> ObdResponse | None:
    """Decode a response body. Returns None if it's empty/too short to classify."""
    if not payload:
        return None
    if payload[0] == NEGATIVE:
        mode = payload[1] if len(payload) > 1 else 0
        code = payload[2] if len(payload) > 2 else 0
        return ObdResponse(mode, ok=False, nrc=code, error=NRC.get(code, f"NRC 0x{code:02X}"), raw=payload)

    mode = payload[0] - POSITIVE_OFFSET
    r = ObdResponse(mode, raw=payload)

    if mode == MODE_CURRENT_DATA and len(payload) >= 2:
        r.pid = payload[1]
        _pid_values(r, payload[2:])
    elif mode == MODE_FREEZE_FRAME and len(payload) >= 3:
        r.pid, r.frame = payload[1], payload[2]
        _pid_values(r, payload[3:])
    elif mode in (MODE_STORED_DTCS, MODE_PENDING_DTCS, MODE_PERMANENT_DTCS):
        r.dtcs = decode_dtc_list(payload[1:])
    elif mode == MODE_TEST_RESULTS and len(payload) >= 2:
        r.pid = payload[1]
        if r.pid in SUPPORT_MIDS and len(payload) == 6:
            r.name = "Supported monitors"
            r.supported = supported_pids(r.pid, payload[2:])
        else:
            r.results = decode_results(payload[1:])
    elif mode == MODE_CONTROL and len(payload) >= 2:
        r.pid = payload[1]
        if r.pid in SUPPORT_PIDS and len(payload) >= 6:
            r.name = "Supported tests"
            r.supported = supported_pids(r.pid, payload[2:])
        else:
            r.name = CONTROL_TESTS.get(r.pid, f"Test 0x{r.pid:02X}")
    elif mode == MODE_VEHICLE_INFO and len(payload) >= 2:
        r.pid = payload[1]
        _vehicle_info(r, payload[2:])
    return r
