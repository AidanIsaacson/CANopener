"""Which live PIDs go with a trouble code: the sensor a generic code is about, so a front-end can
jump from the code to its live value (like a J1939 code's SPN links to its live parameter).

By SAE J2012 code range -> SAE J1979 PIDs; generic codes only. A code can list several PIDs (the
O2 sensor's voltage and the fuel trims it drives); front-ends show whichever the vehicle supports.
"""

# (first code, last code, PIDs), inclusive, P-codes as 16-bit numbers (P0105 -> 0x0105)
_LINKS = [
    (0x0068, 0x0068, (0x0B, 0x10, 0x11)),             # MAP / MAF - throttle correlation
    (0x0070, 0x0074, (0x46,)),                        # ambient air temp sensor
    (0x0100, 0x0104, (0x10,)),                        # MAF
    (0x0105, 0x0109, (0x0B, 0x33)),                   # MAP / barometric
    (0x0110, 0x0114, (0x0F,)),                        # intake air temp
    (0x0115, 0x0119, (0x05,)),                        # coolant temp
    (0x0120, 0x0124, (0x11, 0x45)),                   # throttle position A
    (0x0125, 0x0125, (0x05, 0x03)),                   # coolant too low for closed loop
    (0x0128, 0x0128, (0x05,)),                        # thermostat
    (0x0130, 0x0135, (0x14, 0x06)),                   # O2 B1S1 (+ the trim it drives)
    (0x0136, 0x0141, (0x15,)),                        # O2 B1S2
    (0x0142, 0x0147, (0x16,)),                        # O2 B1S3
    (0x0150, 0x0155, (0x18, 0x08)),                   # O2 B2S1
    (0x0156, 0x0161, (0x19,)),                        # O2 B2S2
    (0x0162, 0x0167, (0x1A,)),                        # O2 B2S3
    (0x0170, 0x0172, (0x06, 0x07, 0x10, 0x14)),       # fuel trim bank 1 (lean / rich)
    (0x0173, 0x0175, (0x08, 0x09, 0x10, 0x18)),       # fuel trim bank 2
    (0x0190, 0x0194, (0x23, 0x22, 0x59)),             # fuel rail pressure sensor
    (0x0196, 0x0198, (0x5C,)),                        # engine oil temp
    (0x0217, 0x0217, (0x05,)),                        # engine over temperature
    (0x0220, 0x0229, (0x47, 0x48)),                   # throttle / pedal position B, C
    (0x0335, 0x0339, (0x0C,)),                        # crankshaft position sensor
    (0x0400, 0x0409, (0x2C, 0x2D)),                   # EGR
    (0x0420, 0x0429, (0x14, 0x15, 0x3C)),             # catalyst bank 1: up / downstream O2, cat temp
    (0x0430, 0x0439, (0x18, 0x19, 0x3D)),             # catalyst bank 2
    (0x0440, 0x0457, (0x2E, 0x32, 0x2F)),             # EVAP: purge, vapour pressure, fuel level
    (0x0460, 0x0464, (0x2F,)),                        # fuel level sensor
    (0x0500, 0x0503, (0x0D,)),                        # vehicle speed sensor
    (0x0505, 0x0507, (0x0C,)),                        # idle control (rpm)
    (0x0560, 0x0563, (0x42,)),                        # system voltage
    (0x2096, 0x2097, (0x15, 0x06, 0x07)),             # post-catalyst trim bank 1
    (0x2098, 0x2099, (0x19, 0x08, 0x09)),             # post-catalyst trim bank 2
    (0x2135, 0x2138, (0x11, 0x47, 0x49, 0x4A)),       # throttle / pedal position correlation
    (0x2195, 0x2196, (0x14, 0x24)),                   # O2 B1S1 stuck lean / rich
    (0x2197, 0x2198, (0x18, 0x28)),                   # O2 B2S1 stuck lean / rich
    (0x2270, 0x2271, (0x15,)),                        # O2 B1S2 stuck
    (0x2272, 0x2273, (0x19,)),                        # O2 B2S2 stuck
]


def pids_for_dtc(code: str) -> list[int]:
    """Live PIDs that show what a code is about, most relevant first ([] if none)."""
    if len(code) != 5 or code[0] != "P" or code[1] not in "02":
        return []
    try:
        n = int(code[1:], 16)
    except ValueError:
        return []
    return next((list(pids) for lo, hi, pids in _LINKS if lo <= n <= hi), [])
