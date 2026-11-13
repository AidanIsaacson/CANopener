"""Live-data signal definitions for common broadcast PGNs (J1939-71).

``start`` is the 0-based byte index (J1939 documents count from 1). Most standard parameters
are whole bytes (``length`` = 1, 2 or 4); machine profiles can also describe bit fields
(``bit`` / ``bits``), signed values, enumerated states, a normal range, and text: ``kind`` =
``"text"`` decodes ``length`` bytes (1-8) as ASCII, the way controllers send display messages
("LOW BTRY") or fault strings. A text value is a ``str``; it has no unit, scale, range or graph.
"""

from dataclasses import dataclass

from . import fields


@dataclass(frozen=True, slots=True)
class Signal:
    name: str
    pgn: int
    start: int
    length: int         # bytes: 1, 2 or 4 (ignored when ``bits`` is set)
    scale: float
    offset: float
    unit: str
    spn: int | None = None
    decimals: int = 1
    sa: int | tuple[int, ...] | None = None   # only decode from this / these source addresses
    na_check: bool = True       # treat 0xFB-0xFF.. / all-ones as 'not available' (standard J1939 data)
    bit: int = 0                # first bit within ``start`` (0 = LSB), for bit fields
    bits: int = 0               # field width in bits; 0 = whole bytes (``length``)
    signed: bool = False
    enum: tuple[tuple[int, str], ...] = ()     # raw value -> text, e.g. ((0, "Off"), (1, "On"))
    group: str = ""
    low: float | None = None    # normal range, for highlighting out-of-range values
    high: float | None = None
    can_id: int | None = None   # an 11-bit CAN ID (CANopen PDO, plain CAN) instead of a PGN
    big_endian: bool = False    # whole-byte values only: high byte first (Motorola order)
    kind: str = ""              # "" = a number; "text" = ``length`` bytes of ASCII

    @property
    def is_text(self) -> bool:
        return self.kind == TEXT

    @property
    def key(self) -> str:
        if self.can_id is not None:
            return f"ID{self.can_id:03X}:{self.start}.{self.bit}:{self.name}"
        return f"{self.pgn:05X}:{self.start}.{self.bit}:{self.name}"

    @property
    def ref(self) -> str:
        """Where it comes from, for display: 'FF10' (a PGN) or 'ID 181' (an 11-bit ID)."""
        return f"ID {self.can_id:03X}" if self.can_id is not None else f"{self.pgn:04X}"

    @property
    def width_bits(self) -> int:
        return self.bits or self.length * 8

    @property
    def location(self) -> tuple:
        msg = self.pgn if self.can_id is None else ("id", self.can_id)
        return (msg, self.start, self.bit, self.width_bits)

    def accepts(self, sa: int) -> bool:
        if self.sa is None:
            return True
        return sa in self.sa if isinstance(self.sa, tuple) else sa == self.sa

    def decode(self, data) -> float | str | None:
        if self.kind == TEXT:
            raw = bytes(data[self.start:self.start + self.length])
            if self.na_check and raw and all(b == 0xFF for b in raw):
                return None                     # J1939 "not available", not a message
            return decode_text(data, self.start, self.length)
        if self.big_endian and not self.bits:
            if self.start + self.length > len(data):
                return None
            data = bytes(reversed(bytes(data[self.start:self.start + self.length])))
            return Signal.decode(_replace_at0(self), data)
        if not self.bits and not self.bit and not self.signed:
            return fields.read(data, self.start, self.length, self.scale, self.offset, self.na_check)
        return fields.read_bits(data, self.start, self.bit, self.width_bits, self.scale, self.offset,
                                self.signed, self.na_check)

    def text(self, value) -> str | None:
        """Enum text for a decoded value, the value itself for a text signal, or None for plain
        numbers."""
        if isinstance(value, str):
            return value
        if value is None or not self.enum:
            return None
        raw = round((value - self.offset) / self.scale) if self.scale else int(value)
        return dict(self.enum).get(raw)

    def out_of_range(self, value) -> bool:
        return value is not None and not isinstance(value, str) and ((self.low is not None and value < self.low)
                                      or (self.high is not None and value > self.high))

    def encode_into(self, buf: bytearray, value) -> None:
        if self.kind == TEXT:
            raw = str(value).encode("ascii", "replace")[:self.length].ljust(self.length, b" ")
            buf[self.start:self.start + self.length] = raw
            return
        if self.big_endian and not self.bits:
            tmp = bytearray(self.length)
            Signal.encode_into(_replace_at0(self), tmp, value)
            buf[self.start:self.start + self.length] = bytes(reversed(tmp))
            return
        if not self.bits and not self.bit and not self.signed:
            buf[self.start:self.start + self.length] = fields.encode(value, self.length, self.scale, self.offset)
        else:
            fields.encode_bits(buf, self.start, self.bit, self.width_bits, value, self.scale, self.offset,
                               self.signed)


