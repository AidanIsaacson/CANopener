"""PDO mapping: which objects a node packs into each process-data message, and where.

A node describes its own PDOs in its object dictionary: for transmit PDO *n* (1-based), object
``0x1800 + n - 1`` sub 1 is the CAN ID it's sent on and ``0x1A00 + n - 1`` lists what's in it, one
entry per mapped object (``index``, ``sub``, bit length), packed in that order from bit 0 up.
Reading those (``core.canopen.CanopenClient.read_tpdos``) or taking them from the device's EDS
file (``canopen.eds``) is enough to split a PDO into its values without a hand-written profile.
What each value *means* (its name, scale and unit) is in the EDS or the device's manual.
"""

from dataclasses import dataclass

TPDO_COMM, TPDO_MAP = 0x1800, 0x1A00
RPDO_COMM, RPDO_MAP = 0x1400, 0x1600
COB_INVALID = 0x80000000        # bit 31 of the COB-ID: this PDO is switched off
COB_EXTENDED = 0x20000000       # bit 29: a 29-bit ID (rare)
MAX_PDOS = 512


@dataclass(frozen=True)
class Mapped:
    """One mapped object. ``index`` below 0x20 is a "dummy" entry: padding, not a value."""
    index: int
    sub: int
    bits: int

    @property
    def is_padding(self) -> bool:
        return self.index < 0x20

    @classmethod
    def from_value(cls, value: int) -> "Mapped":
        return cls((value >> 16) & 0xFFFF, (value >> 8) & 0xFF, value & 0xFF)


@dataclass(frozen=True)
class Pdo:
    number: int                 # 1-based
    cob_id: int                 # as stored in the node (may have the invalid / extended bits)
    mapped: tuple[Mapped, ...]
    transmit: bool = True

    @property
    def enabled(self) -> bool:
        return not self.cob_id & COB_INVALID

    @property
    def can_id(self) -> int | None:
        """The 11-bit ID it's sent on, or None (switched off, or a 29-bit ID)."""
        if not self.enabled or self.cob_id & COB_EXTENDED:
            return None
        return self.cob_id & 0x7FF

    @property
    def label(self) -> str:
        return f"{'TPDO' if self.transmit else 'RPDO'}{self.number}"


def signals(pdo: Pdo, namer=None, group: str = "", describe=None) -> list[dict]:
    """Profile ``signals`` entries (``core.profiles``) for a PDO's mapped objects.

    ``namer(index, sub) -> str`` names an object (default: ``2001:01``-style numbers);
    ``describe(index, sub) -> dict`` may add ``signed`` / ``unit`` / ``scale`` (from an EDS).
    Values wider than 32 bits, and anything in a switched-off PDO, are left out."""
    if pdo.can_id is None:
        return []
    out, pos = [], 0
    for m in pdo.mapped:
        first, pos = pos, pos + m.bits
        if m.is_padding or not 1 <= m.bits <= 32 or first + m.bits > 64:
            continue
        name = (namer(m.index, m.sub) if namer else "") or f"{m.index:04X}:{m.sub:02X}"
        sig = {"name": name, "id": f"0x{pdo.can_id:03X}", "start": first // 8}
        if first % 8 == 0 and m.bits in (8, 16, 32):
            sig["length"] = m.bits // 8
        else:
            sig["bit"], sig["bits"] = first % 8, m.bits
        if m.bits == 1:
            sig["enum"] = {"0": "Off", "1": "On"}
        else:
            sig["decimals"] = 0
        if group:
            sig["group"] = group
        if describe:
            sig.update({k: v for k, v in (describe(m.index, m.sub) or {}).items() if v not in (None, "")})
        out.append(sig)
    return out
