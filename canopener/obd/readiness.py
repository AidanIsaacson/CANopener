"""I/M readiness: decode the monitor status in Mode 01 PID 0x01 (since codes cleared) and 0x41
(this drive cycle), SAE J1979.

    A: bit 7 = MIL on, bits 0-6 = number of confirmed emission codes
    B: bit 3 = compression ignition (diesel); bits 0-2 = misfire / fuel system / components
       monitor supported; bits 4-6 = the same three *not complete*
    C: monitors supported, D: the same monitors not complete. Which monitor each bit is depends
       on spark vs compression ignition (tables below).

A monitor that's supported but not complete is what makes a vehicle fail an emissions inspection.
"""

from dataclasses import dataclass, field

BASE = ("Misfire", "Fuel system", "Comprehensive components")
SPARK = ("Catalyst", "Heated catalyst", "Evaporative system", "Secondary air system", "A/C refrigerant",
         "Oxygen sensor", "Oxygen sensor heater", "EGR / VVT system")
COMPRESSION = ("NMHC catalyst", "NOx / SCR aftertreatment", None, "Boost pressure", None,
               "Exhaust gas sensor", "PM filter", "EGR / VVT system")


@dataclass(frozen=True)
class Monitor:
    name: str
    supported: bool
    complete: bool

    @property
    def state(self) -> str:
        if not self.supported:
            return "not supported"
        return "complete" if self.complete else "INCOMPLETE"


@dataclass
class MonitorStatus:
    mil: bool
    dtc_count: int
    compression: bool
    monitors: list[Monitor] = field(default_factory=list)

    @property
    def incomplete(self) -> list[Monitor]:
        return [m for m in self.monitors if m.supported and not m.complete]

    @property
    def ready(self) -> bool:
        """Everything supported has completed (what an inspection station checks, along with the MIL)."""
        return not self.incomplete


def decode_monitor_status(data: bytes) -> MonitorStatus:
    a, b, c, d = (bytes(data) + b"\x00" * 4)[:4]
    s = MonitorStatus(mil=bool(a & 0x80), dtc_count=a & 0x7F, compression=bool(b & 0x08))
    for i, name in enumerate(BASE):
        s.monitors.append(Monitor(name, bool(b & (1 << i)), not b & (1 << (4 + i))))
    for i, name in enumerate(COMPRESSION if s.compression else SPARK):
        if name is not None:
            s.monitors.append(Monitor(name, bool(c & (1 << i)), not d & (1 << i)))
    return s
