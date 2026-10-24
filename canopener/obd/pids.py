"""SAE J1979 Mode 01 (current data) / Mode 02 (freeze frame) PIDs and their decode formulas.

Each ``Pid`` decodes the data bytes that follow the mode+PID echo in a response. A PID gives one
value (``decode``), several (``fields``: the O2 sensor PIDs carry a voltage *and* a trim, the
exhaust-temperature PID four sensors ...), or a text (``text``: fuel-system status, OBD standard,
fuel type ...). ``Pid.values(data)`` returns all of them as (name, value, unit) rows.

Units use the same names as ``core.units`` (kPa, °C, km/h, %, rpm, V, g/s, L/h, s) so the rest of
the app converts them (psi / °F ...) exactly like J1939 signals. Formulas are the public SAE J1979
ones, cross-checked against python-OBD (GPL-2.0-or-later) for PIDs 0x00-0x5F. Standard PIDs only;
monitor status (0x01 / 0x41) is decoded in full by ``readiness``.
"""

from collections.abc import Callable
from dataclasses import dataclass

from .dtc import decode_dtc
from .readiness import decode_monitor_status

SUPPORT_PIDS = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0)


@dataclass(frozen=True)
class Pid:
    pid: int
    name: str
    nbytes: int
    unit: str = ""
    decode: Callable[[bytes], float] | None = None
    fields: tuple = ()                      # (label, unit, fn(data) -> float | None) per value
    text: Callable[[bytes], str | None] | None = None

    def values(self, data: bytes) -> list[tuple[str, object, str]]:
        """Every value in this PID's data as (name, value, unit); value is a float or a str."""
        data = _need(data, self.nbytes)
        if self.decode is not None:
            return [(self.name, round(self.decode(data), 4), self.unit)]
        if self.text is not None:
            t = self.text(data)
            return [(self.name, t, "")] if t else []
        out = []
        for label, unit, fn in self.fields:
            v = fn(data)
            if v is not None:
                out.append((f"{self.name}: {label}", round(v, 4), unit))
        return out


def _need(data: bytes, n: int) -> bytes:
    return data if len(data) >= n else data + b"\x00" * (n - len(data))


def _u16(d: bytes, i: int = 0) -> int:
    d = _need(d, i + 2)
    return (d[i] << 8) + d[i + 1]


def _s16(d: bytes, i: int = 0) -> int:
    v = _u16(d, i)
    return v - 0x10000 if v & 0x8000 else v


def _pct(d):
    return d[0] * 100 / 255


def _trim(i: int = 0):
    return lambda d: (d[i] - 128) * 100 / 128


def _temp(i: int = 0):
    return lambda d: d[i] - 40.0


def _bit(i: int, n: int, fn):
    """Field present only when support bit ``n`` of byte ``i`` is set (multi-sensor PIDs)."""
    return lambda d: fn(d) if d[i] & (1 << n) else None


# Text tables from SAE J1979 (also in python-OBD's codes.py).
FUEL_STATUS = {1: "Open loop: engine not warm yet", 2: "Closed loop (O2 sensor feedback)",
               4: "Open loop: engine load or decel fuel cut", 8: "Open loop: system fault",
               16: "Closed loop, with a fault in the feedback system"}
AIR_STATUS = {1: "Upstream", 2: "Downstream of catalytic converter",
              4: "From the outside atmosphere or off", 8: "Pump commanded on for diagnostics"}
