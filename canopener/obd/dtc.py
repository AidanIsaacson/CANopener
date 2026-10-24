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

# --- generic (SAE J2012 / ISO 15031-6) descriptions -----------------------------------
#
# Only *generic* codes are described: those the standard defines the same for every maker (P0xxx,
# P2xxx, P34xx, U0xxx ...). Manufacturer codes (P1xxx, P30xx-P33xx, C1/B1/U1xxx, ...) mean
# different things per OEM, so they get no text -- the tool must not invent one. ``is_generic``
# says which is which; OEM meanings belong in a plugin.

from .dtc_codes import DTC as _TABLE  # noqa: E402 - the big table lives in its own (generated) file

# The third character of a P-code selects the subsystem (SAE J2012). Verified, structural -- not
# a guess about any particular code.
_P_SUBSYSTEM = {
    "0": "Fuel & air metering / aux emission controls",
    "1": "Fuel & air metering",
    "2": "Fuel & air metering (injector circuit)",
    "3": "Ignition system or misfire",
    "4": "Auxiliary emission controls",
    "5": "Vehicle speed, idle control & auxiliary inputs",
    "6": "Computer output circuit",
    "7": "Transmission",
    "8": "Transmission",
    "9": "Transmission",
    "A": "Hybrid propulsion",
    "B": "Hybrid propulsion",
    "C": "Hybrid propulsion",
}
_LETTER_SYSTEM = {"P": "Powertrain", "C": "Chassis", "B": "Body", "U": "Network / communication"}

GENERIC_DTC: dict[str, str] = _TABLE

# The bundled text leaves out which bank / sensor a code is for (P0420 and P0430 read the same).
# SAE J2012 assigns them by code range; these are the well-established ranges. (first, last, where)
_LOCATIONS = [
    (0x0010, 0x0015, "Bank 1"), (0x0020, 0x0025, "Bank 2"),
    (0x0016, 0x0016, "Bank 1 Sensor A"), (0x0017, 0x0017, "Bank 1 Sensor B"),
    (0x0018, 0x0018, "Bank 2 Sensor A"), (0x0019, 0x0019, "Bank 2 Sensor B"),
    (0x0030, 0x0032, "Bank 1 Sensor 1"), (0x0036, 0x0038, "Bank 1 Sensor 2"),
    (0x0050, 0x0052, "Bank 2 Sensor 1"), (0x0056, 0x0058, "Bank 2 Sensor 2"),
    (0x0130, 0x0135, "Bank 1 Sensor 1"), (0x0136, 0x0141, "Bank 1 Sensor 2"), (0x0142, 0x0147, "Bank 1 Sensor 3"),
    (0x0150, 0x0155, "Bank 2 Sensor 1"), (0x0156, 0x0161, "Bank 2 Sensor 2"), (0x0162, 0x0167, "Bank 2 Sensor 3"),
    (0x0170, 0x0172, "Bank 1"), (0x0173, 0x0175, "Bank 2"),
    (0x0420, 0x0429, "Bank 1"), (0x0430, 0x0439, "Bank 2"),
    (0x2096, 0x2097, "Bank 1"), (0x2098, 0x2099, "Bank 2"),
    (0x2195, 0x2196, "Bank 1 Sensor 1"), (0x2197, 0x2198, "Bank 2 Sensor 1"),
    (0x2270, 0x2271, "Bank 1 Sensor 2"), (0x2272, 0x2273, "Bank 2 Sensor 2"),
]


def dtc_location(code: str) -> str:
    """'Bank 1 Sensor 1' etc. for the P-code ranges that encode one, else ''."""
    if len(code) != 5 or code[0] != "P":
        return ""
    try:
        n = int(code[1:], 16)
    except ValueError:
        return ""
    return next((where for lo, hi, where in _LOCATIONS if lo <= n <= hi), "")


def is_generic(code: str) -> bool:
    """True when ``code`` is an SAE/ISO generic code (same meaning for every maker), as opposed to
    a manufacturer-specific one. Generic: the P0/C0/B0/U0 groups, the jointly-defined P2 and
    U0/U2/U3 network ranges, and P3400-P34FF; manufacturer: P1, the rest of P3, C1/C2/C3,
    B1/B2/B3, U1."""
    if len(code) != 5:
        return False
    letter, grp = code[0], code[1]
    if letter == "P":
        return grp in ("0", "2") or code[1:3] == "34"
    if letter == "U":
        return grp in ("0", "2", "3")
    return grp == "0"


def dtc_subsystem(code: str) -> str:
    """A standard subsystem label from the code's structure alone (no lookup table)."""
    if len(code) != 5:
        return "Unknown"
    system = _LETTER_SYSTEM.get(code[0], "Unknown")
    if code[0] == "P":
        return f"{system}: {_P_SUBSYSTEM.get(code[2].upper(), 'other')}"
    return system


def describe_dtc(code: str) -> str:
    """Best available description: the generic text if known, else a subsystem-level
    label from the code structure. Manufacturer-specific codes say so (their meaning is OEM)."""
    if code in GENERIC_DTC:
        text, where = GENERIC_DTC[code], dtc_location(code)
        return f"{text} ({where})" if where and "Bank" not in text else text
    if not is_generic(code):
        return f"{dtc_subsystem(code)} — manufacturer-specific (see OEM/plugin data)"
    return dtc_subsystem(code)
