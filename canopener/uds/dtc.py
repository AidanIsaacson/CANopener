"""UDS / ISO 14229 trouble codes.

A UDS DTC is 3 bytes: the first two are the same P/C/B/U code the OBD-II 2-byte form encodes
(ISO 15031-6 / SAE J2012), and the third is a failure-type byte (FTB) — e.g. P0133-00. Each code
in a ReadDTCInformation response is followed by a 1-byte status (the standard status bits).
"""

from ..obd.dtc import decode_dtc
from .services import parse_response, status_flags


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
