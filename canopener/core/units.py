"""Metric / imperial display conversion. Everything is stored and decoded in SI-ish units."""

METRIC = "metric"
IMPERIAL = "imperial"

_TO_IMPERIAL = {
    "°C": ("°F", lambda v: v * 9 / 5 + 32),
    "kPa": ("psi", lambda v: v * 0.1450377),
    "km/h": ("mph", lambda v: v * 0.6213712),
    "km": ("mi", lambda v: v * 0.6213712),
    "L": ("gal", lambda v: v * 0.2641721),
    "L/h": ("gal/h", lambda v: v * 0.2641721),
}


def convert(value, unit: str, system: str = METRIC):
    """Return (value, unit) in the requested unit system. None passes through."""
    if system == IMPERIAL and unit in _TO_IMPERIAL:
        new_unit, fn = _TO_IMPERIAL[unit]
        return (None if value is None else fn(value)), new_unit
    return value, unit


def display_unit(unit: str, system: str = METRIC) -> str:
    return convert(None, unit, system)[1]


def fmt(value, unit: str, decimals: int = 1, system: str = METRIC) -> str:
    v, u = convert(value, unit, system)
    if v is None:
        return "n/a"
    # Whole kPa reads fine, but psi needs a decimal to keep a similar resolution.
    if u == "psi" and decimals == 0:
        decimals = 1
    return f"{v:.{decimals}f} {u}".rstrip()
