"""Mode 06: on-board monitoring test results (SAE J1979, CAN format).

Each emissions monitor (catalyst, O2 sensors, EVAP, misfire ...) stores the numbers behind its
last pass / fail verdict: a test value with the minimum and maximum limits it is judged against.
They show a component that *passes but is close to failing* before it sets a code.

A response is ``0x46`` followed by 9-byte records: OBDMID, TID, UASID, value, min, max (16-bit
each). The UASID picks the unit and scaling from the table below. Monitor IDs and the
unit/scaling table are SAE J1979; both are also in python-OBD (GPL-2.0-or-later), which these
were checked against.
"""

from dataclasses import dataclass

SUPPORT_MIDS = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0)


def _mid_names() -> dict[int, str]:
    names = {}
    for i in range(16):
        names[0x01 + i] = f"O2 sensor B{1 + i // 4}S{1 + i % 4}"
        names[0x41 + i] = f"O2 sensor heater B{1 + i // 4}S{1 + i % 4}"
    for i in range(4):
        b = i + 1
        names.update({0x21 + i: f"Catalyst B{b}", 0x31 + i: f"EGR B{b}", 0x35 + i: f"VVT B{b}",
                      0x61 + i: f"Heated catalyst B{b}", 0x71 + i: f"Secondary air {b}",
                      0x81 + i: f"Fuel system B{b}"})
    names.update({0x39: "EVAP (cap off / 0.150\")", 0x3A: "EVAP (0.090\")", 0x3B: "EVAP (0.040\")",
                  0x3C: "EVAP (0.020\")", 0x3D: "Purge flow", 0x85: "Boost pressure B1",
                  0x86: "Boost pressure B2", 0x90: "NOx absorber B1", 0x91: "NOx absorber B2",
                  0x98: "NOx catalyst B1", 0x99: "NOx catalyst B2", 0xA1: "Misfire (general)",
                  0xB0: "PM filter B1", 0xB1: "PM filter B2"})
    for c in range(12):
        names[0xA2 + c] = f"Misfire cylinder {c + 1}"
    return names


MIDS = _mid_names()

# Standardized test IDs (TIDs 0x01-0x0C); 0x80+ are manufacturer-defined.
TIDS = {0x01: "Rich to lean threshold voltage", 0x02: "Lean to rich threshold voltage",
        0x03: "Low voltage for switch time", 0x04: "High voltage for switch time",
        0x05: "Rich to lean switch time", 0x06: "Lean to rich switch time", 0x07: "Minimum voltage",
        0x08: "Maximum voltage", 0x09: "Time between transitions", 0x0A: "Sensor period",
        0x0B: "Misfire average (last 10 cycles)", 0x0C: "Misfire count (last/current cycle)"}