OBD_STANDARDS = ["Undefined", "OBD-II as defined by the CARB", "OBD as defined by the EPA", "OBD and OBD-II",
                 "OBD-I", "Not OBD compliant", "EOBD (Europe)", "EOBD and OBD-II", "EOBD and OBD",
                 "EOBD, OBD and OBD II", "JOBD (Japan)", "JOBD and OBD II", "JOBD and EOBD",
                 "JOBD, EOBD, and OBD II", "Reserved", "Reserved", "Reserved",
                 "Engine Manufacturer Diagnostics (EMD)", "Engine Manufacturer Diagnostics Enhanced (EMD+)",
                 "Heavy Duty On-Board Diagnostics (Child/Partial) (HD OBD-C)",
                 "Heavy Duty On-Board Diagnostics (HD OBD)", "World Wide Harmonized OBD (WWH OBD)", "Reserved",
                 "Heavy Duty Euro OBD Stage I without NOx control (HD EOBD-I)",
                 "Heavy Duty Euro OBD Stage I with NOx control (HD EOBD-I N)",
                 "Heavy Duty Euro OBD Stage II without NOx control (HD EOBD-II)",
                 "Heavy Duty Euro OBD Stage II with NOx control (HD EOBD-II N)", "Reserved",
                 "Brazil OBD Phase 1 (OBDBr-1)", "Brazil OBD Phase 2 (OBDBr-2)", "Korean OBD (KOBD)",
                 "India OBD I (IOBD I)", "India OBD II (IOBD II)", "Heavy Duty Euro OBD Stage VI (HD EOBD-IV)"]
FUEL_TYPES = ["Not available", "Gasoline", "Methanol", "Ethanol", "Diesel", "LPG", "CNG", "Propane", "Electric",
              "Bifuel running Gasoline", "Bifuel running Methanol", "Bifuel running Ethanol", "Bifuel running LPG",
              "Bifuel running CNG", "Bifuel running Propane", "Bifuel running Electricity",
              "Bifuel running electric and combustion engine", "Hybrid gasoline", "Hybrid Ethanol",
              "Hybrid Diesel", "Hybrid Electric", "Hybrid running electric and combustion engine",
              "Hybrid Regenerative", "Bifuel running diesel"]


def _status_text(d):
    s = decode_monitor_status(d)
    return f"MIL {'ON' if s.mil else 'off'}, {s.dtc_count} confirmed code(s), " + \
        ("compression" if s.compression else "spark") + " ignition"


def _fuel_status(d):
    parts = [f"system {i + 1}: {FUEL_STATUS.get(d[i], f'0x{d[i]:02X}')}" for i in (0, 1) if d[i]]
    return "; ".join(parts) or None


def _o2_present(d):
    on = [f"B{1 + (i // 4)}S{1 + i % 4}" for i in range(8) if d[0] & (1 << i)]
    return ", ".join(on) or "none"


def _o2_present_alt(d):
    on = [f"B{1 + (i // 2)}S{1 + i % 2}" for i in range(8) if d[0] & (1 << i)]
    return ", ".join(on) or "none"


def _o2(n: int) -> Pid:
    bank, sensor = 1 + n // 4, 1 + n % 4
    return Pid(0x14 + n, f"O2 B{bank}S{sensor}", 2, fields=(
        ("voltage", "V", lambda d: d[0] / 200),
        ("short-term trim", "%", lambda d: None if d[1] == 0xFF else (d[1] - 128) * 100 / 128)))


def _wide_v(n: int) -> Pid:
    return Pid(0x24 + n, f"O2 sensor {n + 1} (wide range)", 4, fields=(
        ("equivalence ratio", "", lambda d: _u16(d) * 2 / 65536),
        ("voltage", "V", lambda d: _u16(d, 2) * 8 / 65536)))


def _wide_i(n: int) -> Pid:
    return Pid(0x34 + n, f"O2 sensor {n + 1} (wide range)", 4, fields=(
        ("equivalence ratio", "", lambda d: _u16(d) * 2 / 65536),
        ("current", "mA", lambda d: _u16(d, 2) / 256 - 128)))


def _cat_temp(pid: int, label: str) -> Pid:
    return Pid(pid, f"Catalyst temp {label}", 2, "°C", lambda d: _u16(d) / 10 - 40)


def _egt(pid: int, bank: int) -> Pid:
    return Pid(pid, f"Exhaust gas temp bank {bank}", 9, fields=tuple(
        (f"sensor {s + 1}", "°C", _bit(0, s, lambda d, s=s: _u16(d, 1 + 2 * s) / 10 - 40)) for s in range(4)))