TEXT = "text"


def decode_text(data, start: int, length: int) -> str | None:
    """``length`` bytes from ``start`` as text: printable ASCII kept, anything else shown as '.',
    trailing spaces / NULs dropped. All blank (or not there) = None: displays that blink a message
    send blank frames in between, and those shouldn't wipe the last message."""
    raw = bytes(data[start:start + length])
    if len(raw) < length:
        return None
    text = "".join(chr(b) if 32 <= b < 127 else ("" if b == 0 else ".") for b in raw).rstrip()
    return text or None


def _replace_at0(sig: Signal) -> Signal:
    """The same field at byte 0, in little-endian order (for big-endian decode / encode)."""
    from dataclasses import replace
    return replace(sig, start=0, big_endian=False)


S = Signal
_OFF_ON = ((0, "Off"), (1, "On"), (2, "Error"))             # the standard 2-bit switch / status
_NO_YES = ((0, "No"), (1, "Yes"), (2, "Error"))


def _sw(name, pgn, start, bit, spn, enum=_OFF_ON):
    """A standard 2-bit status field (3 = not available)."""
    return Signal(name, pgn, start, 1, 1, 0, "", spn, 0, bit=bit, bits=2, enum=enum)


def _en(name, pgn, start, bit, bits, spn, enum):
    return Signal(name, pgn, start, 1, 1, 0, "", spn, 0, bit=bit, bits=bits, enum=enum)


_TEMP16 = (0.03125, -273, "°C")        # the standard 16-bit temperature
_KPA128 = (1 / 128, -250, "kPa")       # the standard signed 16-bit pressure
_TORQUE_MODE = ((0, "Low idle / no request"), (1, "Accelerator pedal"), (2, "Cruise control"), (3, "PTO governor"),
                (4, "Road speed governor"), (5, "ASR control"), (6, "Transmission control"), (7, "ABS control"),
                (8, "Torque limiting"), (9, "High speed governor"), (10, "Braking system"), (11, "Remote accelerator"))

