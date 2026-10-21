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
# Only *generic* codes are described here: those the standard defines the same for every maker
# (the "0" group, e.g. P0xxx, plus the jointly-defined P2xxx / U0xxx network codes). Manufacturer
# codes (P1xxx, P3xxx, C1/B1/U1xxx, ...) mean different things per OEM, so they get no text -- the
# tool must not invent one. ``is_generic`` says which is which; OEM meanings belong in a plugin.

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

# A curated set of the most common, well-established generic codes. Kept deliberately small and
# high-confidence rather than a huge (error-prone) table; anything not here still gets a
# subsystem-level description from the code's structure.
GENERIC_DTC = {
    "P0100": "Mass or volume air flow circuit",
    "P0101": "Mass or volume air flow circuit range/performance",
    "P0105": "Manifold absolute pressure/barometric circuit",
    "P0110": "Intake air temperature circuit",
    "P0112": "Intake air temperature circuit low",
    "P0113": "Intake air temperature circuit high",
    "P0115": "Engine coolant temperature circuit",
    "P0117": "Engine coolant temperature circuit low",
    "P0118": "Engine coolant temperature circuit high",
    "P0120": "Throttle/pedal position sensor A circuit",
    "P0121": "Throttle/pedal position sensor A range/performance",
    "P0128": "Coolant thermostat (below regulating temperature)",
    "P0130": "O2 sensor circuit (bank 1 sensor 1)",
    "P0133": "O2 sensor circuit slow response (bank 1 sensor 1)",
    "P0135": "O2 sensor heater circuit (bank 1 sensor 1)",
    "P0171": "System too lean (bank 1)",
    "P0172": "System too rich (bank 1)",
    "P0174": "System too lean (bank 2)",
    "P0175": "System too rich (bank 2)",
    "P0217": "Engine over-temperature condition",
    "P0300": "Random/multiple cylinder misfire detected",
    "P0301": "Cylinder 1 misfire detected",
    "P0302": "Cylinder 2 misfire detected",
    "P0303": "Cylinder 3 misfire detected",
    "P0304": "Cylinder 4 misfire detected",
    "P0401": "Exhaust gas recirculation flow insufficient",
    "P0420": "Catalyst system efficiency below threshold (bank 1)",
    "P0430": "Catalyst system efficiency below threshold (bank 2)",
    "P0440": "Evaporative emission system",
    "P0442": "Evaporative emission system leak (small)",
    "P0446": "Evaporative emission system vent control circuit",
    "P0455": "Evaporative emission system leak (large)",
    "P0500": "Vehicle speed sensor A",
    "P0505": "Idle air control system",
    "P0506": "Idle control system RPM lower than expected",
    "P0507": "Idle control system RPM higher than expected",
    "P0562": "System voltage low",
    "P0563": "System voltage high",
    "P0600": "Serial communication link",
    "P0601": "Internal control module memory checksum error",
    "P0700": "Transmission control system (MIL request)",
    "P0715": "Input/turbine speed sensor circuit",
    "P0720": "Output speed sensor circuit",
    "P0730": "Incorrect gear ratio",
    "P0740": "Torque converter clutch circuit",
    "U0073": "Control module communication bus A off",
    "U0100": "Lost communication with ECM/PCM A",
    "U0101": "Lost communication with TCM",
    "U0121": "Lost communication with ABS control module",
    "U0126": "Lost communication with steering angle sensor module",
    "U0140": "Lost communication with body control module",
    "U0155": "Lost communication with instrument panel cluster",
}


def is_generic(code: str) -> bool:
    """True when ``code`` is an SAE/ISO generic code (same meaning for every maker), as opposed to
    a manufacturer-specific one. Generic: the P0/C0/B0/U0 groups and the jointly-defined P2 and
    U0/U2/U3 network ranges; manufacturer: P1/P3, C1/C2/C3, B1/B2/B3, U1."""
    if len(code) != 5:
        return False
    letter, grp = code[0], code[1]
    if letter == "P":
        return grp in ("0", "2")
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
    """Best available description: the curated generic text if known, else a subsystem-level
    label from the code structure. Manufacturer-specific codes say so (their meaning is OEM)."""
    if code in GENERIC_DTC:
        return GENERIC_DTC[code]
    if not is_generic(code):
        return f"{dtc_subsystem(code)} — manufacturer-specific (see OEM/plugin data)"
    return dtc_subsystem(code)
