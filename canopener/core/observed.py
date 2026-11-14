"""Profiles built from what a bus was seen to do, not from anyone's documentation.

``core.changes`` finds the bytes and bits that follow what the technician did to the machine.
This turns the ones worth keeping into profile signals, with a name the technician picks and a
note of how each was found, and saves them as an ordinary profile in the user's profile folder,
so Live Data, the data log and the reports use them straight away.

* ``candidates(report)``: every unnamed field that changed between two phases, most marked first,
  with its evidence in words and, where it moves with a value we already know, a suggested scale.
* ``GENERIC_NAMES``: plain names to pick from (a handheld has no keyboard). They are ordinary
  words for what machines have. Nothing in an observed profile comes from a maker's data.
* ``signal_entry`` / ``build_profile`` / ``save_profile``: the profile document. Besides the usual
  keys it carries ``"observed"`` (when, from which capture, how) and each signal its own
  ``"observed"`` note (evidence, how sure). The profile loader ignores keys it doesn't know, so
  these travel with the file: they are what makes a shared profile worth trusting or checking.

Pure: no Qt, no Pygame.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field

from . import changes, profiles

# (group, name, unit) to pick from. Plain words, grouped as a technician thinks of a machine.
GENERIC_NAMES = [
    ("Engine", "Engine speed", "rpm"), ("Engine", "Coolant temperature", "°C"), ("Engine", "Oil pressure", "kPa"),
    ("Engine", "Oil temperature", "°C"), ("Engine", "Intake air temperature", "°C"),
    ("Engine", "Manifold pressure", "kPa"), ("Engine", "Throttle position", "%"), ("Engine", "Engine load", "%"),
    ("Engine", "Fuel level", "%"), ("Engine", "Engine running", ""), ("Engine", "Hour meter", "h"),
    ("Electrical", "Battery voltage", "V"), ("Electrical", "Battery current", "A"),
    ("Electrical", "State of charge", "%"), ("Electrical", "Key switch", ""), ("Electrical", "Charging", ""),
    ("Drive", "Accelerator pedal", "%"), ("Drive", "Vehicle speed", "km/h"), ("Drive", "Forward selected", ""),
    ("Drive", "Reverse selected", ""), ("Drive", "Neutral", ""), ("Drive", "Brake pedal", ""),
    ("Drive", "Parking brake", ""), ("Drive", "Steering angle", "°"), ("Drive", "Motor speed", "rpm"),
    ("Drive", "Motor current", "A"), ("Drive", "Motor temperature", "°C"), ("Drive", "Controller temperature", "°C"),
    ("Hydraulics", "Lift", ""), ("Hydraulics", "Lower", ""), ("Hydraulics", "Tilt", ""),
    ("Hydraulics", "Side shift", ""), ("Hydraulics", "Auxiliary function", ""), ("Hydraulics", "Lever position", "%"),
    ("Hydraulics", "Hydraulic pressure", "kPa"), ("Hydraulics", "Hydraulic oil temperature", "°C"),
    ("Hydraulics", "Pump speed", "rpm"), ("Hydraulics", "Mast height", "mm"),
    ("Operator", "Seat switch", ""), ("Operator", "Seat belt", ""), ("Operator", "Operator present", ""),
    ("Operator", "Horn", ""), ("Operator", "Lights", ""), ("Operator", "Warning lamp", ""),
    ("Other", "Switch", ""), ("Other", "Value", ""), ("Other", "Temperature", "°C"), ("Other", "Pressure", "kPa"),
    ("Other", "Speed", "rpm"), ("Other", "Position", "%"),
]
SURE = ("seen once", "seen again", "checked")       # how sure, in the order a signal earns them


@dataclass
class Candidate:
    """One field worth naming."""

    stream: changes.Stream
    field: changes.Field
    score: float
    evidence: list = field(default_factory=list)    # "Key on -> Idling: 0 -> 817 (was 0-194, now 194-1634)"
    follows: changes.Relation | None = None

    @property
    def key(self) -> str:
        return f"{self.stream.label} {self.field.where}"

    @property
    def is_switch(self) -> bool:
        return "." in self.field.where

    def suggestion(self) -> dict:
        """A name, unit and scale to offer: from the value it follows, when there is one."""
        if self.follows is not None and self.follows.other.known:
            k, c, unit = self.follows.formula()
            c = 0.0 if abs(c) < abs(k) else c       # less than one count: there is no offset
            return {"name": self.follows.other.known, "unit": unit, "scale": _round(k), "offset": _round(c, 2)}
        return {}

    def summary(self) -> str:
        known = self.follows.other.known if self.follows is not None else ""
        tail = f"  (follows {known})" if known else ""
        return f"{self.key}: {self.evidence[0] if self.evidence else ''}{tail}"


def _round(x: float, places: int = 4) -> float:
    """A fitted factor tidied up: 0.9998 -> 1, 0.1247 -> 0.125 when it's that close."""
    for nice in (1, 2, 4, 5, 8, 10, 16, 20, 32, 50, 64, 100, 128, 256, 1000):
        for value in (float(nice), 1.0 / nice):
            if abs(x - value) <= 0.012 * value:
                return value
            if abs(x + value) <= 0.012 * value:
                return -value
    return round(x, places) if abs(x) >= 0.005 else 0.0


