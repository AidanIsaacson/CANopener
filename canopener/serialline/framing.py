"""How bytes go over a serial line: baud rate, data bits, parity, stop bits.

``Framing.parse`` takes the usual short forms ("9600 8N1", "4800,8E1", "19200"), ``str()`` gives
them back. ``guess_baud`` works out the rate from pulse widths (a logic analyzer's edges on the
line, ``core.logic.pulse_widths``): the narrowest pulses are one bit long. Standard library only.
"""

from dataclasses import dataclass

COMMON_BAUDS = (300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 62500,
                115200, 125000, 230400, 250000)
PARITIES = {"N": "none", "E": "even", "O": "odd", "M": "mark", "S": "space"}


@dataclass(frozen=True)
class Framing:
    baud: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1

    def __post_init__(self):
        if self.baud <= 0:
            raise ValueError(f"baud rate must be positive, not {self.baud}")
        if self.bytesize not in (5, 6, 7, 8):
            raise ValueError(f"data bits must be 5-8, not {self.bytesize}")
        if self.parity not in PARITIES:
            raise ValueError(f"parity must be one of {''.join(PARITIES)}, not {self.parity!r}")
        if self.stopbits not in (1, 1.5, 2):
            raise ValueError(f"stop bits must be 1, 1.5 or 2, not {self.stopbits}")

    @classmethod
    def parse(cls, text: str) -> "Framing":
        """ "9600 8N1", "4800,8E1", "9600/7O2", "19200" (8N1) -> Framing."""
        parts = (text or "").replace(",", " ").replace("/", " ").split()
        if not parts:
            raise ValueError("no framing given (expected e.g. 9600 8N1)")
        try:
            baud = int(parts[0])
        except ValueError:
            raise ValueError(f"bad baud rate {parts[0]!r} in {text!r}") from None
        if len(parts) == 1:
            return cls(baud)
        f = parts[1].upper()
        if len(f) < 3 or not f[0].isdigit() or not f[2:].replace(".", "").isdigit():
            raise ValueError(f"bad framing {parts[1]!r} in {text!r} (expected e.g. 8N1)")
        stop = float(f[2:])
        return cls(baud, int(f[0]), f[1], int(stop) if stop.is_integer() else stop)

    def __str__(self) -> str:
        stop = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        return f"{self.baud} {self.bytesize}{self.parity}{stop}"

    @property
    def bits_per_char(self) -> float:
        """Start bit + data + parity + stop."""
        return 1 + self.bytesize + (self.parity != "N") + self.stopbits

    @property
    def char_time(self) -> float:
        """Seconds one character takes on the wire."""
        return self.bits_per_char / self.baud


def snap_baud(baud: float, tolerance: float = 0.05) -> int | None:
    """The standard rate within ``tolerance`` (5 %) of ``baud``, else None."""
    best = min(COMMON_BAUDS, key=lambda b: abs(b - baud) / b)
    return best if abs(best - baud) / best <= tolerance else None


def guess_baud(widths: list[float]) -> tuple[float | None, int | None]:
    """(measured rate, nearest standard rate or None) from pulse widths in seconds.

    The narrowest pulses are single bits; wider ones are runs of equal bits, so whole multiples of
    it. The bit time is the average of the pulses within 25 % of the narrowest real one (a glitch
    far narrower than its neighbours is ignored), then checked against the others: if most wider
    pulses aren't close to a whole multiple, there's no clean answer (noise, or not a UART).
    """
    ws = sorted(w for w in widths if w > 0)
    if len(ws) < 4:
        return None, None
    # skip glitches: a "narrowest" pulse needs a few neighbours of about its width
    for i, w in enumerate(ws):
        near = [x for x in ws[i:] if x <= w * 1.25]
        if len(near) >= max(3, len(ws) // 50):
            break
    else:
        return None, None
    bit = sum(near) / len(near)
    fits = sum(1 for x in ws if abs(x / bit - round(x / bit)) <= 0.2)
    if fits / len(ws) < 0.8:
        return None, None
    rate = 1.0 / bit
    return rate, snap_baud(rate)
