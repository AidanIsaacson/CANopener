"""Live-data signal definitions for common broadcast PGNs (J1939-71).

Signals are byte-aligned for now; that covers nearly all of the standard engine and
chassis parameters. ``start`` is the 0-based byte index (J1939 docs count from 1).
"""

from dataclasses import dataclass

from . import fields


@dataclass(frozen=True, slots=True)
class Signal:
    name: str
    pgn: int
    start: int
    length: int         # bytes: 1, 2 or 4
    scale: float
    offset: float
    unit: str
    spn: int | None = None
    decimals: int = 1
    sa: int | None = None       # only decode from this source address (profile signals)
    na_check: bool = True       # treat 0xFB-0xFF.. as 'not available' (standard J1939 data)

    @property
    def key(self) -> str:
        return f"{self.pgn:05X}:{self.start}:{self.name}"

    def decode(self, data) -> float | None:
        return fields.read(data, self.start, self.length, self.scale, self.offset, self.na_check)

    def encode_into(self, buf: bytearray, value) -> None:
        buf[self.start:self.start + self.length] = fields.encode(value, self.length, self.scale, self.offset)


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
    S("Transmission current gear", 0xF005, 3, 1, 1, -125, "", 523, 0),
    S("Hydraulic oil temp", 0xFE68, 0, 1, 1, -40, "°C", 1638, 0),
    S("Engine hours", 0xFEE5, 0, 4, 0.05, 0, "h", 247, 1),
    # Aftertreatment
    S("DEF tank level", 0xFE56, 0, 1, 0.4, 0, "%", 1761),
    S("DEF tank temp", 0xFE56, 1, 1, 1, -40, "°C", 3031, 0),
    S("DPF soot load", 0xFD7B, 0, 1, 1, 0, "%", 3719, 0),
    S("DPF ash load", 0xFD7B, 1, 1, 1, 0, "%", 3720, 0),
]
del S

SIGNALS_BY_PGN: dict[int, list[Signal]] = {}
for _s in SIGNALS:
    SIGNALS_BY_PGN.setdefault(_s.pgn, []).append(_s)

# Extra signals from the active machine profile, e.g. labelled proprietary frames.
PROFILE_SIGNALS: list[Signal] = []


def all_signals() -> list[Signal]:
    return SIGNALS + PROFILE_SIGNALS