def candidates(report: changes.Report, include_known: bool = False) -> list[Candidate]:
    """Fields that changed between neighbouring phases, the most marked first. Fields a decoder
    already names are left out unless ``include_known``."""
    found: dict[tuple, Candidate] = {}
    for i in range(len(report.phases) - 1):
        a, b = report.phases[i].label, report.phases[i + 1].label
        for c in report.changes(i, i + 1, limit=10_000):
            if c.field is None or (c.field.known and not include_known):
                continue
            key = (id(c.stream), c.field.where)
            cand = found.setdefault(key, Candidate(c.stream, c.field, 0.0))
            cand.score = max(cand.score, c.score)
            cand.evidence.append(f"{a} -> {b}: {c.text}")
    by_field = {(id(r.stream), r.field.where): r for r in report.relations()}
    for key, cand in found.items():
        cand.follows = by_field.get(key)
        if cand.follows is not None and cand.follows.other.known:
            cand.score += 25                        # we can say what it is: worth showing first
    return sorted(found.values(), key=lambda c: (-c.score, c.key))


def signal_entry(cand: Candidate, name: str, unit: str = "", scale: float = 1.0, offset: float = 0.0,
                 group: str = "Observed", sure: str = SURE[0]) -> dict:
    """The profile ``signals`` entry for a candidate the technician named."""
    s, where = cand.stream, cand.field.where
    entry: dict = {"name": name}
    if s.kind == "j1939":
        entry.update(pgn=f"0x{s.ident:04X}", sa=f"0x{s.sa:02X}")
    else:
        entry["id"] = f"0x{s.ident:03X}"
    m = re.fullmatch(r"w(\d+)(le|be)|b(\d+)(?:\.(\d))?", where)
    if not m:
        raise ValueError(f"not a field: {where!r}")
    if m.group(1) is not None:
        entry.update(start=int(m.group(1)), length=2)
        if m.group(2) == "be":
            entry["order"] = "big"
    elif m.group(4) is not None:
        entry.update(start=int(m.group(3)), bit=int(m.group(4)), bits=1, enum={"0": "Off", "1": "On"})
    else:
        entry.update(start=int(m.group(3)), length=1)
    if "bits" not in entry:
        entry.update(scale=scale, offset=offset, unit=unit, decimals=0 if scale >= 1 else 1, na_check=False)
    entry["group"] = group
    note = {"sure": sure, "evidence": list(cand.evidence[:4])}
    if cand.follows is not None:
        who = cand.follows.other.known or f"{cand.follows.other_stream.label} {cand.follows.other.where}"
        note["follows"] = who
    entry["observed"] = note
    return entry


def protocol_of(report: changes.Report) -> str:
    """Which protocol the capture's traffic is: J1939, CANopen or both."""
    j = any(s.kind == "j1939" for s in report.streams)
    c = any(s.kind == "11-bit" for s in report.streams)
    return profiles.J1939_CANOPEN if j and c else profiles.CANOPEN if c else profiles.J1939


def build_profile(name: str, entries: list[dict], protocol: str = profiles.J1939, source: str = "",
                  machine: dict | None = None, base: dict | None = None) -> dict:
    """A profile document. ``base`` is an observed profile to add to (same place replaces)."""
    doc = dict(base) if base else {"name": name, "extends": profiles.GENERIC[protocol], "protocol": protocol,
                                   "description": "Built on the machine from what its bus was seen to do.",
                                   "signals": []}
    place = lambda e: (e.get("pgn"), e.get("sa"), e.get("id"), e.get("start"), e.get("bit"), e.get("length"))  # noqa: E731
    taken = {place(e) for e in entries}
    doc["signals"] = [e for e in doc.get("signals", []) if place(e) not in taken] + list(entries)
    history = list((doc.get("observed") or {}).get("sessions", []))
    history.append({"when": time.strftime("%Y-%m-%d %H:%M"), "source": source, "signals": [e["name"] for e in entries]})
    doc["observed"] = {"method": "what changed between timestamps (core.changes)", "sessions": history,
                       **({"machine": machine} if machine else {})}
    return doc


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "observed"


def profile_path(name: str) -> str:
    return os.path.join(profiles.user_dir(), f"observed-{slug(name)}.json")


def load_doc(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and "observed" in doc else None


def save_profile(doc: dict, path: str | None = None) -> str:
    """Write it into the user's profile folder (checked by parsing it first). Returns the path."""
    path = path or profile_path(doc["name"])
    profiles.parse_profile(doc, os.path.splitext(os.path.basename(path))[0], path)     # raises ProfileError
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return path
