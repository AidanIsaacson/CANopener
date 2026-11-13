"""UDS / ISO 14229 trouble codes.

A UDS DTC is 3 bytes: the first two are the same P/C/B/U code the OBD-II 2-byte form encodes
(ISO 15031-6 / SAE J2012), and the third is a failure-type byte (FTB) — e.g. P0133-00. Each code
in a ReadDTCInformation response is followed by a 1-byte status (the standard status bits).
"""

from ..obd.dtc import decode_dtc
from .services import parse_response, status_flags

# The failure-type byte: what kind of fault, the same for every code (ISO 14229-1 / SAE J2012).
# The first digit is the category; these are the types that turn up most.
FAILURE_CATEGORIES = {
    0x0: "general failure", 0x1: "electrical failure", 0x2: "signal failure (frequency / pulse)",
    0x3: "signal failure (PWM)", 0x4: "internal failure", 0x5: "programming failure",
    0x6: "algorithm-based failure", 0x7: "mechanical failure", 0x8: "bus signal / message failure",
    0x9: "component failure",
}
FAILURE_TYPES = {
    0x00: "no sub-type", 0x01: "general electrical failure", 0x02: "general signal failure",
    0x04: "internal failure", 0x07: "mechanical failure", 0x08: "bus signal / message failure",
    0x09: "component failure",
    0x11: "short to ground", 0x12: "short to battery", 0x13: "open circuit",
    0x14: "short to ground or open", 0x15: "short to battery or open", 0x16: "voltage below threshold",
    0x17: "voltage above threshold", 0x1C: "voltage out of range", 0x1F: "intermittent circuit",
    0x29: "signal invalid", 0x31: "no signal", 0x38: "signal frequency incorrect",
    0x41: "checksum failure", 0x42: "memory failure", 0x44: "data memory failure",
    0x45: "program memory failure", 0x46: "calibration / parameter memory failure", 0x47: "watchdog failure",
    0x49: "internal electronic failure", 0x4B: "over temperature",
    0x51: "not programmed", 0x54: "missing calibration", 0x55: "not configured",
    0x62: "signal compare failure", 0x64: "signal plausibility failure",
    0x71: "actuator stuck", 0x72: "actuator stuck open", 0x73: "actuator stuck closed",
    0x81: "invalid data received", 0x82: "alive counter incorrect", 0x83: "signal protection (checksum) incorrect",
    0x86: "signal invalid", 0x87: "missing message", 0x88: "bus off",
    0x92: "performance or incorrect operation", 0x96: "component internal failure",
    0x97: "component or system operation obstructed", 0x98: "component or system over temperature",
}


def failure_type(ftb: int) -> str:
    """What a code's last byte says about the kind of fault ("short to ground"), or its
    category when the exact type isn't in the table."""
    return FAILURE_TYPES.get(ftb) or FAILURE_CATEGORIES.get(ftb >> 4, "")


def decode_uds_dtc(b0: int, b1: int, b2: int) -> str:
    """3-byte UDS DTC -> "P0133-00" (the OBD-style code plus the failure-type byte)."""
    return f"{decode_dtc(b0, b1)}-{b2:02X}"


def parse_dtc_by_status_mask(payload: bytes) -> list[tuple[str, int, list[str]]]:
    """Parse a ReadDTCInformation (0x19 / sub-function 0x02) *payload* (the bytes after the echoed
    service id). Layout: sub-function echo, status-availability mask, then records of 3 DTC bytes +
    1 status byte. Returns ``[(code, status_byte, [flag names])]``."""
    if len(payload) < 2:
        return []
    body = payload[2:]              # skip the echoed sub-function and the availability mask
    out = []
    for i in range(0, len(body) - 3, 4):
        b0, b1, b2, status = body[i:i + 4]
        if (b0, b1, b2) == (0, 0, 0):
            continue
        out.append((decode_uds_dtc(b0, b1, b2), status, status_flags(status)))
    return out


def read_dtcs(response: bytes) -> list[tuple[str, int, list[str]]]:
    """From a full 0x19 response (starting 0x59): the list of (code, status, flags)."""
    return parse_dtc_by_status_mask(parse_response(0x19, response))