PIDS: dict[int, Pid] = {p.pid: p for p in [
    Pid(0x01, "Monitor status since codes cleared", 4, text=_status_text),
    Pid(0x02, "Freeze frame trouble code", 2, text=lambda d: decode_dtc(d[0], d[1]) if d[0] or d[1] else None),
    Pid(0x03, "Fuel system status", 2, text=_fuel_status),
    Pid(0x04, "Calculated engine load", 1, "%", _pct),
    Pid(0x05, "Engine coolant temp", 1, "°C", _temp()),
    Pid(0x06, "Short-term fuel trim B1", 1, "%", _trim()),
    Pid(0x07, "Long-term fuel trim B1", 1, "%", _trim()),
    Pid(0x08, "Short-term fuel trim B2", 1, "%", _trim()),
    Pid(0x09, "Long-term fuel trim B2", 1, "%", _trim()),
    Pid(0x0A, "Fuel pressure (gauge)", 1, "kPa", lambda d: d[0] * 3.0),
    Pid(0x0B, "Intake manifold pressure", 1, "kPa", lambda d: float(d[0])),
    Pid(0x0C, "Engine speed", 2, "rpm", lambda d: _u16(d) / 4),
    Pid(0x0D, "Vehicle speed", 1, "km/h", lambda d: float(d[0])),
    Pid(0x0E, "Timing advance", 1, "°", lambda d: d[0] / 2 - 64),
    Pid(0x0F, "Intake air temp", 1, "°C", _temp()),
    Pid(0x10, "MAF air flow", 2, "g/s", lambda d: _u16(d) / 100),
    Pid(0x11, "Throttle position", 1, "%", _pct),
    Pid(0x12, "Commanded secondary air status", 1, text=lambda d: AIR_STATUS.get(d[0], f"0x{d[0]:02X}")),
    Pid(0x13, "O2 sensors present", 1, text=_o2_present),
    *[_o2(n) for n in range(8)],
    Pid(0x1C, "OBD standard", 1, text=lambda d: OBD_STANDARDS[d[0]] if d[0] < len(OBD_STANDARDS) else f"0x{d[0]:02X}"),
    Pid(0x1D, "O2 sensors present (4 banks)", 1, text=_o2_present_alt),
    Pid(0x1E, "Power take-off (PTO)", 1, text=lambda d: "active" if d[0] & 1 else "inactive"),
    Pid(0x1F, "Run time since start", 2, "s", lambda d: float(_u16(d))),
    Pid(0x21, "Distance with MIL on", 2, "km", lambda d: float(_u16(d))),
    Pid(0x22, "Fuel rail pressure (vs. manifold)", 2, "kPa", lambda d: _u16(d) * 0.079),
    Pid(0x23, "Fuel rail gauge pressure", 2, "kPa", lambda d: _u16(d) * 10.0),
    *[_wide_v(n) for n in range(8)],
    Pid(0x2C, "Commanded EGR", 1, "%", _pct),
    Pid(0x2D, "EGR error", 1, "%", _trim()),
    Pid(0x2E, "Commanded evap purge", 1, "%", _pct),
    Pid(0x2F, "Fuel tank level", 1, "%", _pct),
    Pid(0x30, "Warm-ups since codes cleared", 1, "", lambda d: float(d[0])),
    Pid(0x31, "Distance since codes cleared", 2, "km", lambda d: float(_u16(d))),
    Pid(0x32, "Evap system vapor pressure", 2, "Pa", lambda d: _s16(d) / 4),
    Pid(0x33, "Barometric pressure", 1, "kPa", lambda d: float(d[0])),
    *[_wide_i(n) for n in range(8)],
    _cat_temp(0x3C, "B1S1"), _cat_temp(0x3D, "B2S1"), _cat_temp(0x3E, "B1S2"), _cat_temp(0x3F, "B2S2"),
    Pid(0x41, "Monitor status this drive cycle", 4, text=_status_text),
    Pid(0x42, "Control module voltage", 2, "V", lambda d: _u16(d) / 1000),
    Pid(0x43, "Absolute load value", 2, "%", lambda d: _u16(d) * 100 / 255),
    Pid(0x44, "Commanded equivalence ratio", 2, "", lambda d: _u16(d) * 2 / 65536),
    Pid(0x45, "Relative throttle position", 1, "%", _pct),
    Pid(0x46, "Ambient air temp", 1, "°C", _temp()),
    Pid(0x47, "Absolute throttle position B", 1, "%", _pct),
    Pid(0x48, "Absolute throttle position C", 1, "%", _pct),
    Pid(0x49, "Accelerator pedal position D", 1, "%", _pct),
    Pid(0x4A, "Accelerator pedal position E", 1, "%", _pct),
    Pid(0x4B, "Accelerator pedal position F", 1, "%", _pct),
    Pid(0x4C, "Commanded throttle actuator", 1, "%", _pct),
    Pid(0x4D, "Run time with MIL on", 2, "min", lambda d: float(_u16(d))),
    Pid(0x4E, "Time since codes cleared", 2, "min", lambda d: float(_u16(d))),
    Pid(0x4F, "Maximum values", 4, fields=(
        ("equivalence ratio", "", lambda d: float(d[0])), ("O2 sensor voltage", "V", lambda d: float(d[1])),
        ("O2 sensor current", "mA", lambda d: float(d[2])),
        ("intake manifold pressure", "kPa", lambda d: d[3] * 10.0))),
    Pid(0x50, "Maximum MAF air flow", 4, "g/s", lambda d: d[0] * 10.0),
    Pid(0x51, "Fuel type", 1, text=lambda d: FUEL_TYPES[d[0]] if d[0] < len(FUEL_TYPES) else f"0x{d[0]:02X}"),
    Pid(0x52, "Ethanol fuel", 1, "%", _pct),
    Pid(0x53, "Absolute evap system vapor pressure", 2, "kPa", lambda d: _u16(d) / 200),
    Pid(0x54, "Evap system vapor pressure (wide)", 2, "Pa", lambda d: float(_u16(d) - 32767)),
    Pid(0x55, "Short-term secondary O2 trim", 2, fields=(("bank 1", "%", _trim(0)), ("bank 3", "%", _trim(1)))),
    Pid(0x56, "Long-term secondary O2 trim", 2, fields=(("bank 1", "%", _trim(0)), ("bank 3", "%", _trim(1)))),
    Pid(0x57, "Short-term secondary O2 trim", 2, fields=(("bank 2", "%", _trim(0)), ("bank 4", "%", _trim(1)))),
    Pid(0x58, "Long-term secondary O2 trim", 2, fields=(("bank 2", "%", _trim(0)), ("bank 4", "%", _trim(1)))),
    Pid(0x59, "Fuel rail absolute pressure", 2, "kPa", lambda d: _u16(d) * 10.0),
    Pid(0x5A, "Relative accelerator pedal position", 1, "%", _pct),
    Pid(0x5B, "Hybrid battery pack remaining life", 1, "%", _pct),
    Pid(0x5C, "Engine oil temp", 1, "°C", _temp()),
    Pid(0x5D, "Fuel injection timing", 2, "°", lambda d: (_u16(d) - 26880) / 128),
    Pid(0x5E, "Engine fuel rate", 2, "L/h", lambda d: _u16(d) / 20),
    Pid(0x61, "Driver's demand engine torque", 1, "%", lambda d: d[0] - 125.0),
    Pid(0x62, "Actual engine torque", 1, "%", lambda d: d[0] - 125.0),
    Pid(0x63, "Engine reference torque", 2, "N·m", lambda d: float(_u16(d))),
    Pid(0x64, "Engine percent torque", 5, fields=tuple(
        (label, "%", lambda d, i=i: d[i] - 125.0)
        for i, label in enumerate(("idle", "point 1", "point 2", "point 3", "point 4")))),
    Pid(0x66, "MAF sensor", 5, fields=(
        ("A", "g/s", _bit(0, 0, lambda d: _u16(d, 1) / 32)), ("B", "g/s", _bit(0, 1, lambda d: _u16(d, 3) / 32)))),
    Pid(0x67, "Engine coolant temp", 3, fields=(
        ("sensor 1", "°C", _bit(0, 0, _temp(1))), ("sensor 2", "°C", _bit(0, 1, _temp(2))))),
    Pid(0x68, "Intake air temp", 3, fields=(
        ("bank 1 sensor 1", "°C", _bit(0, 0, _temp(1))), ("bank 1 sensor 2", "°C", _bit(0, 1, _temp(2))))),
    _egt(0x78, 1), _egt(0x79, 2),
    Pid(0x7F, "Engine run time", 13, fields=(
        ("total", "s", _bit(0, 0, lambda d: float(int.from_bytes(d[1:5], "big")))),
        ("idle", "s", _bit(0, 1, lambda d: float(int.from_bytes(d[5:9], "big")))),
        ("with PTO", "s", _bit(0, 2, lambda d: float(int.from_bytes(d[9:13], "big")))))),
    Pid(0x8E, "Engine friction torque", 1, "%", lambda d: d[0] - 125.0),
    Pid(0x9D, "Fuel rate (mass)", 4, fields=(
        ("engine", "g/s", lambda d: _u16(d) / 50), ("vehicle", "g/s", lambda d: _u16(d, 2) / 50))),
    Pid(0x9E, "Engine exhaust flow rate", 2, "kg/h", lambda d: _u16(d) / 5),
    Pid(0xA2, "Cylinder fuel rate", 2, "mg/stroke", lambda d: _u16(d) / 32),
    Pid(0xA6, "Odometer", 4, "km", lambda d: int.from_bytes(d[:4], "big") / 10),
]}


