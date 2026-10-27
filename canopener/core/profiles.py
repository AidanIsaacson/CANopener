"""Machine profiles: per make/model labels for source addresses, SPNs, PGNs and custom signals.

A profile is a JSON file. Built-in profiles ship in ``canopener/profiles``; user profiles go
in ``~/.config/canopener/profiles`` (that's also where generated / private profiles belong,
so OEM data never has to live in this repository). Numbers may be decimal or "0x.." strings::

    {
      "name": "Example loader",
      "description": "...",
      "protocol": "j1939",
      "sources": {"0x27": "Machine controller"},
      "spns":    {"520205": "OEM code description"},
      "pgns":    {"0xFF10": "Boom status (proprietary)"},
      "detect":  {"addresses": ["0x00", "0x27", "0xE4"], "min_match": 2},
      "signals": [
        {"name": "Boom angle raw", "pgn": "0xFF10", "sa": "0x27", "start": 1, "length": 2,
         "unit": "counts", "decimals": 0, "group": "Boom"},
        {"name": "Seat switch", "pgn": "0xFF10", "start": 4, "bit": 4, "bits": 1,
         "enum": {"0": "Open", "1": "Closed"}, "group": "Operator"}
      ]
    }

Signal keys: ``name``, ``pgn``, ``start`` (0-based byte) are required. Optional:

* ``length`` (bytes: 1, 2, 4) for whole-byte values, or ``bit`` (0 = LSB of ``start``) and
  ``bits`` (width) for bit fields, which may span bytes (J1939 / Intel order)
* ``scale`` (default 1), ``offset`` (default 0), ``unit``, ``decimals``, ``signed``
* ``spn``, ``group`` (Live Data grouping), ``enum`` (raw value -> text), ``low`` / ``high``
  (normal range; values outside it are highlighted)
* ``sa``: only decode from this source address, or a list of addresses
* ``na_check``: treat all-ones / J1939 error values as "not available" (default true for
  standard PGNs, false for proprietary ones)

``protocol`` is what the machine speaks: ``"j1939"`` (the default: fault codes, live data,
network, machine info) or ``"obd2"`` (a car / light truck: the OBD-II reader instead). Front-ends
show only the screens for the active profile's protocol.

``detect`` lets the app recognise a machine from the source addresses it sees on the bus and
offer to switch to the profile.

``extends`` (a profile id) builds on another profile: its sources, SPN / PGN names, signals,
detect section and protocol carry over, and this file only adds or replaces things. A signal here
replaces the base's signal at the same place (PGN, byte, bit, width). That's how labels you make
in the app (``core.labeling``) sit on top of a built-in profile without copying it, so the base
keeps getting updates.

Applying a profile fills the override tables in ``canopener.j1939`` so every view (GUI,
CLI, reports, a future on-device UI) picks the labels up without knowing about profiles.
"""

import glob
import json
import os
from dataclasses import dataclass, field

from ..j1939 import addresses, constants, signals, spn
from ..j1939.signals import Signal
from . import config, units

J1939, OBD2 = "j1939", "obd2"
PROTOCOLS = (J1939, OBD2)
BUILTIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "profiles")


def user_dir() -> str:
    return os.path.join(config.config_dir(), "profiles")


@dataclass
class Profile:
    id: str
    name: str
    description: str = ""
    sources: dict[int, str] = field(default_factory=dict)
    spns: dict[int, str] = field(default_factory=dict)
    pgns: dict[int, str] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    detect_addresses: frozenset[int] = frozenset()
    detect_min: int = 0
    path: str = ""
    builtin: bool = False
    protocol: str = J1939
    extends: str = ""           # id of the profile this one builds on ("" = standalone)


class ProfileError(ValueError):
    pass


def _int(v) -> int:
    if isinstance(v, int):
        return v
    return int(str(v).strip(), 0)


def _num_map(d: dict, what: str) -> dict[int, str]:
    try:
        return {_int(k): str(v) for k, v in (d or {}).items()}
    except ValueError as e:
        raise ProfileError(f"bad {what} key: {e}") from None


def _opt_float(v):
    return None if v is None else float(v)


def parse_signal(s: dict) -> Signal:
    pgn = _int(s["pgn"])
    bits = int(s.get("bits", 0))
    bit = int(s.get("bit", 0))
    length = int(s.get("length", 1))
    if bits:
        if not 1 <= bits <= 64 or not 0 <= bit <= 7:
            raise ValueError("bits must be 1-64 and bit 0-7")
        length = (bit + bits + 7) // 8
    elif length not in (1, 2, 4):
        raise ValueError("length must be 1, 2 or 4 (or use bit / bits)")
    sa = s.get("sa")
    if isinstance(sa, list):
        sa = tuple(_int(x) for x in sa) or None
    elif sa is not None:
        sa = _int(sa)
    enum = tuple(sorted((_int(k), str(v)) for k, v in (s.get("enum") or {}).items()))
    return Signal(
        name=str(s["name"]), pgn=pgn, start=int(s["start"]), length=length,
        scale=float(s.get("scale", 1)), offset=float(s.get("offset", 0)),
        unit=units.normalize_unit(str(s.get("unit") or "")),
        spn=_int(s["spn"]) if s.get("spn") is not None else None,
        decimals=int(s.get("decimals", 0 if (enum or bits == 1) else 1)), sa=sa,
        # Proprietary payloads don't follow the J1939 'not available' convention.
        na_check=bool(s.get("na_check", not constants.is_proprietary(pgn))),
        bit=bit, bits=bits, signed=bool(s.get("signed", False)), enum=enum,
        group=str(s.get("group", "")), low=_opt_float(s.get("low")), high=_opt_float(s.get("high")),
    )


