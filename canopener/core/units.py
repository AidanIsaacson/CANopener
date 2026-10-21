"""Display units. Everything is stored and decoded in the units the data arrives in (SI-ish);
conversion happens only for display.

``system`` is one of three choices:

* ``METRIC``: show values as reported (kPa, °C, km/h ...)
* ``IMPERIAL``: psi, °F, mph, mi, gal, gal/h
* ``CUSTOM``: one unit per quantity, set with ``set_custom()``, e.g. psi but °C
"""

METRIC = "metric"
IMPERIAL = "imperial"
CUSTOM = "custom"
SYSTEMS = (METRIC, IMPERIAL, CUSTOM)

# quantity -> (base unit, {display unit: function(base value) -> display value})
QUANTITIES: dict[str, tuple[str, dict]] = {
    "pressure": ("kPa", {"kPa": lambda v: v, "psi": lambda v: v * 0.1450377, "bar": lambda v: v / 100}),
    "temperature": ("°C", {"°C": lambda v: v, "°F": lambda v: v * 9 / 5 + 32}),
    "speed": ("km/h", {"km/h": lambda v: v, "mph": lambda v: v * 0.6213712}),
    "distance": ("km", {"km": lambda v: v, "mi": lambda v: v * 0.6213712}),
    "volume": ("L", {"L": lambda v: v, "gal": lambda v: v * 0.2641721}),
    "flow": ("L/h", {"L/h": lambda v: v, "gal/h": lambda v: v * 0.2641721}),
}
QUANTITY_LABELS = {"pressure": "Pressure", "temperature": "Temperature", "speed": "Speed",
                   "distance": "Distance", "volume": "Volume", "flow": "Fuel / fluid flow"}

# reported unit -> (quantity, factor to the quantity's base unit)
_SOURCES = {
    "kPa": ("pressure", 1.0), "MPa": ("pressure", 1000.0), "bar": ("pressure", 100.0),
    "°C": ("temperature", 1.0), "km/h": ("speed", 1.0), "km": ("distance", 1.0),
    "L": ("volume", 1.0), "L/h": ("flow", 1.0),
}

PRESETS = {
    METRIC: {q: base for q, (base, _) in QUANTITIES.items()},
    IMPERIAL: {"pressure": "psi", "temperature": "°F", "speed": "mph", "distance": "mi",
               "volume": "gal", "flow": "gal/h"},
}
_custom: dict[str, str] = dict(PRESETS[METRIC])

# Extra decimals so a converted value keeps about the resolution of the original
# (whole kPa -> psi needs one decimal, -> bar two).
_EXTRA_DECIMALS = {("kPa", "psi"): 1, ("kPa", "bar"): 2, ("MPa", "psi"): 0, ("MPa", "bar"): 1}


def set_custom(prefs: dict[str, str]):
    """Choose a display unit per quantity for the CUSTOM system (unknown entries are ignored)."""
    for q, u in (prefs or {}).items():
        if q in QUANTITIES and u in QUANTITIES[q][1]:
            _custom[q] = u


def custom() -> dict[str, str]:
    return dict(_custom)


def prefs(system: str) -> dict[str, str]:
    return _custom if system == CUSTOM else PRESETS.get(system, PRESETS[METRIC])


def describe(system: str) -> str:
    if system != CUSTOM:
        return system.capitalize()
    return "Custom (" + ", ".join(_custom[q] for q in ("pressure", "temperature", "speed")) + ")"


# Spellings seen in OEM data -> the units this app converts and displays.
_UNIT_ALIASES = {
    "c": "°C", "degc": "°C", "deg c": "°C", "°c": "°C", "celsius": "°C", "degrees c": "°C",
    "v": "V", "volt": "V", "volts": "V",
    "a": "A", "amp": "A", "amps": "A", "ma": "mA",
    "kph": "km/h", "kmph": "km/h", "km/hr": "km/h",
    "rpm": "rpm", "deg": "°", "degree": "°", "degrees": "°",
    "min": "min", "minute": "min", "minutes": "min",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "kpa": "kPa", "mpa": "MPa", "l/h": "L/h", "lph": "L/h", "l": "L",
}


def normalize_unit(unit: str) -> str:
    """'C' / 'Volts' / 'kph' / 'RPM' -> '°C' / 'V' / 'km/h' / 'rpm'; unknown units pass through."""
    u = (unit or "").strip()
    return _UNIT_ALIASES.get(u.lower(), u)


def convert(value, unit: str, system: str = METRIC):
    """Return (value, unit) for display in ``system``. None passes through."""
    if system == METRIC or not unit:
        return value, unit
    base, _, qualifier = unit.partition(" ")        # "kPa gauge" converts like "kPa"
    src = _SOURCES.get(base)
    if src is None:
        return value, unit
    quantity, factor = src
    target = prefs(system).get(quantity, QUANTITIES[quantity][0])
    if target == base:
        return value, unit
    fn = QUANTITIES[quantity][1][target]
    shown = f"{target} {qualifier}".strip()
    return (None if value is None else fn(value * factor)), shown


def display_unit(unit: str, system: str = METRIC) -> str:
    return convert(None, unit, system)[1]


def display_decimals(unit: str, shown_unit: str, decimals: int) -> int:
    """Decimals to show after converting ``unit`` -> ``shown_unit``."""
    extra = _EXTRA_DECIMALS.get((unit.partition(" ")[0], shown_unit.partition(" ")[0]), 0)
    return decimals + extra if decimals < 2 else decimals


def fmt(value, unit: str, decimals: int = 1, system: str = METRIC) -> str:
    v, u = convert(value, unit, system)
    if v is None:
        return "n/a"
    return f"{v:.{display_decimals(unit, u, decimals)}f} {u}".rstrip()
