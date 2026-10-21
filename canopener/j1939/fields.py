"""Little-endian field extraction.

Each helper returns None for J1939 'not available' / 'error' raw values (anything above
0xFA, 0xFAFF or 0xFAFFFFFF) or when the payload is too short.
"""


def u8(d, i, scale=1.0, off=0.0):
    if i >= len(d) or d[i] > 0xFA:
        return None
    return d[i] * scale + off


def u16(d, i, scale=1.0, off=0.0):
    if i + 1 >= len(d):
        return None
    v = d[i] | (d[i + 1] << 8)
    return None if v > 0xFAFF else v * scale + off


def u32(d, i, scale=1.0, off=0.0):
    if i + 3 >= len(d):
        return None
    v = d[i] | (d[i + 1] << 8) | (d[i + 2] << 16) | (d[i + 3] << 24)
    return None if v > 0xFAFFFFFF else v * scale + off


_READERS = {1: u8, 2: u16, 4: u32}


def read(d, start, length, scale=1.0, off=0.0, na_check=True):
    """Read an unsigned little-endian field. ``na_check=False`` skips the J1939 'not available'
    convention, for proprietary frames where 0xFF.. can be a real value."""
    if na_check:
        return _READERS[length](d, start, scale, off)
    if start + length > len(d):
        return None
    return int.from_bytes(bytes(d[start:start + length]), "little") * scale + off


def read_bits(d, start, bit, nbits, scale=1.0, off=0.0, signed=False, na_check=True):
    """Read a bit field in J1939 (little-endian / Intel) layout.

    The field's least significant bit is bit ``bit`` (0 = LSB) of byte ``start``; it may run
    on into the following bytes. With ``na_check`` an unsigned field of 2+ bits that is all ones
    (and, for 8/16/32-bit fields, anything in the J1939 error / not-available range) reads as
    None. Single-bit flags have no "not available" value: 1 is just "on".
    """
    nbytes = (bit + nbits + 7) // 8
    if nbits <= 0 or start + nbytes > len(d):
        return None
    raw = (int.from_bytes(bytes(d[start:start + nbytes]), "little") >> bit) & ((1 << nbits) - 1)
    if signed:
        if raw >> (nbits - 1):
            raw -= 1 << nbits
    elif na_check and nbits >= 2:
        limit = {8: 0xFA, 16: 0xFAFF, 32: 0xFAFFFFFF}.get(nbits)
        if (limit is not None and raw > limit) or raw == (1 << nbits) - 1:
            return None
    return raw * scale + off


def encode_bits(buf: bytearray, start, bit, nbits, value, scale=1.0, off=0.0, signed=False):
    """Inverse of ``read_bits``, written into ``buf`` in place (None = all ones)."""
    nbytes = (bit + nbits + 7) // 8
    mask = (1 << nbits) - 1
    if value is None:
        raw = mask
    else:
        raw = round((value - off) / scale)
        raw = raw & mask if signed else max(0, min(raw, mask))
    cur = int.from_bytes(bytes(buf[start:start + nbytes]), "little")
    cur = (cur & ~(mask << bit)) | (raw << bit)
    buf[start:start + nbytes] = cur.to_bytes(nbytes, "little")


def encode(value, length, scale=1.0, off=0.0) -> bytes:
    """Inverse of ``read``; None encodes as 'not available' (all 0xFF)."""
    if value is None:
        return b"\xff" * length
    raw = round((value - off) / scale)
    limit = {1: 0xFA, 2: 0xFAFF, 4: 0xFAFFFFFF}[length]
    raw = max(0, min(raw, limit))
    return raw.to_bytes(length, "little")


def hexs(d) -> str:
    return " ".join(f"{b:02X}" for b in d)
