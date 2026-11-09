"""Which vehicle / bus a Bluetooth OBD adapter connects to, for both front-ends.

Built in: a car (OBD-II, the adapter finds the protocol) and a truck or machine (J1939 at 250k,
listened to). Plugins can add more with an ``adapter_buses()`` hook returning dicts shaped like
``can.elm327.BusSetup`` (id, label, protocol, commands, undo, adapter, note, verified, source):
an adapter's own commands for reaching another bus, for example a maker's second network on
other OBD pins. Public CANopener ships none; the commands are whatever the plugin's author has
proven, and only adapter commands (AT / ST) are allowed in them.

The choice is two shared settings: ``elm.protocol`` (the ATSP protocol) and ``elm.bus`` (a
plugin bus setup's id, or ""). The pickers use the combined value ``"<protocol>|<bus id>"``.
"""

from .. import plugins
from ..can.elm327 import BusSetup

CAR, J1939 = "0|", "A|"
BUILT_IN = [
    ("Car or light truck (OBD-II)", CAR,
     "Finds the vehicle's protocol by itself: CAN cars and the older J1850 / ISO 9141 / KWP ones"),
    ("Truck or equipment (J1939, 250k)", J1939,
     "Listens to J1939 through the adapter (a 16-to-9-pin Deutsch cable on most trucks). It can miss "
     "frames on a busy bus."),
]


def available() -> list[BusSetup]:
    """Bus setups from the installed plugins (a broken entry is skipped)."""
    out = []
    for p in plugins.loaded():
        hook = p.hook("adapter_buses")
        if hook is None:
            continue
        try:
            entries = hook() or []
        except Exception:               # a broken plugin must not stop the app
            continue
        for d in entries:
            try:
                out.append(d if isinstance(d, BusSetup) else BusSetup(**d))
            except TypeError:
                continue
    return out


def find(bus_id: str) -> BusSetup | None:
    return next((b for b in available() if b.id == bus_id), None) if bus_id else None


def choices() -> list[tuple[str, str, str]]:
    """(label, value, tooltip) for the Vehicle picker."""
    out = list(BUILT_IN)
    for b in available():
        state = "" if b.commands else "  (not filled in yet)"
        tip = b.note + (f" Adapter: {b.adapter}." if b.adapter else "") + \
            (f" Proven: {b.verified}." if b.verified else " Not proven yet.")
        out.append((b.label + state, f"{b.protocol or '0'}|{b.id}", tip.strip()))
    return out


def current(settings) -> str:
    return f"{(settings.get('elm.protocol') or '0').upper()}|{settings.get('elm.bus') or ''}"


def choose(settings, value: str):
    proto, _, bus = (value or CAR).partition("|")
    settings.update({"elm.protocol": proto or "0", "elm.bus": bus})


def label(settings) -> str:
    cur = current(settings)
    return next((lb for lb, v, _ in choices() if v == cur), "Car or light truck (OBD-II)")
