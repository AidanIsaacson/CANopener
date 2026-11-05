"""Source-address names and the 64-bit NAME carried in address claims."""

from dataclasses import dataclass

# SAE J1939 preferred addresses (industry-group-independent range 0-127 and 248-255).
# These are conventions, not guarantees: OEM controllers often sit at other addresses,
# and addresses 128-247 depend on the industry group.
SA_NAMES = {
    0x00: "Engine #1",
    0x01: "Engine #2",
    0x02: "Turbocharger",
    0x03: "Transmission #1",
    0x04: "Transmission #2",
    0x05: "Shift console (primary)",
    0x06: "Shift console (secondary)",
    0x07: "Power take-off",
    0x08: "Axle - steering",
    0x09: "Axle - drive #1",
    0x0A: "Axle - drive #2",
    0x0B: "Brakes - system controller",
    0x0C: "Brakes - steer axle",
    0x0D: "Brakes - drive axle #1",
    0x0E: "Brakes - drive axle #2",
    0x0F: "Retarder - engine",
    0x10: "Retarder - driveline",
    0x11: "Cruise control",
    0x12: "Fuel system",
    0x13: "Steering controller",
    0x14: "Suspension - steer axle",
    0x15: "Suspension - drive axle #1",
    0x16: "Suspension - drive axle #2",
    0x17: "Instrument cluster #1",
    0x18: "Trip recorder",
    0x19: "Climate control #1",
    0x1A: "Alternator / charging system",
    0x1B: "Aerodynamic control",
    0x1C: "Vehicle navigation",
    0x1D: "Vehicle security",
    0x1E: "Electrical system",
    0x1F: "Starter system",
    0x20: "Tractor-trailer bridge #1",
    0x21: "Body controller",
    0x22: "Auxiliary valve control",
    0x23: "Hitch control",
    0x24: "Power take-off (front / secondary)",
    0x25: "Off-vehicle gateway",
    0x26: "Virtual terminal (in cab)",
    0x27: "Management computer #1",
    0x28: "Cab display #1",
    0x29: "Retarder - exhaust, engine #1",
    0x2A: "Headway controller",
    0x2B: "On-board diagnostic unit",
    0x2C: "Retarder - exhaust, engine #2",
    0x2D: "Endurance braking system",
    0x2E: "Hydraulic pump controller",
    0x2F: "Suspension - system controller #1",
    0x30: "Pneumatic - system controller",
    0x31: "Cab controller (primary)",
    0x32: "Cab controller (secondary)",
    0x33: "Tire pressure controller",
    0x34: "Ignition control module #1",
    0x35: "Ignition control module #2",
    0x36: "Seat control #1",
    0x37: "Lighting - operator controls",
    0x38: "Rear axle steering controller #1",
    0x39: "Water pump controller",
    0x3A: "Climate control #2",
    0x3B: "Transmission display (primary)",
    0x3C: "Transmission display (secondary)",
    0x3D: "Exhaust emission controller",
    0x3E: "Vehicle dynamic stability controller",
    0x3F: "Oil sensor",
    0x40: "Suspension - system controller #2",
    0x41: "Information system controller #1",
    0x42: "Ramp control",
    0x43: "Clutch / converter unit",
    0x44: "Auxiliary heater #1",
    0x45: "Auxiliary heater #2",
    0x46: "Engine valve controller",
    0x47: "Chassis controller #1",
    0x48: "Chassis controller #2",
    0x49: "Propulsion battery charger",
    0x4A: "Communications unit, cellular",
    0x4B: "Communications unit, satellite",
    0x4C: "Communications unit, radio",
    0x4D: "Steering column unit",
    0x4E: "Fan drive controller",
    0x4F: "Seat control #2",
    0x50: "Parking brake controller",
    0x51: "Aftertreatment #1 gas intake (NOx sensor)",
    0x52: "Aftertreatment #1 gas outlet (NOx sensor)",
    0x53: "Safety restraint system",
    0x54: "Cab display #2",
    0x55: "DPF controller",
    0x56: "Aftertreatment #2 gas intake",
    0x57: "Aftertreatment #2 gas outlet",
    0x58: "Safety restraint system #2",
    0x59: "Atmospheric sensor",
    0x5A: "Powertrain control module",
    0x5B: "Power systems manager",
    0x5C: "Engine injection control module",
    0xF8: "File server / printer",
    0xF9: "Off-board tool #1",
    0xFA: "Off-board tool #2",
    0xFB: "On-board data logger",
    0xFE: "Null address",
    0xFF: "Global",
}