def supported_pids(base: int, data: bytes) -> list[int]:
    """Decode a 'supported PIDs' bitmask response (PID 0x00/0x20/0x40/...). ``base`` is that
    PID number; the 32 bits cover ``base+1``..``base+0x20``, MSB first."""
    bits = int.from_bytes(_need(data, 4)[:4], "big")
    return [base + 1 + i for i in range(32) if bits & (1 << (31 - i))]


def pid_name(pid: int) -> str:
    p = PIDS.get(pid)
    return p.name if p else f"PID 0x{pid:02X}"


# Live Data groups (for the group filter), by PID.
_GROUPS = [
    ("Engine", (0x04, 0x0C, 0x0E, 0x1F, 0x43, 0x4D, 0x4E, 0x5D, 0x61, 0x62, 0x63, 0x64, 0x7F, 0x8E)),
    ("Temperatures", (0x05, 0x0F, 0x3C, 0x3D, 0x3E, 0x3F, 0x46, 0x5C, 0x67, 0x68, 0x78, 0x79)),
    ("Fuel", (0x03, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x22, 0x23, 0x2F, 0x44, 0x51, 0x52, 0x59, 0x5E, 0x9D, 0xA2)),
    ("Air & throttle", (0x0B, 0x10, 0x11, 0x33, 0x45, 0x47, 0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4F, 0x50, 0x5A, 0x66)),
    ("Oxygen sensors", tuple(range(0x13, 0x1C)) + (0x1D,) + tuple(range(0x24, 0x2C)) + tuple(range(0x34, 0x3C))
     + (0x55, 0x56, 0x57, 0x58)),
    ("Emissions", (0x12, 0x2C, 0x2D, 0x2E, 0x30, 0x31, 0x32, 0x53, 0x54, 0x9E)),
    ("Vehicle", (0x0D, 0x1C, 0x1E, 0x21, 0x42, 0x5B, 0xA6)),
]
PID_GROUPS = {pid: name for name, pids in _GROUPS for pid in pids}


def pid_group(pid: int) -> str:
    return PID_GROUPS.get(pid, "Other")
