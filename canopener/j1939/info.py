"""Decoders for identification and lifetime-counter PGNs."""

from . import fields
from .constants import (
    PGN_COMPONENT_ID,
    PGN_ECU_ID,
    PGN_ENGINE_HOURS,
    PGN_FUEL_CONSUMPTION,
    PGN_SOFTWARE_ID,
    PGN_VIN,
)


def decode_ascii_fields(d, skip_first: bool = False) -> list[str]:
    """Split a '*'-delimited ASCII payload into its printable fields."""
    raw = bytes(d[1:] if skip_first else d)
    s = raw.decode("ascii", "replace")
    parts = [p.strip().strip("\x00\xff") for p in s.split("*")]
    return [p for p in parts if p and all(32 <= ord(c) < 127 for c in p)]


def decode_ascii(d, skip_first: bool = False) -> str:
    parts = decode_ascii_fields(d, skip_first)
    return " | ".join(parts) if parts else f"(raw) {fields.hexs(bytes(d))}"


COMPONENT_FIELDS = ("Make", "Model", "Serial number", "Unit number")
ECU_ID_FIELDS = ("Part number", "Serial number", "Location", "Type", "Manufacturer")


def describe(pgn: int, data) -> str:
    d = bytes(data)
    if pgn == PGN_ENGINE_HOURS:
        hours = fields.u32(d, 0, 0.05)
        revs = fields.u32(d, 4, 1000)
        return f"{_fmt(hours, 'h', 2)}, {_fmt(revs, 'revs', 0)}"
    if pgn == PGN_FUEL_CONSUMPTION:
        return f"trip {_fmt(fields.u32(d, 0, 0.5), 'L')}, total {_fmt(fields.u32(d, 4, 0.5), 'L')}"
    if pgn == PGN_SOFTWARE_ID:
        return decode_ascii(d, skip_first=True)
    if pgn in (PGN_COMPONENT_ID, PGN_ECU_ID):
        names = COMPONENT_FIELDS if pgn == PGN_COMPONENT_ID else ECU_ID_FIELDS
        parts = decode_ascii_fields(d)
        if not parts:
            return decode_ascii(d)
        # Labels only line up when no field was empty, so only use them on an exact count.
        if len(parts) == len(names):
            return ", ".join(f"{n}: {p}" for n, p in zip(names, parts))
        return " | ".join(parts)
    if pgn == PGN_VIN:
        return decode_ascii(d)
    return fields.hexs(d)


def _fmt(v, unit, nd=1):
    return "n/a" if v is None else f"{v:,.{nd}f} {unit}"