INDUSTRY_GROUPS = {
    0: "Global",
    1: "On-highway",
    2: "Agriculture & forestry",
    3: "Construction",
    4: "Marine",
    5: "Industrial / process control / stationary",
}

# Function codes 0-127 are industry-group independent; only the unambiguous ones are named.
FUNCTIONS = {
    0: "Engine",
    3: "Transmission",
}


# Filled from the active machine profile (see canopener.core.profiles).
SA_OVERRIDES: dict[int, str] = {}


def sa_name(sa: int, with_hex: bool = True) -> str:
    name = SA_OVERRIDES.get(sa) or SA_NAMES.get(sa, "")
    if not with_hex:
        return name or f"SA {sa:02X}"
    return f"{sa:02X} {name}".strip()


def is_tool_address(sa: int) -> bool:
    return sa >= 0xF8


@dataclass(frozen=True, slots=True)
class J1939Name:
    raw: int
    identity: int
    manufacturer: int
    ecu_instance: int
    function_instance: int
    function: int
    vehicle_system: int
    vehicle_system_instance: int
    industry_group: int
    arbitrary_address_capable: bool

    @classmethod
    def from_bytes(cls, data) -> "J1939Name":
        v = int.from_bytes(bytes(data[:8]).ljust(8, b"\x00"), "little")
        return cls(
            raw=v,
            identity=v & 0x1FFFFF,
            manufacturer=(v >> 21) & 0x7FF,
            ecu_instance=(v >> 32) & 0x7,
            function_instance=(v >> 35) & 0x1F,
            function=(v >> 40) & 0xFF,
            vehicle_system=(v >> 49) & 0x7F,
            vehicle_system_instance=(v >> 56) & 0xF,
            industry_group=(v >> 60) & 0x7,
            arbitrary_address_capable=bool(v >> 63),
        )

    def to_bytes(self) -> bytes:
        return self.raw.to_bytes(8, "little")

    @classmethod
    def build(cls, *, identity=0, manufacturer=0, ecu_instance=0, function_instance=0,
              function=0, vehicle_system=0, vehicle_system_instance=0, industry_group=0,
              arbitrary_address_capable=False) -> "J1939Name":
        v = (identity & 0x1FFFFF) | ((manufacturer & 0x7FF) << 21) | ((ecu_instance & 0x7) << 32) \
            | ((function_instance & 0x1F) << 35) | ((function & 0xFF) << 40) \
            | ((vehicle_system & 0x7F) << 49) | ((vehicle_system_instance & 0xF) << 56) \
            | ((industry_group & 0x7) << 60) | (int(bool(arbitrary_address_capable)) << 63)
        return cls.from_bytes(v.to_bytes(8, "little"))

    @property
    def function_text(self) -> str:
        name = FUNCTIONS.get(self.function) if self.function < 128 else None
        return f"{self.function} ({name})" if name else str(self.function)

    @property
    def industry_text(self) -> str:
        return INDUSTRY_GROUPS.get(self.industry_group, str(self.industry_group))

    def summary(self) -> str:
        return (f"mfr {self.manufacturer}, function {self.function_text}, "
                f"inst {self.function_instance}/{self.ecu_instance}, "
                f"industry {self.industry_text}, identity {self.identity}")
