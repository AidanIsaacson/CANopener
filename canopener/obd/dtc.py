"""OBD-II diagnostic trouble codes (SAE J2012): the 2-byte on-wire form <-> "P0133" text, and
parsing the Mode 03 / 07 / 0A responses that carry a list of them.

Each DTC is two bytes. The top two bits of the first byte pick the letter; the next two bits and
the remaining nibbles are the digits:

    bits 7-6: 00=P (powertrain) 01=C (chassis) 10=B (body) 11=U (network)
    bits 5-4: first digit 0-3
    bits 3-0: second digit (hex)
    byte 2  : third and fourth digits (hex)

e.g. 0x01 0x33 -> "P0133" (O2 sensor circuit).
"""

_LETTERS = ("P", "C", "B", "U")


def decode_dtc(b0: int, b1: int) -> str:
    letter = _LETTERS[(b0 >> 6) & 0x03]
    return f"{letter}{(b0 >> 4) & 0x03}{b0 & 0x0F:X}{b1 >> 4:X}{b1 & 0x0F:X}"


def encode_dtc(code: str) -> bytes:
    """"P0133" -> b'\\x01\\x33'. Raises ValueError on a malformed code."""
    code = code.strip().upper()
    if len(code) != 5 or code[0] not in _LETTERS or not all(c in "0123456789ABCDEF" for c in code[1:]):
        raise ValueError(f"not a DTC: {code!r}")
    b0 = (_LETTERS.index(code[0]) << 6) | (int(code[1]) << 4) | int(code[2], 16)
    b1 = (int(code[3], 16) << 4) | int(code[4], 16)
    return bytes([b0, b1])


def decode_dtc_list(payload: bytes) -> list[str]:
    """DTCs from a Mode 03/07/0A response body (the bytes after the service id).

    Layout is ``count`` then ``count`` 2-byte DTCs. Some ECUs omit the count and just pack pairs;
    we accept both by skipping a leading count byte only when it matches the pair count, and we
    drop the all-zero padding pair that means "no code".
    """
    if not payload:
        return []
    body = payload
    if len(payload) >= 1 and payload[0] == (len(payload) - 1) // 2:
        body = payload[1:]
    codes = []
    for i in range(0, len(body) - 1, 2):
        b0, b1 = body[i], body[i + 1]
        if b0 == 0 and b1 == 0:
            continue
        codes.append(decode_dtc(b0, b1))
    return codes
