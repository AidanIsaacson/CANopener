"""Live-data signal definitions for common broadcast PGNs (J1939-71).

``start`` is the 0-based byte index (J1939 documents count from 1). Most standard parameters
are whole bytes (``length`` = 1, 2 or 4); machine profiles can also describe bit fields
(``bit`` / ``bits``), signed values, enumerated states and a normal range.
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

    def decode(self, data) -> float | None:
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
        """Enum text for a decoded value, or None for plain numbers."""
        if value is None or not self.enum:
            return None
        raw = round((value - self.offset) / self.scale) if self.scale else int(value)
        return dict(self.enum).get(raw)

    def out_of_range(self, value) -> bool:
        return value is not None and ((self.low is not None and value < self.low)
                                      or (self.high is not None and value > self.high))

    def encode_into(self, buf: bytearray, value) -> None:
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


def _replace_at0(sig: Signal) -> Signal:
    """The same field at byte 0, in little-endian order (for big-endian decode / encode)."""
    from dataclasses import replace
    return replace(sig, start=0, big_endian=False)


S = Signal
SIGNALS: list[Signal] = [
    # EEC1 / EEC2 - engine controllers
    S("Engine speed", 0xF004, 3, 2, 0.125, 0, "rpm", 190, 0),
    S("Actual engine torque", 0xF004, 2, 1, 1, -125, "%", 513, 0),
    S("Accelerator pedal 1", 0xF003, 1, 1, 0.4, 0, "%", 91),
    S("Load at current speed", 0xF003, 2, 1, 1, 0, "%", 92, 0),
    # ET1 - temperatures
    S("Coolant temp", 0xFEEE, 0, 1, 1, -40, "°C", 110, 0),
    S("Fuel temp", 0xFEEE, 1, 1, 1, -40, "°C", 174, 0),
    S("Oil temp", 0xFEEE, 2, 2, 0.03125, -273, "°C", 175),
    # EFL/P1 - fluids
    S("Fuel delivery pressure", 0xFEEF, 0, 1, 4, 0, "kPa", 94, 0),
    S("Oil level", 0xFEEF, 2, 1, 0.4, 0, "%", 98),
    S("Oil pressure", 0xFEEF, 3, 1, 4, 0, "kPa", 100, 0),
    S("Coolant pressure", 0xFEEF, 6, 1, 2, 0, "kPa", 109, 0),
    S("Coolant level", 0xFEEF, 7, 1, 0.4, 0, "%", 111),
    # IC1 - intake / exhaust
    S("DPF inlet pressure", 0xFEF6, 0, 1, 0.5, 0, "kPa", 81),
    S("Boost pressure", 0xFEF6, 1, 1, 2, 0, "kPa", 102, 0),
    S("Intake manifold temp", 0xFEF6, 2, 1, 1, -40, "°C", 105, 0),
    S("Air inlet pressure", 0xFEF6, 3, 1, 2, 0, "kPa", 106, 0),
    S("Air filter diff. pressure", 0xFEF6, 4, 1, 0.05, 0, "kPa", 107, 2),
    S("Exhaust gas temp", 0xFEF6, 5, 2, 0.03125, -273, "°C", 173, 0),
    # VEP1 - electrical
    S("Charging system voltage", 0xFEF7, 2, 2, 0.05, 0, "V", 167, 2),
    S("Battery voltage", 0xFEF7, 4, 2, 0.05, 0, "V", 168, 2),
    S("Keyswitch voltage", 0xFEF7, 6, 2, 0.05, 0, "V", 158, 2),
    # AMB - ambient
    S("Barometric pressure", 0xFEF5, 0, 1, 0.5, 0, "kPa", 108),
    S("Ambient air temp", 0xFEF5, 3, 2, 0.03125, -273, "°C", 171),
    S("Air inlet temp", 0xFEF5, 5, 1, 1, -40, "°C", 172, 0),
    # Fuel / chassis
    S("Fuel rate", 0xFEF2, 0, 2, 0.05, 0, "L/h", 183, 2),
    S("Fuel level", 0xFEFC, 1, 1, 0.4, 0, "%", 96),
    S("Vehicle speed", 0xFEF1, 1, 2, 1 / 256, 0, "km/h", 84),
    # ETC1 / ETC2 - transmission (J1939-71)
    S("Transmission output shaft speed", 0xF002, 1, 2, 0.125, 0, "rpm", 191, 0),
    S("Transmission input shaft speed", 0xF002, 4, 2, 0.125, 0, "rpm", 161, 0),
    S("Transmission selected gear", 0xF005, 0, 1, 1, -125, "", 524, 0),
    S("Transmission actual gear ratio", 0xF005, 1, 2, 0.001, 0, "", 526, 3),
    S("Transmission current gear", 0xF005, 3, 1, 1, -125, "", 523, 0),
    # EBC1 - brakes / ABS (J1939-71)
    S("Brake pedal position", 0xF001, 1, 1, 0.4, 0, "%", 521),
    S("Hydraulic oil temp", 0xFE68, 0, 1, 1, -40, "°C", 1638, 0),
    S("Engine hours", 0xFEE5, 0, 4, 0.05, 0, "h", 247, 1),
    # Aftertreatment
    S("DEF tank level", 0xFE56, 0, 1, 0.4, 0, "%", 1761),
    S("DEF tank temp", 0xFE56, 1, 1, 1, -40, "°C", 3031, 0),
    S("DPF soot load", 0xFD7B, 0, 1, 1, 0, "%", 3719, 0),
    S("DPF ash load", 0xFD7B, 1, 1, 1, 0, "%", 3720, 0),
]
del S

_GROUPS = {
    0xF004: "Engine", 0xF003: "Engine", 0xFEE5: "Engine",
    0xFEEE: "Temperatures", 0xFE68: "Temperatures",
    0xFEEF: "Fluids", 0xFEF2: "Fuel", 0xFEFC: "Fuel",
    0xFEF6: "Intake / exhaust", 0xFEF5: "Intake / exhaust",
    0xFEF7: "Electrical", 0xFEF1: "Drivetrain", 0xF005: "Drivetrain", 0xF002: "Drivetrain",
    0xF001: "Brakes",
    0xFE56: "Aftertreatment", 0xFD7B: "Aftertreatment",
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