def parse_profile(doc: dict, pid: str = "custom", path: str = "") -> Profile:
    if not isinstance(doc, dict) or "name" not in doc:
        raise ProfileError("profile must be a JSON object with a 'name'")
    sigs = []
    for i, s in enumerate(doc.get("signals", [])):
        try:
            sigs.append(parse_signal(s))
        except (KeyError, ValueError, TypeError) as e:
            raise ProfileError(f"signal #{i + 1} ({s.get('name', '?') if isinstance(s, dict) else '?'}): "
                               f"{e}") from None
    det = doc.get("detect") or {}
    try:
        det_addr = frozenset(_int(a) for a in det.get("addresses", []))
        det_min = int(det.get("min_match", min(2, len(det_addr))))
    except (ValueError, TypeError) as e:
        raise ProfileError(f"bad detect section: {e}") from None
    extends = str(doc.get("extends") or "").strip()
    # A profile that extends another inherits its protocol unless it names one ("" until resolved).
    protocol = str(doc.get("protocol", "" if extends else J1939)).lower()
    if protocol and protocol not in PROTOCOLS:
        raise ProfileError(f"protocol must be one of {', '.join(PROTOCOLS)}, not {protocol!r}")
    return Profile(
        id=pid, name=str(doc["name"]), description=str(doc.get("description", "")), protocol=protocol,
        sources=_num_map(doc.get("sources"), "sources"), spns=_num_map(doc.get("spns"), "spns"),
        pgns=_num_map(doc.get("pgns"), "pgns"), signals=sigs,
        detect_addresses=det_addr, detect_min=det_min, path=path, extends=extends,
    )


def merged(base: "Profile", own: "Profile") -> "Profile":
    """``own`` laid over ``base`` (see ``extends``); keeps ``own``'s id, name, path and origin."""
    taken = {s.location for s in own.signals}
    return Profile(
        id=own.id, name=own.name, description=own.description or base.description,
        sources={**base.sources, **own.sources}, spns={**base.spns, **own.spns},
        pgns={**base.pgns, **own.pgns},
        signals=[s for s in base.signals if s.location not in taken] + list(own.signals),
        detect_addresses=own.detect_addresses or base.detect_addresses,
        detect_min=own.detect_min if own.detect_addresses else base.detect_min,
        path=own.path, builtin=own.builtin, protocol=own.protocol or base.protocol, extends=own.extends,
    )


def _resolve(found: dict[str, "Profile"], errors: list[str]) -> dict[str, "Profile"]:
    """Apply every profile's ``extends`` (chains allowed). A missing base or a loop is reported
    and the profile loads on its own, so a deleted base never makes labels vanish."""
    out: dict[str, Profile] = {}

    def get(pid: str, chain: tuple = ()) -> Profile:
        if pid in out:
            return out[pid]
        p = found[pid]
        if p.extends:
            if p.extends in chain or p.extends == pid:
                errors.append(f"{os.path.basename(p.path)}: 'extends' loops back to {p.extends!r}")
            elif p.extends not in found:
                errors.append(f"{os.path.basename(p.path)}: extends {p.extends!r}, which isn't installed "
                              f"(loaded on its own)")
            else:
                p = merged(get(p.extends, chain + (pid,)), p)
        if not p.protocol:
            p.protocol = J1939
        out[pid] = p
        return p

    for pid in found:
        get(pid)
    return out


def load_profile(path: str) -> Profile:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ProfileError(f"{os.path.basename(path)}: {e}") from None
    pid = os.path.splitext(os.path.basename(path))[0]
    try:
        p = parse_profile(doc, pid, path)
    except ProfileError as e:
        raise ProfileError(f"{os.path.basename(path)}: {e}") from None
    p.builtin = os.path.dirname(os.path.abspath(path)) == os.path.abspath(BUILTIN_DIR)
    return p


def available_profiles() -> tuple[list[Profile], list[str]]:
    """All loadable profiles and any errors. Later folders win on the same id:
    built-in < plugin < user."""
    found: dict[str, Profile] = {}
    errors = []
    from .. import plugins  # plugins may ship their own (e.g. private) profiles
    for d in (BUILTIN_DIR, *plugins.extra_profile_dirs(), user_dir()):
        for path in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                p = load_profile(path)
            except ProfileError as e:
                errors.append(str(e))
                continue
            found[p.id] = p
    found = _resolve(found, errors)
    return sorted(found.values(), key=lambda p: (p.id != "generic", p.name.lower())), errors


