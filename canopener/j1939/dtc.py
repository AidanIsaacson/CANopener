"""Diagnostic trouble codes and the DM1/DM2/DM4/DM5-style messages that carry them (J1939-73)."""

from dataclasses import dataclass

from .fields import u8, u16

FMI_TEXT = {
    0: "Above normal range (most severe)",
    1: "Below normal range (most severe)",
    2: "Data erratic, intermittent or incorrect",
    3: "Voltage above normal / shorted high",
    4: "Voltage below normal / shorted low",
    5: "Current below normal / open circuit",
    6: "Current above normal / grounded circuit",
    7: "Mechanical system not responding / out of adj.",
    8: "Abnormal frequency, pulse width or period",
    9: "Abnormal update rate",
    10: "Abnormal rate of change",
    11: "Root cause not known",
    12: "Bad intelligent device or component",
    13: "Out of calibration",
    14: "Special instructions",
    15: "Above normal (least severe)",
    16: "Above normal (moderately severe)",
    17: "Below normal (least severe)",
    18: "Below normal (moderately severe)",
    19: "Received network data in error",
    20: "Data drifted high",
    21: "Data drifted low",
    31: "Condition exists",
}


def fmi_text(fmi: int) -> str:
    return FMI_TEXT.get(fmi, "Reserved")


# ----------------------------------------------------------------------------
# Lamps
# ----------------------------------------------------------------------------
_FLASH = {0: "slow flash", 1: "fast flash"}


@dataclass(frozen=True, slots=True)
class Lamp:
    status: int = 3     # 0 off, 1 on, 2 error, 3 not available
    flash: int = 3      # 0 slow (1 Hz), 1 fast (2 Hz), 2 reserved, 3 not flashing / n/a

    @property
    def on(self) -> bool:
        return self.status == 1

    @property
    def flashing(self) -> bool:
        return self.on and self.flash in _FLASH

    def text(self) -> str:
        if self.status == 1:
            return "ON" + (f" ({_FLASH[self.flash]})" if self.flash in _FLASH else "")
        return "off" if self.status == 0 else "n/a"


LAMP_NAMES = ("MIL", "Stop", "Warn", "Protect")


@dataclass(frozen=True, slots=True)
class Lamps:
    mil: Lamp = Lamp()
    stop: Lamp = Lamp()      # red stop lamp
    warn: Lamp = Lamp()      # amber warning lamp
    protect: Lamp = Lamp()

    @classmethod
    def from_bytes(cls, b0: int, b1: int) -> "Lamps":
        def lamp(shift):
            return Lamp((b0 >> shift) & 3, (b1 >> shift) & 3)
        return cls(lamp(6), lamp(4), lamp(2), lamp(0))

    def items(self):
        return zip(LAMP_NAMES, (self.mil, self.stop, self.warn, self.protect))

    @property
    def any_on(self) -> bool:
        return any(lamp.on for _, lamp in self.items())

    def __str__(self) -> str:
        return " ".join(f"{name}:{lamp.text()}" for name, lamp in self.items())


# ----------------------------------------------------------------------------
# DTCs
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Dtc:
    spn: int
    fmi: int
    oc: int | None          # occurrence count, None if not available
    cm: int = 0             # SPN conversion method bit
    alt_spn: int | None = None  # SPN under the old (CM=1) byte order, if CM is set

    def raw(self) -> bytes:
        """Encode in the current (CM=0) byte order."""
        return bytes([
            self.spn & 0xFF, (self.spn >> 8) & 0xFF,
            ((self.spn >> 11) & 0xE0) | (self.fmi & 0x1F),
            ((self.cm & 1) << 7) | (0x7F if self.oc is None else self.oc & 0x7F),
        ])


def decode_dtc(b) -> Dtc | None:
    b = bytes(b)
    if len(b) < 4 or b[:4] in (b"\xff\xff\xff\xff", b"\x00\x00\x00\x00"):
        return None
    spn = b[0] | (b[1] << 8) | ((b[2] & 0xE0) << 11)
    fmi = b[2] & 0x1F
    cm = b[3] >> 7
    oc = b[3] & 0x7F
    if (spn == 0 and fmi == 0) or spn == 0x7FFFF:
        return None
    # CM=1 means the ECU uses an old (pre-version-4) SPN byte order; keep the alternate too.
    alt = ((b[0] << 11) | (b[1] << 3) | (b[2] >> 5)) if cm else None
    return Dtc(spn, fmi, None if oc == 0x7F else oc, cm, alt)


def decode_dm(data) -> tuple[Lamps, list[Dtc]]:
    """Decode a DM1/DM2/DM6/DM12/DM23 payload: 2 lamp bytes then 4 bytes per DTC."""
    data = bytes(data)
    lamps = Lamps.from_bytes(data[0], data[1]) if len(data) >= 2 else Lamps()
    dtcs = []
    for i in range(2, len(data) - 3, 4):
        d = decode_dtc(data[i:i + 4])
        if d:
            dtcs.append(d)
    return lamps, dtcs


def encode_dm(lamps_b0: int, lamps_b1: int, dtcs: list[Dtc]) -> bytes:
    """Build a DM1-style payload. With no DTCs this is the standard 'no faults' frame."""
    body = b"".join(d.raw() for d in dtcs) or b"\x00\x00\x00\x00"
    out = bytes([lamps_b0, lamps_b1]) + body
    return out.ljust(8, b"\xff")


# ----------------------------------------------------------------------------
# DM4 freeze frames / DM5 readiness
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class FreezeFrame:
    dtc: Dtc
    torque_mode: int | None
    boost_kpa: float | None
    rpm: float | None
    load_pct: float | None
    coolant_c: float | None
    speed_kmh: float | None
    extra: bytes


def decode_dm4(data) -> list[FreezeFrame]:
    """[len][DTC x4][torque mode][boost][rpm x2][load][coolant][speed x2][mfr ...] repeated."""
    data = bytes(data)
    frames, i = [], 0
    while i < len(data):
        ln = data[i]
        if ln in (0, 0xFF) or i + 1 + ln > len(data):
            break
        body = data[i + 1:i + 1 + ln]
        i += 1 + ln
        dtc = decode_dtc(body[0:4])
        if not dtc:
            continue
        frames.append(FreezeFrame(
            dtc=dtc,
            torque_mode=(body[4] & 0x0F) if len(body) > 4 else None,
            boost_kpa=u8(body, 5, 2),
            rpm=u16(body, 6, 0.125),
            load_pct=u8(body, 8, 1),
            coolant_c=u8(body, 9, 1, -40),
            speed_kmh=u16(body, 10, 1 / 256),
            extra=body[12:],
        ))
    return frames


OBD_COMPLIANCE = {
    1: "OBD II (California ARB)",
    2: "OBD (Federal EPA)",
    3: "OBD and OBD II",
    4: "OBD I",
    5: "Not intended to meet OBD requirements",
    6: "EOBD",
}


@dataclass(frozen=True)
class Readiness:
    active: int | None
    previously_active: int | None
    obd_compliance: int | None

    @property
    def obd_text(self) -> str:
        if self.obd_compliance is None:
            return "n/a"
        return OBD_COMPLIANCE.get(self.obd_compliance, f"0x{self.obd_compliance:02X}")


def decode_dm5(data) -> Readiness | None:
    data = bytes(data)
    if len(data) < 3:
        return None
    return Readiness(
        None if data[0] == 0xFF else data[0],
        None if data[1] == 0xFF else data[1],
        None if data[2] == 0xFF else data[2],
    )