# Standard J1939-71 parameters, in our own words. Layouts are the published standard ones; the
# ones a machine actually sends vary, so anything that looks wrong on a real bus is worth checking.
SIGNALS: list[Signal] = [
    # EEC1 (61444) - engine controller 1
    _en("Engine torque mode", 0xF004, 0, 0, 4, 899, _TORQUE_MODE),
    S("Driver's demand torque", 0xF004, 1, 1, 1, -125, "%", 512, 0),
    S("Actual engine torque", 0xF004, 2, 1, 1, -125, "%", 513, 0),
    S("Engine speed", 0xF004, 3, 2, 0.125, 0, "rpm", 190, 0),
    S("Engine demand torque", 0xF004, 7, 1, 1, -125, "%", 2432, 0),
    # EEC2 (61443) - engine controller 2
    _sw("Accelerator low-idle switch", 0xF003, 0, 0, 558),
    _sw("Accelerator kickdown switch", 0xF003, 0, 2, 559),
    S("Accelerator pedal 1", 0xF003, 1, 1, 0.4, 0, "%", 91),
    S("Load at current speed", 0xF003, 2, 1, 1, 0, "%", 92, 0),
    S("Remote accelerator pedal", 0xF003, 3, 1, 0.4, 0, "%", 974),
    S("Accelerator pedal 2", 0xF003, 4, 1, 0.4, 0, "%", 29),
    S("Maximum available torque", 0xF003, 6, 1, 0.4, 0, "%", 3357),
    # EEC3 (65247) - engine controller 3
    S("Nominal friction torque", 0xFEDF, 0, 1, 1, -125, "%", 514, 0),
    S("Desired operating speed", 0xFEDF, 1, 2, 0.125, 0, "rpm", 515, 0),
    S("Estimated parasitic losses", 0xFEDF, 4, 1, 1, -125, "%", 2978, 0),
    S("Exhaust gas mass flow", 0xFEDF, 5, 2, 0.2, 0, "kg/h", 3236),
    # EGF1 (61450) - engine gas flow
    S("EGR mass flow", 0xF00A, 0, 2, 0.05, 0, "kg/h", 2659),
    S("Intake air mass flow", 0xF00A, 2, 2, 0.05, 0, "kg/h", 132),
    # HOURS (65253), IO (65244) - hours and revolutions
    S("Engine hours", 0xFEE5, 0, 4, 0.05, 0, "h", 247, 1),
    S("Total engine revolutions", 0xFEE5, 4, 4, 1000, 0, "rev", 249, 0),
    S("Total idle fuel used", 0xFEDC, 0, 4, 0.5, 0, "L", 236),
    S("Total idle hours", 0xFEDC, 4, 4, 0.05, 0, "h", 235, 1),
    # EOI (64914), SHUTDN (65252), AS (65237), EI (65170)
    _en("Engine operating state", 0xFD92, 0, 0, 4, 3543,
        ((0, "Stopped"), (1, "Pre-start"), (2, "Starting"), (3, "Warm-up"), (4, "Running"), (5, "Cool-down"),
         (6, "Stopping"), (7, "Post-run"))),
    _sw("Wait to start lamp", 0xFEE4, 3, 0, 1081),
    S("Alternator speed", 0xFED5, 0, 2, 0.5, 0, "rpm", 589, 0),
    S("Oil pressure before filter", 0xFE92, 0, 1, 4, 0, "kPa", 1208, 0),
    S("Exhaust gas pressure", 0xFE92, 1, 2, *_KPA128, 1209),
    # ET1 (65262), ET2 (65188), ET3 (65129) - temperatures
    S("Coolant temp", 0xFEEE, 0, 1, 1, -40, "°C", 110, 0),
    S("Fuel temp", 0xFEEE, 1, 1, 1, -40, "°C", 174, 0),
    S("Oil temp", 0xFEEE, 2, 2, *_TEMP16, 175),
    S("Turbocharger oil temp", 0xFEEE, 4, 2, *_TEMP16, 176),
    S("Intercooler temp", 0xFEEE, 6, 1, 1, -40, "°C", 52, 0),
    S("Intercooler thermostat opening", 0xFEEE, 7, 1, 0.4, 0, "%", 1134),
    S("Engine ECU temp", 0xFE84, 2, 2, *_TEMP16, 1136),
    S("EGR differential pressure", 0xFE84, 4, 2, *_KPA128, 411),
    S("EGR temp", 0xFE84, 6, 2, *_TEMP16, 412),
    S("Coolant temp (high resolution)", 0xFE69, 0, 2, *_TEMP16, 1637),
    S("Intake manifold temp (high resolution)", 0xFE69, 2, 2, *_TEMP16, 1636),
    S("Hydraulic oil temp", 0xFE68, 0, 1, 1, -40, "°C", 1638, 0),
    # EFL/P1 (65263), EFL/P2 (65243) - fluid levels and pressures
    S("Fuel delivery pressure", 0xFEEF, 0, 1, 4, 0, "kPa", 94, 0),
    S("Crankcase blow-by pressure", 0xFEEF, 1, 1, 0.05, 0, "kPa", 22, 2),
    S("Oil level", 0xFEEF, 2, 1, 0.4, 0, "%", 98),
    S("Oil pressure", 0xFEEF, 3, 1, 4, 0, "kPa", 100, 0),
    S("Crankcase pressure", 0xFEEF, 4, 2, *_KPA128, 101),
    S("Coolant pressure", 0xFEEF, 6, 1, 2, 0, "kPa", 109, 0),
    S("Coolant level", 0xFEEF, 7, 1, 0.4, 0, "%", 111),
    S("Injection control pressure", 0xFEDB, 0, 2, 1 / 256, 0, "MPa", 164, 2),
    S("Fuel rail pressure", 0xFEDB, 2, 2, 1 / 256, 0, "MPa", 157, 2),
    S("Fuel rail 2 pressure", 0xFEDB, 6, 2, 1 / 256, 0, "MPa", 1349, 2),
    # IC1 (65270), TC (65245) - intake, exhaust, turbo
    S("DPF inlet pressure", 0xFEF6, 0, 1, 0.5, 0, "kPa", 81),
    S("Boost pressure", 0xFEF6, 1, 1, 2, 0, "kPa", 102, 0),
    S("Intake manifold temp", 0xFEF6, 2, 1, 1, -40, "°C", 105, 0),
    S("Air inlet pressure", 0xFEF6, 3, 1, 2, 0, "kPa", 106, 0),
    S("Air filter diff. pressure", 0xFEF6, 4, 1, 0.05, 0, "kPa", 107, 2),
    S("Exhaust gas temp", 0xFEF6, 5, 2, *_TEMP16, 173, 0),
    S("Coolant filter diff. pressure", 0xFEF6, 7, 1, 0.5, 0, "kPa", 112),
    S("Turbocharger oil pressure", 0xFEDD, 0, 1, 4, 0, "kPa", 104, 0),
    S("Turbocharger speed", 0xFEDD, 1, 2, 4, 0, "rpm", 103, 0),
    # FD (65213) - fan drive
    S("Fan speed (estimated)", 0xFEBD, 0, 1, 0.4, 0, "%", 975),
    _en("Fan drive state", 0xFEBD, 1, 0, 4, 977,
        ((0, "Off"), (1, "Engine system"), (2, "Excessive engine air temp"), (3, "Excessive engine oil temp"),
         (4, "Excessive coolant temp"), (5, "Excessive transmission oil temp"),
         (6, "Excessive hydraulic oil temp"), (7, "Default operation"), (8, "Not defined"), (9, "Manual control"),
         (10, "Transmission retarder"), (11, "A/C system"), (12, "Timer"), (13, "Engine brake"), (14, "Other"))),
    S("Fan speed", 0xFEBD, 2, 2, 0.125, 0, "rpm", 1639, 0),
    # VEP1 (65271) - electrical
    S("Battery current", 0xFEF7, 0, 1, 1, -125, "A", 114, 0),
    S("Alternator current", 0xFEF7, 1, 1, 1, 0, "A", 115, 0),
    S("Charging system voltage", 0xFEF7, 2, 2, 0.05, 0, "V", 167, 2),
    S("Battery voltage", 0xFEF7, 4, 2, 0.05, 0, "V", 168, 2),
    S("Keyswitch voltage", 0xFEF7, 6, 2, 0.05, 0, "V", 158, 2),
    # AMB (65269) - ambient
    S("Barometric pressure", 0xFEF5, 0, 1, 0.5, 0, "kPa", 108),
    S("Cab interior temp", 0xFEF5, 1, 2, *_TEMP16, 170),
    S("Ambient air temp", 0xFEF5, 3, 2, *_TEMP16, 171),
    S("Air inlet temp", 0xFEF5, 5, 1, 1, -40, "°C", 172, 0),
    S("Road surface temp", 0xFEF5, 6, 2, *_TEMP16, 79),
    # LFE (65266), LFC (65257), DD (65276), WFI (65279) - fuel
    S("Fuel rate", 0xFEF2, 0, 2, 0.05, 0, "L/h", 183, 2),
    S("Fuel economy (instant)", 0xFEF2, 2, 2, 1 / 512, 0, "km/L", 184, 2),
    S("Fuel economy (average)", 0xFEF2, 4, 2, 1 / 512, 0, "km/L", 185, 2),
    S("Throttle position", 0xFEF2, 6, 1, 0.4, 0, "%", 51),
    S("Trip fuel", 0xFEE9, 0, 4, 0.5, 0, "L", 182),
    S("Total fuel used", 0xFEE9, 4, 4, 0.5, 0, "L", 250),
    S("Washer fluid level", 0xFEFC, 0, 1, 0.4, 0, "%", 80),
    S("Fuel level", 0xFEFC, 1, 1, 0.4, 0, "%", 96),
    S("Fuel filter diff. pressure", 0xFEFC, 2, 1, 2, 0, "kPa", 95, 0),
    S("Oil filter diff. pressure", 0xFEFC, 3, 1, 0.5, 0, "kPa", 99),
    S("Fuel level 2", 0xFEFC, 6, 1, 0.4, 0, "%", 38),
    _sw("Water in fuel", 0xFEFF, 0, 0, 97, _NO_YES),
    # CCVS (65265), VD (65248), VDHR (65217), TCO1 (65132), EBC2 (65215) - vehicle speed and distance
    _sw("Parking brake switch", 0xFEF1, 0, 2, 70),
    S("Vehicle speed", 0xFEF1, 1, 2, 1 / 256, 0, "km/h", 84),
    _sw("Cruise control active", 0xFEF1, 3, 0, 595),
    _sw("Cruise control enable switch", 0xFEF1, 3, 2, 596),
    _sw("Brake switch", 0xFEF1, 3, 4, 597, ((0, "Released"), (1, "Pressed"), (2, "Error"))),
    _sw("Clutch switch", 0xFEF1, 3, 6, 598, ((0, "Released"), (1, "Pressed"), (2, "Error"))),
    S("Cruise control set speed", 0xFEF1, 5, 1, 1, 0, "km/h", 86, 0),
    _en("PTO governor state", 0xFEF1, 6, 0, 5, 976,
        ((0, "Off"), (1, "Hold"), (2, "Remote hold"), (3, "Standby"), (4, "Remote standby"), (5, "Set"),
         (6, "Decelerate / coast"), (7, "Resume"), (8, "Accelerate"), (9, "Accelerator override"),
         (10, "Preset speed 1"), (11, "Preset speed 2"), (12, "Preset speed 3"), (13, "Preset speed 4"),
         (14, "Preset speed 5"), (15, "Preset speed 6"), (16, "Preset speed 7"), (17, "Preset speed 8"))),
    S("Trip distance", 0xFEE0, 0, 4, 0.125, 0, "km", 244),
    S("Total vehicle distance", 0xFEE0, 4, 4, 0.125, 0, "km", 245),
    S("Total vehicle distance (high resolution)", 0xFEC1, 0, 4, 0.005, 0, "km", 917, 2),
    S("Trip distance (high resolution)", 0xFEC1, 4, 4, 0.005, 0, "km", 918, 2),
    S("Tachograph vehicle speed", 0xFE6C, 6, 2, 1 / 256, 0, "km/h", 1624),
    S("Front axle speed", 0xFEBF, 0, 2, 1 / 256, 0, "km/h", 904),
    # ETC1 (61442), ETC2 (61445), TRF1 (65272) - transmission
    _sw("Driveline engaged", 0xF002, 0, 0, 560, _NO_YES),
    _sw("Torque converter lockup", 0xF002, 0, 2, 573, ((0, "Released"), (1, "Engaged"), (2, "Error"))),
    _sw("Shift in process", 0xF002, 0, 4, 574, _NO_YES),
    S("Transmission output shaft speed", 0xF002, 1, 2, 0.125, 0, "rpm", 191, 0),
    S("Clutch slip", 0xF002, 3, 1, 0.4, 0, "%", 522),
    S("Transmission input shaft speed", 0xF002, 5, 2, 0.125, 0, "rpm", 161, 0),
    S("Transmission selected gear", 0xF005, 0, 1, 1, -125, "", 524, 0),
    S("Transmission actual gear ratio", 0xF005, 1, 2, 0.001, 0, "", 526, 3),
    S("Transmission current gear", 0xF005, 3, 1, 1, -125, "", 523, 0),
    S("Transmission requested range", 0xF005, 4, 2, 1, 0, "", 162, kind=TEXT),
    S("Transmission current range", 0xF005, 6, 2, 1, 0, "", 163, kind=TEXT),
    S("Clutch pressure", 0xFEF8, 0, 1, 16, 0, "kPa", 123, 0),
    S("Transmission oil level", 0xFEF8, 1, 1, 0.4, 0, "%", 124),
    S("Transmission filter diff. pressure", 0xFEF8, 2, 1, 2, 0, "kPa", 126, 0),
    S("Transmission oil pressure", 0xFEF8, 3, 1, 16, 0, "kPa", 127, 0),
    S("Transmission oil temp", 0xFEF8, 4, 2, *_TEMP16, 177),
    # ERC1 (61440), EBC1 (61441) - retarder and brakes
    S("Retarder torque", 0xF000, 1, 1, 1, -125, "%", 520, 0),
    _sw("ASR engine control active", 0xF001, 0, 0, 561),
    _sw("ASR brake control active", 0xF001, 0, 2, 562),
    _sw("ABS active", 0xF001, 0, 4, 563),
    S("Brake pedal position", 0xF001, 1, 1, 0.4, 0, "%", 521),
    _sw("ABS fully operational", 0xF001, 5, 0, 1243, _NO_YES),
    _sw("EBS red warning", 0xF001, 5, 2, 1439),
    _sw("ABS / EBS amber warning", 0xF001, 5, 4, 1438),
    # PTO (65264), HPG (61448) - power take-off and hydraulics
    S("PTO oil temp", 0xFEF0, 0, 1, 1, -40, "°C", 90, 0),
    S("PTO speed", 0xFEF0, 1, 2, 0.125, 0, "rpm", 186, 0),
    S("PTO set speed", 0xFEF0, 3, 2, 0.125, 0, "rpm", 187, 0),
    S("Hydraulic pressure", 0xF008, 0, 2, 2, 0, "kPa", 1762, 0),
    # Aftertreatment: AT1T1I (65110), AT1S (64891), DPFC1 (64892), NOx (61454 / 61455), gas temps
    S("DEF tank level", 0xFE56, 0, 1, 0.4, 0, "%", 1761),
    S("DEF tank temp", 0xFE56, 1, 1, 1, -40, "°C", 3031, 0),
    S("DPF soot load", 0xFD7B, 0, 1, 1, 0, "%", 3719, 0),
    S("DPF ash load", 0xFD7B, 1, 1, 1, 0, "%", 3720, 0),
    S("Time since last DPF regeneration", 0xFD7B, 2, 4, 1 / 3600, 0, "h", 3721, 1),
    _en("DPF lamp", 0xFD7C, 0, 0, 3, 3697, ((0, "Off"), (1, "On"), (4, "Flashing"))),
    _sw("DPF passive regeneration", 0xFD7C, 1, 0, 3699, ((0, "Not active"), (1, "Active"), (2, "Needed"))),
    _sw("DPF active regeneration", 0xFD7C, 1, 2, 3700, ((0, "Not active"), (1, "Active"), (2, "Needed"))),
    _en("DPF status", 0xFD7C, 1, 4, 3, 3701,
        ((0, "Regeneration not needed"), (1, "Regeneration needed: lowest level"),
         (2, "Regeneration needed: moderate level"), (3, "Regeneration needed: highest level"))),
    _sw("DPF regeneration inhibited", 0xFD7C, 2, 0, 3702, _NO_YES),
    _en("High exhaust temperature lamp", 0xFD7C, 6, 2, 3, 3698, ((0, "Off"), (1, "On"))),
    S("NOx before SCR", 0xF00E, 0, 2, 0.05, -200, "ppm", 3216, 0),
    S("NOx after SCR", 0xF00F, 0, 2, 0.05, -200, "ppm", 3226, 0),
    S("Exhaust temp 1 (before DOC)", 0xFDB4, 0, 2, *_TEMP16, 3241, 0),
    S("DPF inlet temp", 0xFDB4, 2, 2, *_TEMP16, 3242, 0),
    S("Exhaust temp 2 (DPF middle)", 0xFDB2, 0, 2, *_TEMP16, 3249, 0),
    S("DPF differential pressure", 0xFDB2, 4, 2, 0.1, 0, "kPa", 3251),
    S("Exhaust temp 3 (after DPF)", 0xFDB3, 0, 2, *_TEMP16, 3245, 0),
    S("DPF outlet temp", 0xFDB3, 2, 2, *_TEMP16, 3246, 0),
    # VP (65267), VDS (65256) - position
    S("Latitude", 0xFEF3, 0, 4, 1e-7, -210, "°", 584, 5),
    S("Longitude", 0xFEF3, 4, 4, 1e-7, -210, "°", 585, 5),
    S("Compass bearing", 0xFEE8, 0, 2, 1 / 128, 0, "°", 165),
    S("Navigation speed", 0xFEE8, 2, 2, 1 / 256, 0, "km/h", 517),
    S("Altitude", 0xFEE8, 6, 2, 0.125, -2500, "m", 580, 0),
]
del S, _sw, _en

