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

from ..j1939.signals import all_signals, id_signals
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

    @property
    def known(self) -> str:
        return known_name(self.stream, self.field)

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


def known_name(stream: changes.Stream, f: changes.Field) -> str:
    """A decoder's name for a field, or for the value it is part of (a bit of a known temperature
    is still that temperature, not a switch; a byte of a known 16-bit number is that number).
    Bits are matched exactly: naming one bit of a status byte leaves its other bits unnamed.
    "" when unnamed."""
    if f.known:
        return f.known
    m = re.fullmatch(r"w(\d+)(?:le|be)|b(\d+)(?:\.(\d))?", f.where)
    if not m:
        return ""
    if stream.kind == "j1939":
        sigs = [g for g in all_signals() if g.can_id is None and g.pgn == stream.ident and g.accepts(stream.sa)]
    else:
        sigs = list(id_signals().get(stream.ident, ()))
    if m.group(1) is not None:
        mine = {int(m.group(1)), int(m.group(1)) + 1}
    else:
        mine = {int(m.group(2))}
    bit = None if m.group(3) is None else int(m.group(2)) * 8 + int(m.group(3))
    for g in sigs:
        if g.bits:
            lo = g.start * 8 + g.bit
            if bit is not None and lo <= bit < lo + g.bits:
                return g.name
            if bit is None and g.bits >= 8 and all(lo <= i * 8 and i * 8 + 8 <= lo + g.bits for i in mine):
                return g.name
        elif mine & set(range(g.start, g.start + g.length)):
            return g.name
    return ""


def candidates(report: changes.Report, include_known: bool = False) -> list[Candidate]:
    """Fields that changed between neighbouring phases, the most marked first. Fields a decoder
    already names (or bits of a named value) are left out unless ``include_known``."""
    found: dict[tuple, Candidate] = {}
    for i in range(len(report.phases) - 1):
        a, b = report.phases[i].label, report.phases[i + 1].label
        for c in report.changes(i, i + 1, limit=10_000):
            if c.field is None or (known_name(c.stream, c.field) and not include_known):
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
                  machine: dict | None = None, base: dict | None = None, extends: str = "",
                  bus: list | None = None) -> dict:
    """A profile document. ``base`` is a profile document to add to (same place replaces);
    otherwise a new one that ``extends`` the profile in use (the generic one for the protocol when
    none is given), so nothing that profile already decodes is lost. ``bus``: the streams heard
    (``bus_fingerprint``), kept so the profile can be recognised on another machine later."""
    doc = dict(base) if base else {"name": name, "extends": extends or profiles.GENERIC[protocol],
                                   "protocol": protocol,
                                   "description": "Built on the machine from what its bus was seen to do.",
                                   "signals": []}
    place = lambda e: (e.get("pgn"), e.get("sa"), e.get("id"), e.get("start"), e.get("bit"), e.get("length"))  # noqa: E731
    taken = {place(e) for e in entries}
    doc["signals"] = [e for e in doc.get("signals", []) if place(e) not in taken] + list(entries)
    old = doc.get("observed") or {}
    history = list(old.get("sessions", []))
    history.append({"when": time.strftime("%Y-%m-%d %H:%M"), "source": source, "signals": [e["name"] for e in entries]})
    doc["observed"] = {"method": "what changed between timestamps (core.changes)", "sessions": history,
                       **({"machine": machine or old.get("machine")} if machine or old.get("machine") else {}),
                       **({"bus": bus or old.get("bus")} if bus or old.get("bus") else {})}
    return doc


def bus_fingerprint(stream_keys) -> list:
    """The streams a bus carried, as plain JSON: [["j1939", "0xFF04", "0x27"], ["11-bit", "0x181", null]]."""
    out = set()
    for kind, ident, sa in stream_keys:
        out.add((kind, f"0x{ident:04X}" if kind == "j1939" else f"0x{ident:03X}",
                 None if sa is None else f"0x{sa:02X}"))
    return [list(x) for x in sorted(out, key=lambda x: (x[0], x[1], x[2] or ""))]


def _raw_doc(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


@dataclass
class Target:
    """Where 'Add to the profile in use' writes."""

    path: str
    base: dict              # the document to add to
    label: str              # for the menu: "Add to Hyster H80UT"
    note: str = ""          # a second line: "kept in its own file, on top of it"


def in_use_target(active: "profiles.Profile | None") -> Target | None:
    """'Add to the profile in use': your own profiles are added to directly; a built-in or plugin
    profile gets a profile of its own on top (``extends`` it), so updates to it still arrive and
    nothing of it is lost. None when there's nothing worth adding to (no profile, or a generic one:
    then it's a new profile)."""
    if active is None or active.id in profiles.GENERIC.values():
        return None
    if profiles.origin(active) == "user" and active.path:
        doc = _raw_doc(active.path)
        if doc is not None:
            return Target(active.path, doc, f"Add to {active.name}")
    path = profile_path(f"{active.id} additions")
    doc = _raw_doc(path) or {"name": f"{active.name} + observed", "extends": active.id,
                             "description": f"Values named on the machine, on top of {active.name}.",
                             "signals": []}
    return Target(path, doc, f"Add to {active.name}", "kept in a profile on top of it, so it still gets updates")


def save_entries(entries: list[dict], active: "profiles.Profile | None", *, new_name: str = "",
                 protocol: str = profiles.J1939, source: str = "", machine: dict | None = None,
                 bus: list | None = None) -> str:
    """Save named values: into the profile in use (``in_use_target``) when ``new_name`` is empty,
    else as a new profile of that name built on the profile in use. Returns the path written."""
    if not new_name:
        target = in_use_target(active)
        if target is not None:
            return save_profile(build_profile("", entries, protocol, source, machine, base=target.base, bus=bus),
                                target.path)
        new_name = f"Observed {time.strftime('%Y-%m-%d %H%M')}"
    name, n = new_name, 2
    while os.path.exists(profile_path(name)):
        name, n = f"{new_name} {n}", n + 1
    extends = active.id if active is not None else ""
    return save_profile(build_profile(name, entries, protocol, source, machine, extends=extends, bus=bus),
                        profile_path(name))


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
