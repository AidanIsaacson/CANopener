"""Auto-Connect: one button that listens first, connects, and says what the bus looks like.

Unless something was set by hand, connecting is the same every time: listen (listen-only, so the
machine is never disturbed) until steady traffic is heard, which finds the bitrate (and, with two
ports, the busier bus), connect at that, then compare the traffic with every profile on the unit.
This holds the parts both front-ends share:

* ``manual(settings, two_ports)``: what has been set by hand, in words ("250k", "listen-only"):
  then the button connects with those instead, and says so. ``reset`` puts them back to Auto.
* ``suggest_for(...)``: a few seconds into the connection, a known profile that fits the bus
  better than the one in use (``core.profile_match``), and the values other profiles know for the
  traffic that's still undecoded (``core.suggest``), relative to that profile when there is one.

No Qt, no Pygame.
"""

from dataclasses import dataclass, field

from . import profile_match, profiles, suggest
from .connection import SOCKETCAN

AUTO = {"can.source": SOCKETCAN, "can.bitrate": 0, "can.listen_only": False, "can.main_auto": True}


def manual(settings, two_ports: bool = False) -> list[str]:
    """What's been dialled in by hand (empty: Auto-Connect does it all)."""
    out = []
    if (settings.get("can.source") or SOCKETCAN) != SOCKETCAN:
        out.append("not the CAN adapter")
    bitrate = int(settings.get("can.bitrate") or 0)
    if bitrate:
        out.append(f"{bitrate // 1000}k")
    if settings.get("can.listen_only"):
        out.append("listen-only")
    if two_ports and not settings.get("can.main_auto"):
        out.append("main bus picked by hand")
    return out


def reset(settings):
    """Back to Auto-Connect: the bitrate found by listening, normal mode, the busier port."""
    for key, value in AUTO.items():
        settings.set(key, value)


@dataclass
class Suggestion:
    """What Auto-Connect found worth offering once connected."""

    profile: object = None                          # a profile_match.Match that fits better, or None
    borrowed: list = field(default_factory=list)    # suggest.Borrowed, relative to that profile

    @property
    def empty(self) -> bool:
        return self.profile is None and not self.borrowed

    @property
    def values(self) -> int:
        return sum(len(b.good) for b in self.borrowed)

    def entries(self) -> list[dict]:
        return [e for b in self.borrowed for e in b.entries()]

    def sources(self) -> list[str]:
        return sorted({b.profile_name for b in self.borrowed})

    def summary(self) -> str:
        bits = []
        if self.profile is not None:
            m = self.profile
            bits.append(f"it looks like {m.profile.name} ({m.matched} of {m.total} of its messages here)")
        if self.borrowed:
            bits.append(f"{self.values} more value(s) are known from {', '.join(self.sources())}")
        return "; ".join(bits)


def suggest_for(session, watch, candidates: list, active) -> Suggestion:
    """The profile to offer (when one fits clearly better than ``active``) and the values other
    profiles know beyond it. ``watch``: the connection's ``core.fieldwatch.FieldWatch``."""
    matches = profile_match.similar(candidates, profiles.present_pgns(session))
    best = profile_match.better_than_active(matches, active)
    if best is not None and best.share < profile_match.GOOD_SHARE:
        best = None
    base = best.profile if best is not None else active
    try:
        borrowed = suggest.from_watch(watch, candidates, base) if watch is not None else []
    except Exception:                               # a suggestion is a nicety: never break a connection
        borrowed = []
    return Suggestion(best, borrowed)