_GROUPS = {
    0xF004: "Engine", 0xF003: "Engine", 0xFEE5: "Engine", 0xFEDF: "Engine", 0xF00A: "Engine", 0xFEDC: "Engine",
    0xFD92: "Engine", 0xFEE4: "Engine", 0xFE92: "Engine",
    0xFEEE: "Temperatures", 0xFE68: "Temperatures", 0xFE84: "Temperatures", 0xFE69: "Temperatures",
    0xFEEF: "Fluids", 0xFEDB: "Fuel", 0xFEF2: "Fuel", 0xFEFC: "Fuel", 0xFEE9: "Fuel", 0xFEFF: "Fuel",
    0xFEF6: "Intake / exhaust", 0xFEF5: "Intake / exhaust", 0xFEDD: "Intake / exhaust", 0xFEBD: "Cooling fan",
    0xFEF7: "Electrical", 0xFED5: "Electrical",
    0xFEF1: "Drivetrain", 0xF005: "Drivetrain", 0xF002: "Drivetrain", 0xFEF8: "Drivetrain", 0xFEE0: "Drivetrain",
    0xFEC1: "Drivetrain", 0xFE6C: "Drivetrain", 0xFEBF: "Drivetrain", 0xFEF0: "Drivetrain",
    0xF001: "Brakes", 0xF000: "Brakes", 0xF008: "Hydraulics",
    0xFE56: "Aftertreatment", 0xFD7B: "Aftertreatment", 0xFD7C: "Aftertreatment", 0xF00E: "Aftertreatment",
    0xF00F: "Aftertreatment", 0xFDB4: "Aftertreatment", 0xFDB2: "Aftertreatment", 0xFDB3: "Aftertreatment",
    0xFEF3: "Position", 0xFEE8: "Position",
}
SIGNALS = [Signal(**{**{f: getattr(s, f) for f in s.__slots__}, "group": _GROUPS.get(s.pgn, "Other")})
           for s in SIGNALS]

