"""The handheld's own battery: a power-tool pack read through a divider on the button ADC's A0.

The prototype runs from a DeWalt 20V MAX pack (5 Li-ion cells in series) through a buck
converter. A 100 kohm / 15 kohm divider brings the pack's 13-21 V down to under 3.3 V for the
ADS1115 at 0x49 (docs/DEVICE.md, "Buttons and battery on the second ADC"); ``battery.divider`` is
pack volts per ADC volt, ``battery.cells`` the cells in series.

Power-tool packs don't all cut off by themselves when they're flat (some leave that to the
tool), so ``BatteryMonitor`` warns at low charge and, while ``battery.power_off`` is on, asks for
a power-off once the pack has sat at or below ``EMPTY_CELL_V`` per cell for ``EMPTY_HOLD_S``.

An input with no divider on it floats, and a floating input can read anything. So nothing counts
until the input has read like a real, charged pack (``CONFIRM_CELL_V`` per cell or more) for
``CONFIRM_READINGS`` readings in a row, all close together. Until then the battery isn't shown,
nothing warns and nothing powers off. A reading below ``ABSENT_V`` (the divider taken off) goes
back to that.

Standard library only; the handheld app feeds it readings (``device/input.ButtonAdc``).
"""

from dataclasses import dataclass

# Rough resting volts per Li-ion cell -> charge left (%). Under the handheld's light load (a few
# watts from a pack of 2 Ah or more) the sag is small. [verify the ends against your pack]
CELL_CURVE = [(3.00, 0), (3.30, 5), (3.50, 15), (3.60, 25), (3.70, 45), (3.80, 62), (3.90, 76),
              (4.00, 90), (4.10, 98), (4.20, 100)]
LOW_PCT, VERY_LOW_PCT = 20, 8
EMPTY_CELL_V = 3.0              # at or below this per cell, the pack needs to rest
EMPTY_HOLD_S = 30.0             # for this long (a dip while something starts up isn't empty)
ABSENT_V = 10.0                 # below this the pack can't be what's powering the Pi
CONFIRM_CELL_V = 3.3            # a pack is believed once it has read at least this per cell ...
CONFIRM_READINGS = 5            # ... this many readings in a row ...
CONFIRM_SPREAD = 0.3            # ... within this many volts of each other
SMOOTH = 0.2                    # each reading's share of the shown value (the rest is the last one)

OK, LOW, VERY_LOW, EMPTY, ABSENT = "ok", "low", "very low", "empty", "absent"


def percent(pack_v: float, cells: int = 5) -> int:
    """Charge left, 0-100, from the pack's volts."""
    v = pack_v / max(1, cells)
    if v <= CELL_CURVE[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(CELL_CURVE, CELL_CURVE[1:]):
        if v <= v1:
            return round(p0 + (p1 - p0) * (v - v0) / (v1 - v0))
    return 100


@dataclass
class Battery:
    volts: float                # the pack, smoothed
    percent: int
    state: str                  # OK / LOW / VERY_LOW / EMPTY / ABSENT

    @property
    def level(self) -> str:
        """Lamp colour (core.measure levels)."""
        return {OK: "ok", LOW: "wait", VERY_LOW: "bad", EMPTY: "bad"}.get(self.state, "off")

    @property
    def text(self) -> str:
        return f"{self.percent}%" if self.state != ABSENT else "no pack"

    @property
    def shown(self) -> bool:
        """Worth a place in the status strip (not while nothing's believed to be on the input)."""
        return self.state != ABSENT


class BatteryMonitor:
    """Feed it ADC readings (``update``); it keeps ``battery`` and says once when something needs
    the user: "low", "very low", "empty" (power off now), or None."""

    def __init__(self, divider: float = 115 / 15, cells: int = 5, power_off: bool = True):
        self.divider, self.cells, self.power_off = divider or 1.0, cells or 5, power_off
        self.battery: Battery | None = None
        self.confirmed = False          # the input has read like a real pack (see the module notes)
        self._said = OK
        self._empty_since: float | None = None
        self._recent: list[float] = []

    def update(self, adc_v: float | None, now: float) -> str | None:
        if adc_v is None:
            return None
        pack = adc_v * self.divider
        if pack < ABSENT_V:
            self.battery = Battery(round(pack, 2), 0, ABSENT)
            self._empty_since, self.confirmed, self._recent = None, False, []
            return None
        if not self.confirmed:
            self._recent = (self._recent + [pack])[-CONFIRM_READINGS:]
            r = self._recent
            if (len(r) < CONFIRM_READINGS or min(r) < CONFIRM_CELL_V * self.cells
                    or max(r) - min(r) > CONFIRM_SPREAD):
                self.battery = Battery(round(pack, 2), 0, ABSENT)
                return None
            self.confirmed = True
        old = self.battery
        v = pack if old is None or old.state == ABSENT else old.volts + (pack - old.volts) * SMOOTH
        pct = percent(v, self.cells)
        state = OK if pct > LOW_PCT else LOW if pct > VERY_LOW_PCT else VERY_LOW
        empty = pack / self.cells <= EMPTY_CELL_V          # on the raw reading: smoothing would delay it
        if empty:
            self._empty_since = now if self._empty_since is None else self._empty_since
            if now - self._empty_since >= EMPTY_HOLD_S:
                state = EMPTY
        else:
            self._empty_since = None
        self.battery = Battery(round(v, 2), pct, state)
        rank = [OK, LOW, VERY_LOW, EMPTY]
        if state == EMPTY and not self.power_off:
            state = VERY_LOW
        if rank.index(state) > rank.index(self._said):
            self._said = state
            return state
        if state == OK and pct > LOW_PCT + 5:              # charged again (or a fresh pack): warn next time
            self._said = OK
        return None

    def message(self, event: str) -> str:
        b = self.battery
        pct = f"{b.percent}%" if b is not None else ""
        volts = f"{b.volts:.1f} V" if b is not None else ""
        return {LOW: f"Battery low: {pct} ({volts}). Swap the pack soon.",
                VERY_LOW: f"Battery very low: {pct} ({volts}). Save your work and swap the pack.",
                EMPTY: f"Battery empty ({volts}): powering off to protect the pack. A log is saved first."
                }.get(event, "")