@dataclass(frozen=True)
class Match:
    profile: Profile
    matched: int            # detect addresses seen
    total: int              # detect addresses listed
    signals: int = 0        # profile signals whose PGN is on the bus from an accepted address

    @property
    def score(self) -> float:
        return self.matched / self.total if self.total else 0.0

    def describe(self) -> str:
        sig = f", {self.signals} of its signals on the bus" if self.signals else ""
        return f"{self.profile.name} ({self.matched} of {self.total} expected modules seen{sig})"


def signal_coverage(profile: Profile, present: dict[int, set[int]]) -> int:
    """How many of the profile's signals have their PGN on the bus from an address they accept."""
    return sum(1 for sig in profile.signals if any(sig.accepts(sa) for sa in present.get(sig.pgn, ())))


def detect(seen_addresses, candidates: list[Profile], present: dict[int, set[int]] | None = None) -> list[Match]:
    """Profiles whose ``detect`` addresses are present on the bus, best match first.

    A profile matches when at least ``min_match`` of its addresses are seen. With ``present``
    (PGN -> source addresses seen), matches are ranked by how many of their signals are actually
    on the bus: real machines often have only some of a family's optional modules, and the
    decodable data tells families apart better than the module list does. Without it, ranking
    is by addresses matched, then by the fraction matched.
    """
    seen = set(seen_addresses)
    out = []
    for p in candidates:
        if not p.detect_addresses:
            continue
        n = len(p.detect_addresses & seen)
        if n >= max(1, p.detect_min):
            out.append(Match(p, n, len(p.detect_addresses), signal_coverage(p, present) if present else 0))
    out.sort(key=lambda m: (-m.signals, -m.matched, -m.score, m.profile.name.lower()))
    return out


def present_pgns(session) -> dict[int, set[int]]:
    """PGN -> source addresses, from a Session's latest messages (for ``detect``)."""
    with session.lock:
        keys = list(session.latest)
    out: dict[int, set[int]] = {}
    for pgn, sa in keys:
        out.setdefault(pgn, set()).add(sa)
    return out


_active: Profile | None = None


def active() -> Profile | None:
    return _active


def _replace(table: dict, new: dict):
    for k in [k for k in table if k not in new]:
        del table[k]
    table.update(new)


def apply(profile: Profile | None, *, spn_csv: str | None = None):
    """Make ``profile`` the active one (None = no profile). A user SPN CSV is layered on top."""
    global _active
    _active = profile
    signals.set_profile_signals(profile.signals if profile else [])
    # Replace in place without emptying first: other threads may be looking names up meanwhile.
    _replace(addresses.SA_OVERRIDES, profile.sources if profile else {})
    _replace(constants.PGN_OVERRIDES, profile.pgns if profile else {})
    _replace(spn.SPN_OVERRIDES, profile.spns if profile else {})
    spn.load_spn_csv(spn_csv if spn_csv is not None else config.spn_csv_path())


def protocol() -> str:
    """The active profile's protocol (J1939 with no profile)."""
    return _active.protocol if _active is not None else J1939


def find(pid: str) -> Profile | None:
    for p in available_profiles()[0]:
        if p.id == pid:
            return p
    return None


# --- which profiles the picker offers, in what order (Profiles > Manage profiles) ------------
# Kept in ~/.config/canopener/profile_list.json so every front-end shares it. Disabled profiles
# stay installed (a plugin's, say) but aren't offered or auto-detected.

def _list_path() -> str:
    return os.path.join(config.config_dir(), "profile_list.json")


def list_prefs() -> tuple[list[str], set[str]]:
    """(order, disabled) as saved: profile ids, missing file = defaults."""
    try:
        with open(_list_path(), encoding="utf-8") as f:
            doc = json.load(f)
        return [str(x) for x in doc.get("order", [])], {str(x) for x in doc.get("disabled", [])}
    except (OSError, ValueError, AttributeError):
        return [], set()


def save_list_prefs(order: list[str], disabled) -> None:
    config.ensure_dir(config.config_dir())
    with open(_list_path(), "w", encoding="utf-8") as f:
        json.dump({"order": list(order), "disabled": sorted(disabled)}, f, indent=2)


def arrange(found: list[Profile], include_disabled: bool = False) -> list[Profile]:
    """Profiles in the user's order (ones not ordered yet keep their default place, after), without
    the disabled ones unless asked."""
    order, disabled = list_prefs()
    rank = {pid: i for i, pid in enumerate(order)}
    out = sorted(found, key=lambda p: (rank.get(p.id, len(rank)), found.index(p)))
    return out if include_disabled else [p for p in out if p.id not in disabled]


def origin(p: Profile) -> str:
    """'built-in', 'plugin' or 'user', from where the file lives."""
    if p.builtin:
        return "built-in"
    from .. import plugins
    folder = os.path.dirname(os.path.abspath(p.path))
    if any(folder == os.path.abspath(d) for d in plugins.extra_profile_dirs()):
        return "plugin"
    return "user"
