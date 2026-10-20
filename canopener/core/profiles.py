"""Machine profiles: per make/model labels for source addresses, SPNs, PGNs and custom signals.

A profile is a JSON file. Built-in profiles ship in ``canopener/profiles``; user profiles go
in ``~/.config/canopener/profiles``. Numbers may be written as decimal or "0x.." strings::

    {
      "name": "Genie GTH-1056 (Deutz TCD3.6)",
      "description": "...",
      "sources": {"0x27": "Machine controller"},
      "spns":    {"520205": "Rail pressure monitoring (OEM)"},
      "pgns":    {"0xFF10": "Boom status (proprietary)"},
      "signals": [
        {"name": "Boom angle raw", "pgn": "0xFF10", "sa": "0x27", "start": 1, "length": 2,
         "scale": 1, "offset": 0, "unit": "counts", "decimals": 0}

    Optional signal keys: "spn", "decimals", "sa" (only decode from that source) and
    "na_check" (default true for standard PGNs, false for proprietary ones).
      ]
    }

Applying a profile fills the override tables in ``canopener.j1939`` so every view (GUI,
CLI, reports, a future on-device UI) picks the labels up without knowing about profiles.
"""

import glob
import json
import os
from dataclasses import dataclass, field

from ..j1939 import addresses, constants, signals, spn
from ..j1939.signals import Signal
from . import config

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
    path: str = ""
    builtin: bool = False


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


def parse_profile(doc: dict, pid: str = "custom", path: str = "") -> Profile:
    if not isinstance(doc, dict) or "name" not in doc:
        raise ProfileError("profile must be a JSON object with a 'name'")
    sigs = []
    for i, s in enumerate(doc.get("signals", [])):
        try:
            length = int(s.get("length", 1))
            if length not in (1, 2, 4):
                raise ValueError("length must be 1, 2 or 4")
            sigs.append(Signal(
                name=str(s["name"]), pgn=_int(s["pgn"]), start=int(s["start"]), length=length,
                scale=float(s.get("scale", 1)), offset=float(s.get("offset", 0)),
                unit=str(s.get("unit", "")), spn=_int(s["spn"]) if "spn" in s else None,
                decimals=int(s.get("decimals", 1)), sa=_int(s["sa"]) if "sa" in s else None,
                # Proprietary payloads don't follow the J1939 'not available' convention.
                na_check=bool(s.get("na_check", not constants.is_proprietary(_int(s["pgn"])))),
            ))
        except (KeyError, ValueError, TypeError) as e:
            raise ProfileError(f"signal #{i + 1}: {e}") from None
    return Profile(
        id=pid, name=str(doc["name"]), description=str(doc.get("description", "")),
        sources=_num_map(doc.get("sources"), "sources"), spns=_num_map(doc.get("spns"), "spns"),
        pgns=_num_map(doc.get("pgns"), "pgns"), signals=sigs, path=path,
    )


def load_profile(path: str) -> Profile:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ProfileError(f"{os.path.basename(path)}: {e}") from None
    pid = os.path.splitext(os.path.basename(path))[0]
    p = parse_profile(doc, pid, path)
    p.builtin = os.path.dirname(os.path.abspath(path)) == os.path.abspath(BUILTIN_DIR)
    return p


def available_profiles() -> tuple[list[Profile], list[str]]:
    """All loadable profiles (user ones override built-ins with the same id) and any errors."""
    found: dict[str, Profile] = {}
    errors = []
    for d in (BUILTIN_DIR, user_dir()):
        for path in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                p = load_profile(path)
            except ProfileError as e:
                errors.append(str(e))
                continue
            found[p.id] = p
    return sorted(found.values(), key=lambda p: (p.id != "generic", p.name.lower())), errors


_active: Profile | None = None


def active() -> Profile | None:
    return _active


def apply(profile: Profile | None, *, spn_csv: str | None = None):
    """Make ``profile`` the active one (None = no profile). A user SPN CSV is layered on top."""
    global _active
    addresses.SA_OVERRIDES.clear()
    constants.PGN_OVERRIDES.clear()
    spn.SPN_OVERRIDES.clear()
    signals.PROFILE_SIGNALS.clear()
    _active = profile
    if profile:
        addresses.SA_OVERRIDES.update(profile.sources)
        constants.PGN_OVERRIDES.update(profile.pgns)
        spn.SPN_OVERRIDES.update(profile.spns)
        signals.PROFILE_SIGNALS.extend(profile.signals)
    spn.load_spn_csv(spn_csv if spn_csv is not None else config.spn_csv_path())


def find(pid: str) -> Profile | None:
    for p in available_profiles()[0]:
        if p.id == pid:
            return p
    return None
