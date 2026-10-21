"""Interpret a reassembled OBD-II response (the bytes from ``IsoTpReassembler.feed``).

A positive response echoes the request mode + 0x40 (Mode 01 -> 0x41, 03 -> 0x43, ...). A negative
response is ``0x7F <mode> <code>``. These helpers turn a response body into something displayable
without knowing how it arrived (single- or multi-frame).
"""

from dataclasses import dataclass, field

from .dtc import decode_dtc_list
from .pids import PIDS, Pid, supported_pids

MODE_CURRENT_DATA = 0x01
MODE_FREEZE_FRAME = 0x02
MODE_STORED_DTCS = 0x03
MODE_CLEAR_DTCS = 0x04          # a write; never sent from this read-only package
MODE_PENDING_DTCS = 0x07
MODE_VEHICLE_INFO = 0x09
MODE_PERMANENT_DTCS = 0x0A

POSITIVE_OFFSET = 0x40
NEGATIVE = 0x7F

NRC = {0x10: "general reject", 0x11: "service not supported", 0x12: "sub-function not supported",
       0x21: "busy, repeat request", 0x22: "conditions not correct", 0x31: "request out of range",
       0x78: "response pending"}


@dataclass
class ObdResponse:
    mode: int
    ok: bool = True
    pid: int | None = None
    value: float | None = None
    unit: str = ""
    name: str = ""
    dtcs: list[str] = field(default_factory=list)
    text: str = ""                  # VIN / cal id and similar ASCII
    supported: list[int] = field(default_factory=list)
    error: str = ""
    raw: bytes = b""


def decode(payload: bytes) -> ObdResponse | None:
    """Decode a response body. Returns None if it's empty/too short to classify."""
    if not payload:
        return None
    if payload[0] == NEGATIVE:
        mode = payload[1] if len(payload) > 1 else 0
        code = payload[2] if len(payload) > 2 else 0
        return ObdResponse(mode, ok=False, error=NRC.get(code, f"NRC 0x{code:02X}"), raw=payload)

    mode = payload[0] - POSITIVE_OFFSET
    r = ObdResponse(mode, raw=payload)

    if mode in (MODE_CURRENT_DATA, MODE_FREEZE_FRAME) and len(payload) >= 2:
        r.pid = payload[1]
        data = payload[2:]
        if r.pid in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
            r.name = f"Supported PIDs {r.pid:#04x}"
            r.supported = supported_pids(r.pid, data)
        else:
            pid: Pid | None = PIDS.get(r.pid)
            if pid is not None:
                r.name, r.unit, r.value = pid.name, pid.unit, round(pid.decode(data), 3)
            else:
                r.name = f"PID {r.pid:#04x}"
        return r

    if mode in (MODE_STORED_DTCS, MODE_PENDING_DTCS, MODE_PERMANENT_DTCS):
        r.dtcs = decode_dtc_list(payload[1:])
        return r

    if mode == MODE_VEHICLE_INFO and len(payload) >= 2:
        r.pid = payload[1]
        body = payload[2:]
        if body and body[0] <= 8:       # leading "number of data items" byte
            body = body[1:]
        r.text = bytes(b for b in body if 0x20 <= b < 0x7F).decode("ascii", "replace").strip()
        r.name = "VIN" if r.pid == 0x02 else f"Info {r.pid:#04x}"
        return r

    return r
