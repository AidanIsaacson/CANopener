"""Which known profile does this bus look like?

``profiles.detect`` recognises a machine from the module addresses a profile lists in its
``detect`` section. Most profiles made on a machine (by hand, by the Profile builder or with the
bus explorer) have no such section, but they do say which messages they decode. Those messages
are the machine's fingerprint: a maker's proprietary messages (J1939 PGNs 0xFF00-0xFFFF and
Proprietary A, CANopen / plain 11-bit IDs) are rarely the same on two different machines, while
standard ones (engine speed, coolant temperature) are on nearly every engine and say nothing.

``similar(profiles, present)`` ranks the profiles by how many of their own messages are on the
bus right now. A profile saved by the builder also carries the streams heard when it was made
(``"observed": {"bus": [...]}``), which counts too. No Qt, no Pygame.
"""

import json
from dataclasses import dataclass

from ..j1939 import constants
from . import profiles

MIN_MATCHED = 2         # messages a profile must have on the bus to be offered at all
MIN_SHARE = 0.6         # and this share of its messages
GOOD_SHARE = 0.8        # this or better is worth saying out loud (a toast on connect)


@dataclass(frozen=True)
class Similar:
    profile: profiles.Profile
    matched: int        # its messages that are on the bus
    total: int          # its messages that tell machines apart

    @property
    def share(self) -> float:
        return self.matched / self.total if self.total else 0.0

    def describe(self) -> str:
        return f"{self.profile.name}: {self.matched} of its {self.total} messages are on this bus"


def _telling(pgn: int) -> bool:
    return constants.is_proprietary(pgn)


def _observed_bus(p: profiles.Profile) -> set:
    """The streams a builder-made profile recorded when it was saved."""
    if not p.path:
        return set()
    try:
        with open(p.path, encoding="utf-8") as f:
            doc = json.load(f)
        rows = ((doc.get("observed") or {}).get("bus")) or []
        out = set()
        for kind, ident, sa in rows:
            ident = int(str(ident), 0)
            if kind == "j1939" and _telling(ident):
                out.add((ident, None if sa is None else int(str(sa), 0)))
            elif kind != "j1939":
                out.add(("id", ident))
        return out
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def profile_streams(p: profiles.Profile) -> set:
    """The messages a profile decodes that tell machines apart: (pgn, sa or None), ("id", can_id)."""
    out = set()
    for sig in p.signals:
        if sig.can_id is not None:
            out.add(("id", sig.can_id))
        elif _telling(sig.pgn):
            sa = sig.sa if isinstance(sig.sa, int) else None
            out.add((sig.pgn, sa))
    return out | _observed_bus(p)


def _on_bus(stream, present: dict) -> bool:
    if stream[0] == "id":
        return stream in present
    pgn, sa = stream
    have = present.get(pgn)
    return bool(have) and (sa is None or sa in have)


def similar(candidates: list, present: dict) -> list[Similar]:
    """Profiles whose own messages are on the bus (``present`` as ``profiles.present_pgns`` makes
    it), best first. The generic profiles are left out: they fit everything."""
    generic = set(profiles.GENERIC.values())
    out = []
    for p in candidates:
        if p.id in generic:
            continue
        mine = profile_streams(p)
        if len(mine) < MIN_MATCHED:
            continue
        n = sum(1 for s in mine if _on_bus(s, present))
        if n >= MIN_MATCHED and n / len(mine) >= MIN_SHARE:
            out.append(Similar(p, n, len(mine)))
    out.sort(key=lambda m: (-m.share, -m.matched, m.profile.name.lower()))
    return out


def better_than_active(matches: list[Similar], active: profiles.Profile | None) -> Similar | None:
    """The best match, when it isn't the profile in use (or one built on it) and fits at least as
    well as the profile in use does."""
    if not matches:
        return None
    best = matches[0]
    if active is not None and (best.profile.id == active.id or active.extends == best.profile.id):
        return None
    mine = next((m for m in matches if active is not None and m.profile.id == active.id), None)
    if mine is not None and mine.share >= best.share:
        return None
    return best
