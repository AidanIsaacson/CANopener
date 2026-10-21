"""SAE J1979 Mode 01 (current data) PIDs: a useful subset with their decode formulas and units.

Each ``Pid`` decodes the data bytes that follow the mode+PID echo in a response. Units use the
same quantity names as ``core.units`` (kPa, °C, km/h, %, rpm, V, g/s, L/h, s) so the rest of the
app can convert them (e.g. to psi / °F) exactly like J1939 signals.

Standard-only (no OEM PIDs), read-only. Formulas from J1979 / the public OBD-II PID tables.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Pid:
    pid: int
    name: str
    nbytes: int
    unit: str
    decode: Callable[[bytes], float]


def _need(data: bytes, n: int) -> bytes:
    return data if len(data) >= n else data + b"\x00" * (n - len(data))


# A = first byte, B = second, etc.
PIDS: dict[int, Pid] = {p.pid: p for p in [
    Pid(0x04, "Calculated engine load", 1, "%", lambda d: _need(d, 1)[0] * 100 / 255),
    Pid(0x05, "Engine coolant temp", 1, "°C", lambda d: _need(d, 1)[0] - 40),
    Pid(0x0A, "Fuel pressure", 1, "kPa", lambda d: _need(d, 1)[0] * 3),
    Pid(0x0B, "Intake manifold pressure", 1, "kPa", lambda d: float(_need(d, 1)[0])),
    Pid(0x0C, "Engine speed", 2, "rpm", lambda d: ((_need(d, 2)[0] << 8) + _need(d, 2)[1]) / 4),
    Pid(0x0D, "Vehicle speed", 1, "km/h", lambda d: float(_need(d, 1)[0])),
    Pid(0x0E, "Timing advance", 1, "°", lambda d: _need(d, 1)[0] / 2 - 64),
    Pid(0x0F, "Intake air temp", 1, "°C", lambda d: _need(d, 1)[0] - 40),
    Pid(0x10, "MAF air flow", 2, "g/s", lambda d: ((_need(d, 2)[0] << 8) + _need(d, 2)[1]) / 100),
    Pid(0x11, "Throttle position", 1, "%", lambda d: _need(d, 1)[0] * 100 / 255),
    Pid(0x1F, "Run time since start", 2, "s", lambda d: float((_need(d, 2)[0] << 8) + _need(d, 2)[1])),
    Pid(0x21, "Distance with MIL on", 2, "km", lambda d: float((_need(d, 2)[0] << 8) + _need(d, 2)[1])),
    Pid(0x2F, "Fuel tank level", 1, "%", lambda d: _need(d, 1)[0] * 100 / 255),
    Pid(0x33, "Barometric pressure", 1, "kPa", lambda d: float(_need(d, 1)[0])),
    Pid(0x42, "Control module voltage", 2, "V", lambda d: ((_need(d, 2)[0] << 8) + _need(d, 2)[1]) / 1000),
    Pid(0x46, "Ambient air temp", 1, "°C", lambda d: _need(d, 1)[0] - 40),
    Pid(0x5C, "Engine oil temp", 1, "°C", lambda d: _need(d, 1)[0] - 40),
    Pid(0x5E, "Engine fuel rate", 2, "L/h", lambda d: ((_need(d, 2)[0] << 8) + _need(d, 2)[1]) / 20),
]}


def supported_pids(base: int, data: bytes) -> list[int]:
    """Decode a 'supported PIDs' bitmask response (PID 0x00/0x20/0x40/...). ``base`` is that
    PID number; the 32 bits cover ``base+1``..``base+0x20``, MSB first."""
    bits = int.from_bytes(_need(data, 4)[:4], "big")
    return [base + 1 + i for i in range(32) if bits & (1 << (31 - i))]
