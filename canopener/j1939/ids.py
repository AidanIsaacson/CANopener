"""29-bit J1939 identifier packing and unpacking."""

from dataclasses import dataclass

from .constants import GLOBAL_SA


@dataclass(frozen=True, slots=True)
class J1939Id:
    priority: int
    pgn: int
    sa: int
    da: int = GLOBAL_SA     # GLOBAL_SA for PDU2 (broadcast) PGNs

    @property
    def is_pdu1(self) -> bool:
        return ((self.pgn >> 8) & 0xFF) < 240


def parse_id(can_id: int) -> J1939Id:
    priority = (can_id >> 26) & 0x7
    dp = (can_id >> 24) & 0x3          # EDP + DP
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    sa = can_id & 0xFF
    if pf < 240:                       # PDU1: PS is the destination address
        return J1939Id(priority, (dp << 16) | (pf << 8), sa, ps)
    return J1939Id(priority, (dp << 16) | (pf << 8) | ps, sa, GLOBAL_SA)


def build_id(pgn: int, sa: int, da: int = GLOBAL_SA, priority: int = 6) -> int:
    pf = (pgn >> 8) & 0xFF
    if pf < 240:
        pgn = (pgn & 0x3FF00) | (da & 0xFF)
    return ((priority & 0x7) << 26) | ((pgn & 0x3FFFF) << 8) | (sa & 0xFF)


def pgn_bytes(pgn: int) -> bytes:
    """PGN as the 3 little-endian bytes used in requests, ACKs and TP.CM."""
    return bytes([pgn & 0xFF, (pgn >> 8) & 0xFF, (pgn >> 16) & 0xFF])


def pgn_from_bytes(b) -> int:
    return b[0] | (b[1] << 8) | (b[2] << 16)