SIGNALS_BY_PGN: dict[int, list[Signal]] = {}
for _s in SIGNALS:
    SIGNALS_BY_PGN.setdefault(_s.pgn, []).append(_s)

# Extra signals from the active machine profile, e.g. labelled proprietary frames.
# Use set_profile_signals() to change them.
PROFILE_SIGNALS: list[Signal] = []
_all_cache: list[Signal] | None = None


def set_profile_signals(sigs: list[Signal]):
    global _all_cache, _by_id_cache
    PROFILE_SIGNALS[:] = sigs
    _all_cache = None
    _by_id_cache = None


_by_id_cache: dict[int, list[Signal]] | None = None


def id_signals() -> dict[int, list[Signal]]:
    """Signals on 11-bit CAN IDs (all from profiles), by ID."""
    global _by_id_cache
    if _by_id_cache is None:
        out: dict[int, list[Signal]] = {}
        for s in all_signals():
            if s.can_id is not None:
                out.setdefault(s.can_id, []).append(s)
        _by_id_cache = out
    return _by_id_cache


def all_signals() -> list[Signal]:
    """Built-in signals plus the profile's. A profile signal at the same place (PGN, byte, bit,
    width) as a built-in one replaces it, so a profile can rename / regroup standard data."""
    global _all_cache
    if _all_cache is None:
        taken = {s.location for s in PROFILE_SIGNALS}
        _all_cache = [s for s in SIGNALS if s.location not in taken] + list(PROFILE_SIGNALS)
    return _all_cache
