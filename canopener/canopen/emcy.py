"""EMCY (emergency) messages: a node reports an error as it happens, and sends code 0x0000 when
its errors clear. Meanings are the generic CiA 301 classes (plus the few specific codes it
defines); what a manufacturer puts in the five extra bytes is theirs and is shown as hex."""

from dataclasses import dataclass

NO_ERROR = 0x0000

# CiA 301 error code classes (high byte, or high nibble for the broad ones), most specific first.
_CLASSES = [
    (0xFF00, 0xFF00, "Device specific"),
    (0xFF00, 0xF000, "Additional functions"),
    (0xFF00, 0x1000, "Generic error"),
    (0xFF00, 0x2100, "Current, device input side"),
    (0xFF00, 0x2200, "Current inside the device"),
    (0xFF00, 0x2300, "Current, device output side"),
    (0xF000, 0x2000, "Current"),
    (0xFF00, 0x3100, "Mains voltage"),
    (0xFF00, 0x3200, "Voltage inside the device"),
    (0xFF00, 0x3300, "Output voltage"),
    (0xF000, 0x3000, "Voltage"),
    (0xFF00, 0x4100, "Ambient temperature"),
    (0xFF00, 0x4200, "Device temperature"),
    (0xF000, 0x4000, "Temperature"),
    (0xF000, 0x5000, "Device hardware"),
    (0xFF00, 0x6100, "Internal software"),
    (0xFF00, 0x6200, "User software"),
    (0xFF00, 0x6300, "Data set"),
    (0xF000, 0x6000, "Device software"),
    (0xF000, 0x7000, "Additional modules"),
    (0xFF00, 0x8100, "Communication"),
    (0xFF00, 0x8200, "Protocol error"),
    (0xF000, 0x8000, "Monitoring"),
    (0xF000, 0x9000, "External error"),
]

# Specific codes CiA 301 defines.
SPECIFIC = {
    0x0000: "Error reset / no error",
    0x8110: "CAN overrun (messages lost)",
    0x8120: "CAN in error passive mode",
    0x8130: "Life guard or heartbeat error",
    0x8140: "Recovered from bus off",
    0x8150: "CAN-ID collision",
    0x8210: "PDO not processed (length error)",
    0x8220: "PDO length exceeded",
    0x8230: "DAM MPDO not processed (destination object not available)",
    0x8240: "Unexpected SYNC data length",
    0x8250: "RPDO timeout",
}

# Error register (object 0x1001, byte 2 of an EMCY): bit -> meaning.
REGISTER_BITS = {0: "generic", 1: "current", 2: "voltage", 3: "temperature", 4: "communication",
                 5: "device profile", 7: "manufacturer"}


@dataclass(frozen=True)
class Emcy:
    code: int
    register: int
    data: bytes             # the manufacturer-specific bytes (up to 5)

    @property
    def text(self) -> str:
        return describe(self.code)

    @property
    def reset(self) -> bool:
        """True for 'my errors cleared'."""
        return self.code == NO_ERROR

    @property
    def register_text(self) -> str:
        return register_flags(self.register)

    @property
    def code_str(self) -> str:
        return f"{self.code:04X}"


def decode(data: bytes) -> Emcy | None:
    if len(data) < 3:
        return None
    return Emcy(data[0] | data[1] << 8, data[2], bytes(data[3:8]))


def encode(code: int, register: int, mfr: bytes = b"") -> bytes:
    return bytes([code & 0xFF, code >> 8, register]) + bytes(mfr[:5]).ljust(5, b"\x00")


# Text for manufacturer codes from the active machine profile (``"emcy"``), filled by core.profiles.
EMCY_OVERRIDES: dict[int, str] = {}


def describe(code: int) -> str:
    if code in EMCY_OVERRIDES:
        return EMCY_OVERRIDES[code]
    if code in SPECIFIC:
        return SPECIFIC[code]
    for mask, value, text in _CLASSES:
        if code & mask == value:
            return text
    return "Unknown error class"


def register_flags(register: int) -> str:
    flags = [text for bit, text in REGISTER_BITS.items() if register >> bit & 1]
    return ", ".join(flags) if flags else "none"
