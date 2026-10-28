"""Standard object dictionary entries (CiA 301 communication profile, and the few CiA 402 drive
objects a mechanic is likely to want), device profile numbers, and how to show their values."""

DEVICE_TYPE = 0x1000
ERROR_REGISTER = 0x1001
MFR_STATUS = 0x1002
ERROR_HISTORY = 0x1003          # pre-defined error field: sub 0 = count, 1.. = newest first
DEVICE_NAME = 0x1008
HARDWARE_VERSION = 0x1009
SOFTWARE_VERSION = 0x100A
HEARTBEAT_TIME = 0x1017
IDENTITY = 0x1018               # 1 vendor ID, 2 product code, 3 revision, 4 serial number

NAMES = {
    0x1000: "Device type", 0x1001: "Error register", 0x1002: "Manufacturer status register",
    0x1003: "Pre-defined error field (error history)", 0x1005: "COB-ID SYNC",
    0x1006: "Communication cycle period", 0x1008: "Manufacturer device name",
    0x1009: "Manufacturer hardware version", 0x100A: "Manufacturer software version",
    0x100C: "Guard time", 0x100D: "Life time factor", 0x1010: "Store parameters",
    0x1011: "Restore default parameters", 0x1012: "COB-ID TIME", 0x1014: "COB-ID EMCY",
    0x1015: "Inhibit time EMCY", 0x1016: "Consumer heartbeat time", 0x1017: "Producer heartbeat time",
    0x1018: "Identity object", 0x1029: "Error behaviour",
    0x603F: "Error code (drive)", 0x6040: "Controlword", 0x6041: "Statusword",
    0x6060: "Modes of operation", 0x6061: "Modes of operation display", 0x6064: "Position actual value",
    0x606C: "Velocity actual value", 0x6077: "Torque actual value", 0x6502: "Supported drive modes",
}
IDENTITY_SUBS = {1: "Vendor ID", 2: "Product code", 3: "Revision number", 4: "Serial number"}
TEXT_OBJECTS = (DEVICE_NAME, HARDWARE_VERSION, SOFTWARE_VERSION)

DEVICE_PROFILES = {
    0: "no standard device profile",
    401: "CiA 401 generic I/O module",
    402: "CiA 402 drive / motion controller",
    404: "CiA 404 measuring device / closed-loop controller",
    406: "CiA 406 encoder",
    408: "CiA 408 proportional hydraulic valve",
    410: "CiA 410 inclinometer",
    418: "CiA 418 battery module",
    419: "CiA 419 battery charger",
}


def name(index: int, sub: int = 0) -> str:
    if 0x1400 <= index <= 0x15FF:
        return f"RPDO{index - 0x1400 + 1} communication"
    if 0x1600 <= index <= 0x17FF:
        return f"RPDO{index - 0x1600 + 1} mapping"
    if 0x1800 <= index <= 0x19FF:
        return f"TPDO{index - 0x1800 + 1} communication"
    if 0x1A00 <= index <= 0x1BFF:
        return f"TPDO{index - 0x1A00 + 1} mapping"
    if index == IDENTITY and sub in IDENTITY_SUBS:
        return IDENTITY_SUBS[sub]
    if index in NAMES:
        return NAMES[index]
    if 0x2000 <= index <= 0x5FFF:
        return "Manufacturer-specific"
    if 0x6000 <= index <= 0x9FFF:
        return "Device profile object"
    return "Object"


def device_profile(device_type: int) -> str:
    """'CiA 402 drive / motion controller' from object 0x1000's value (low 16 bits)."""
    num = device_type & 0xFFFF
    return DEVICE_PROFILES.get(num, f"device profile {num}")


def as_uint(value: bytes) -> int:
    return int.from_bytes(bytes(value[:4]), "little")


def as_text(value: bytes) -> str:
    return bytes(value).split(b"\x00", 1)[0].decode("latin-1").strip()


def show(index: int, sub: int, value: bytes) -> str:
    """A readable rendering of a value read from ``index`` / ``sub``."""
    if index in TEXT_OBJECTS:
        return as_text(value)
    if index == DEVICE_TYPE and len(value) >= 4:
        v = as_uint(value)
        return f"0x{v:08X} ({device_profile(v)})"
    if index == ERROR_REGISTER and value:
        from .emcy import register_flags
        return f"0x{value[0]:02X} ({register_flags(value[0])})"
    if index == HEARTBEAT_TIME and len(value) >= 2:
        return f"{as_uint(value)} ms"
    if index == ERROR_HISTORY and sub > 0 and len(value) >= 4:
        from .emcy import describe
        v = as_uint(value)
        return f"{v & 0xFFFF:04X} {describe(v & 0xFFFF)} (extra 0x{v >> 16:04X})"
    if index == IDENTITY and len(value) >= 4:
        return f"0x{as_uint(value):08X}"
    if 1 <= len(value) <= 4:
        v = as_uint(value)
        return f"{v} (0x{v:0{len(value) * 2}X})"
    return bytes(value).hex(" ").upper()