# UASID -> (signed, scale, unit, offset)
UAS = {
    0x01: (False, 1, "", 0), 0x02: (False, 0.1, "", 0), 0x03: (False, 0.01, "", 0), 0x04: (False, 0.001, "", 0),
    0x05: (False, 0.0000305, "", 0), 0x06: (False, 0.000305, "", 0), 0x07: (False, 0.25, "rpm", 0),
    0x08: (False, 0.01, "km/h", 0), 0x09: (False, 1, "km/h", 0), 0x0A: (False, 0.122, "mV", 0),
    0x0B: (False, 0.001, "V", 0), 0x0C: (False, 0.01, "V", 0), 0x0D: (False, 0.00390625, "mA", 0),
    0x0E: (False, 0.001, "A", 0), 0x0F: (False, 0.01, "A", 0), 0x10: (False, 1, "ms", 0),
    0x11: (False, 100, "ms", 0), 0x12: (False, 1, "s", 0), 0x13: (False, 1, "mΩ", 0), 0x14: (False, 1, "Ω", 0),
    0x15: (False, 1, "kΩ", 0), 0x16: (False, 0.1, "°C", -40), 0x17: (False, 0.01, "kPa", 0),
    0x18: (False, 0.0117, "kPa", 0), 0x19: (False, 0.079, "kPa", 0), 0x1A: (False, 1, "kPa", 0),
    0x1B: (False, 10, "kPa", 0), 0x1C: (False, 0.01, "°", 0), 0x1D: (False, 0.5, "°", 0),
    0x1E: (False, 0.0000305, "ratio", 0), 0x1F: (False, 0.05, "ratio", 0), 0x20: (False, 0.00390625, "ratio", 0),
    0x21: (False, 1, "mHz", 0), 0x22: (False, 1, "Hz", 0), 0x23: (False, 1, "kHz", 0), 0x24: (False, 1, "counts", 0),
    0x25: (False, 1, "km", 0), 0x26: (False, 0.1, "mV/ms", 0), 0x27: (False, 0.01, "g/s", 0),
    0x28: (False, 1, "g/s", 0), 0x29: (False, 0.25, "Pa/s", 0), 0x2A: (False, 0.001, "kg/h", 0),
    0x2B: (False, 1, "switches", 0), 0x2C: (False, 0.01, "g/cyl", 0), 0x2D: (False, 0.01, "mg/stroke", 0),
    0x2F: (False, 0.01, "%", 0), 0x30: (False, 0.001526, "%", 0), 0x31: (False, 0.001, "L", 0),
    0x32: (False, 0.0000305, "in", 0), 0x33: (False, 0.00024414, "ratio", 0), 0x34: (False, 1, "min", 0),
    0x35: (False, 10, "ms", 0), 0x36: (False, 0.01, "g", 0), 0x37: (False, 0.1, "g", 0), 0x38: (False, 1, "g", 0),
    0x39: (False, 0.01, "%", -327.68), 0x3A: (False, 0.001, "g", 0), 0x3B: (False, 0.0001, "g", 0),
    0x3C: (False, 0.1, "µs", 0), 0x3D: (False, 0.01, "mA", 0), 0x3E: (False, 0.00006103516, "mm²", 0),
    0x3F: (False, 0.01, "L", 0), 0x40: (False, 1, "ppm", 0), 0x41: (False, 0.01, "µA", 0),
    0x81: (True, 1, "", 0), 0x82: (True, 0.1, "", 0), 0x83: (True, 0.01, "", 0), 0x84: (True, 0.001, "", 0),
    0x85: (True, 0.0000305, "", 0), 0x86: (True, 0.000305, "", 0), 0x87: (True, 1, "ppm", 0),
    0x8A: (True, 0.122, "mV", 0), 0x8B: (True, 0.001, "V", 0), 0x8C: (True, 0.01, "V", 0),
    0x8D: (True, 0.00390625, "mA", 0), 0x8E: (True, 0.001, "A", 0), 0x90: (True, 1, "ms", 0),
    0x96: (True, 0.1, "°C", 0), 0x99: (True, 0.1, "kPa", 0), 0x9C: (True, 0.01, "°", 0), 0x9D: (True, 0.5, "°", 0),
    0xA8: (True, 1, "g/s", 0), 0xA9: (True, 0.25, "Pa/s", 0), 0xAD: (True, 0.01, "mg/stroke", 0),
    0xAE: (True, 0.1, "mg/stroke", 0), 0xAF: (True, 0.01, "%", 0), 0xB0: (True, 0.003052, "%", 0),
    0xB1: (True, 2, "mV/s", 0), 0xFC: (True, 0.01, "kPa", 0), 0xFD: (True, 0.001, "kPa", 0),
    0xFE: (True, 0.25, "Pa", 0),
}


def scale(uas: int, raw: int) -> tuple[float, str]:
    """A raw 16-bit test value in its unit. Unknown UASIDs come back raw, unitless."""
    signed, factor, unit, offset = UAS.get(uas, (False, 1, "", 0))
    if signed and raw & 0x8000:
        raw -= 0x10000
    return round(raw * factor + offset, 6), unit


@dataclass(frozen=True)
class TestResult:
    __test__ = False        # not a pytest test class, despite the name

    mid: int
    tid: int
    uas: int
    value: float
    minimum: float
    maximum: float
    unit: str

    @property
    def monitor(self) -> str:
        return MIDS.get(self.mid, f"Monitor 0x{self.mid:02X}")

    @property
    def test(self) -> str:
        return TIDS.get(self.tid, f"Test 0x{self.tid:02X}" + (" (manufacturer)" if self.tid >= 0x80 else ""))

    @property
    def passed(self) -> bool:
        return self.minimum <= self.value <= self.maximum


def decode_results(payload: bytes) -> list[TestResult]:
    """Records from a Mode 06 response body (the bytes after 0x46)."""
    out = []
    for i in range(0, len(payload) - 8, 9):
        mid, tid, uas = payload[i], payload[i + 1], payload[i + 2]
        raw = [int.from_bytes(payload[i + 3 + 2 * k:i + 5 + 2 * k], "big") for k in range(3)]
        (value, unit), (lo, _), (hi, _) = (scale(uas, r) for r in raw)
        out.append(TestResult(mid, tid, uas, value, lo, hi, unit))
    return out
