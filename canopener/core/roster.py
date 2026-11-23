"""Which modules are on the bus, and which ones the machine should have but doesn't.

Many controllers report a lost module only as "communication error", without saying which one:
when a CAN control handle goes dark, two or three other modules complain and none names the
handle. A profile knows the machine's modules (its ``expect`` list, or else its ``detect`` list:
what **Remember these modules** / **Recognise this machine** save on a good machine), so comparing
that with what's heard names the one that's gone.

``check(session, profile)`` gives one ``Module`` row per expected module and per module heard:

* **on the bus**: heard from recently;
* **went quiet**: it was sending regularly and stopped (``quiet_after``): a module that dropped
  out while connected;
* **not heard**: listed by the profile but nothing from it since connecting. For the first
  ``SETTLE`` seconds of traffic it's **waiting** instead, so a slow heartbeat isn't flagged at once.

Only frames a module sends itself count: a J1939 source address, or a CANopen node's heartbeat,
its own PDOs (TPDO), EMCY or SDO answers. A master keeps sending to a module that's gone (RPDOs,
SDO requests), so messages *to* a node prove nothing; the row says when they're on the bus
anyway. When nothing at all has been heard for a while the whole bus is quiet (key off, a
connector off), and that's what the summary says instead of blaming every module.

``Alerts`` turns successive checks into one-off messages for a toast or the status bar ("Node 60
Tiller head went quiet", "... is back"), and ``remember`` saves the modules heard as the machine's.

Read-only on the bus: it looks at what the ``Session`` heard. No Qt, no Pygame.
"""

import time
from dataclasses import dataclass

from ..canopen import cob
from . import canopen, labeling, profiles

ON, QUIET, MISSING, WAITING = "on", "quiet", "missing", "waiting"
QUIET_MIN = 2.0         # seconds: nothing for this long can count as "went quiet" (as capture_report)
QUIET_FACTOR = 4        # ... and this many of the module's usual gaps between frames
SETTLE = 3.0            # seconds of traffic before "not heard" is said
REGULAR = 3             # frames needed before a module has a rhythm to miss

FROM_NODE = frozenset({"tpdo1", "tpdo2", "tpdo3", "tpdo4", "heartbeat", "emcy", "sdo_tx"})
TO_NODE = frozenset({"rpdo1", "rpdo2", "rpdo3", "rpdo4", "sdo_rx"})


@dataclass(frozen=True)
class Module:
    src: int                    # core.canopen source int: J1939 address or canopen.node_src(node)
    name: str
    expected: bool              # the profile lists it
    status: str                 # ON, QUIET, MISSING, or WAITING (expected, not heard yet, still listening)
    frames: int = 0
    last_seen: float | None = None
    age: float | None = None    # seconds since it was last heard
    period: float | None = None  # its usual gap between frames, once it has a rhythm
    addressed: bool = False     # messages *to* it are on the bus (CANopen RPDO / SDO requests)
    unlisted: bool = False      # heard, but the profile's list (it has one) leaves it out

    @property
    def problem(self) -> bool:
        return self.status in (QUIET, MISSING)

    @property
    def node(self) -> int | None:
        return self.src - canopen.NODE_SRC if canopen.is_node(self.src) else None

    @property
    def text(self) -> str:
        """One line for the row: 'On the bus', 'Went quiet 12 s ago (it sent every 0.1 s)' ..."""
        to_it = "; messages to it are still on the bus" if self.addressed else ""
        if self.status == WAITING:
            return "Not heard yet (listening)"
        if self.status == MISSING:
            return "Not heard since connecting" + (" (messages to it are on the bus, nothing from it)"
                                                   if self.addressed else "")
        if self.status == QUIET:
            every = f" (it sent every {_secs(self.period)})" if self.period else ""
            return f"Went quiet {_secs(self.age)} ago{every}{to_it}"
        return "On the bus" + (" (not in the profile's list)" if self.unlisted else "")


def _secs(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v * 1000:.0f} ms" if v < 1 else f"{v:.1f} s" if v < 10 else f"{v:.0f} s"


@dataclass(frozen=True)
class Roster:
    modules: list[Module]       # problems first (quiet, then missing), then by address / node
    has_list: bool              # the profile lists the machine's modules
    listening: bool = False     # not heard long enough yet to say what's missing
    bus_quiet: bool = False     # nothing at all heard for a while
    quiet_for: float = 0.0      # ... for this long (0 = nothing heard yet)

    @property
    def expected(self) -> list[Module]:
        return [m for m in self.modules if m.expected]

    @property
    def problems(self) -> list[Module]:
        return [] if self.bus_quiet else [m for m in self.modules if m.problem]

    def summary(self) -> str:
        """One line: '3 of 4 expected modules on the bus. Not heard: Node 60 Tiller head.'"""
        if self.bus_quiet:
            return ("Nothing heard on the bus yet." if not self.quiet_for else
                    f"Nothing heard on the bus for {_secs(self.quiet_for)}: is the machine on?")
        exp = self.expected
        parts = []
        if exp:
            on = sum(1 for m in exp if m.status == ON)
            parts.append(f"{on} of {len(exp)} expected modules on the bus.")
        else:
            on = sum(1 for m in self.modules if m.status == ON)
            parts.append(f"{on} module(s) on the bus.")
        quiet = [m.name for m in self.modules if m.status == QUIET]
        missing = [m.name for m in self.modules if m.status == MISSING]
        if quiet:
            parts.append("Went quiet: " + ", ".join(quiet) + ".")
        if missing:
            parts.append("Not heard: " + ", ".join(missing) + ".")
        elif any(m.status == WAITING for m in self.modules):
            parts.append("Still listening...")
        return " ".join(parts)

    def hint(self, remember: str = "Remember these modules") -> str:
        """What to do next, or ''. ``remember`` is what the front-end calls saving the list."""
        if self.bus_quiet:
            return ""
        if not self.has_list:
            return (f"This profile doesn't list the machine's modules. On a machine that works, use {remember} "
                    "to save the ones heard; next time, any that's missing is named here.")
        if any(m.status == MISSING for m in self.modules):
            return ("A module that only answers when asked can look missing: ask for address claims (J1939) "
                    "or find CANopen nodes. Check a missing one's power, ground and CAN wiring.")
        return ""


def expected_modules(profile: profiles.Profile | None, proto: str | None = None) -> list[int]:
    """The modules a profile says the machine has, as source ints (J1939 addresses, then CANopen
    nodes as ``node_src``), for the protocols it speaks."""
    if profile is None:
        return []
    proto = proto or profile.protocol
    out = []
    if profiles.speaks_j1939(proto):
        out += sorted(profile.expect_addresses)
    if profiles.speaks_canopen(proto):
        out += [canopen.node_src(n) for n in sorted(profile.expect_nodes)]
    return out


@dataclass
class _Heard:
    frames: int = 0
    first: float = 0.0
    last: float = 0.0
    addressed: bool = False

    def add(self, count: int, first: float, last: float):
        self.first = first if not self.frames else min(self.first, first)
        self.last = max(self.last, last)
        self.frames += count


def _heard(session, j1939: bool, co: bool) -> tuple[dict[int, _Heard], dict[int, _Heard], set[int], float, float]:
    """(J1939 address -> heard, CANopen node -> heard, nodes the sniffer has confirmed as CANopen,
    times of the oldest and newest frame of any kind, 0 if none)."""
    addrs: dict[int, _Heard] = {}
    nodes: dict[int, _Heard] = {}
    oldest = newest = 0.0
    with session.lock:
        ecus = [(e.sa, e.frames, e.first_seen, e.last_seen) for e in session.ecus.values()]
        std = [(cid, st.count, st.first, st.last) for cid, st in session.std_stats.items()]
        confirmed = set(session.canopen.nodes)
    for sa, n, first, last in ecus:
        oldest, newest = min(oldest or first, first), max(newest, last)
        if j1939 and sa < 0xF8:
            addrs.setdefault(sa, _Heard()).add(n, first, last)
    for cid, n, first, last in std:
        oldest, newest = min(oldest or first, first), max(newest, last)
        if not co:
            continue
        kind, node = cob.classify(cid)
        if kind in FROM_NODE:
            nodes.setdefault(node, _Heard()).add(n, first, last)
        elif kind in TO_NODE:
            nodes.setdefault(node, _Heard()).addressed = True
    return addrs, nodes, confirmed, oldest, newest


def heard_modules(session, proto: str | None = None) -> tuple[list[int], list[int]]:
    """(J1939 addresses, CANopen nodes) heard sending, for **Recognise this machine**. A node
    counts when it sent anything of its own (heartbeat, TPDO, EMCY, SDO answer): many controllers
    send no heartbeat."""
    proto = proto or profiles.protocol()
    addrs, nodes, *_ = _heard(session, profiles.speaks_j1939(proto), profiles.speaks_canopen(proto))
    return sorted(addrs), sorted(n for n, h in nodes.items() if h.frames)


def quiet_after(period: float | None) -> float:
    """How long a module can be silent before it 'went quiet'."""
    return max(QUIET_MIN, QUIET_FACTOR * period) if period else QUIET_MIN


def check(session, profile: profiles.Profile | None = None, now: float | None = None) -> Roster:
    """Every module the profile expects and every module heard, with what's wrong (if anything).
    ``profile`` defaults to the active one. A decoded log (``session.offline``) is judged at its
    last frame unless ``now`` says otherwise."""
    profile = profile if profile is not None else profiles.active()
    proto = profile.protocol if profile is not None else profiles.protocol()
    j1939, co = profiles.speaks_j1939(proto), profiles.speaks_canopen(proto)
    addrs, nodes, confirmed, oldest, newest = _heard(session, j1939, co)
    if now is None:
        now = newest if session.offline else time.time()
    heard: dict[int, _Heard] = dict(addrs)
    heard.update({canopen.node_src(n): h for n, h in nodes.items()})
    expected = expected_modules(profile, proto)
    # A node seen only on PDO-range IDs (plain 11-bit traffic can sit there) is listed when the
    # profile expects it; otherwise only once its heartbeat or an SDO answer shows it's CANopen.
    srcs = list(dict.fromkeys(expected + [s for s, h in heard.items() if h.frames and (
        not canopen.is_node(s) or s - canopen.NODE_SRC in confirmed)]))

    bus_quiet = newest == 0.0 or now - newest > QUIET_MIN
    listening = oldest == 0.0 or now - oldest < SETTLE        # from the first frame of any kind
    want = set(expected)
    rows = []
    for src in srcs:
        h = heard.get(src)
        name = canopen.source_name(src)
        if h is None or not h.frames:
            rows.append(Module(src, name, True, WAITING if listening else MISSING,
                               addressed=bool(h and h.addressed)))
            continue
        period = (h.last - h.first) / (h.frames - 1) if h.frames >= REGULAR and h.last > h.first else None
        age = max(0.0, now - h.last)
        status = QUIET if period and age > quiet_after(period) else ON
        rows.append(Module(src, name, src in want, status, h.frames, h.last, age, period, h.addressed,
                           bool(want) and src not in want))
    order = {QUIET: 0, MISSING: 1, WAITING: 2, ON: 3}
    rows.sort(key=lambda m: (order[m.status], m.src >= canopen.NODE_SRC, m.src))
    return Roster(rows, bool(want), listening, bus_quiet, (now - newest) if newest else 0.0)


class Alerts:
    """Says each change once while connected: a module that went quiet, the expected ones not heard
    (once, when listening is over) and a module that came back. When the whole bus goes quiet the
    link lamps already say so: nothing here, and no 'back' for every module when it returns."""

    def __init__(self):
        self._said: dict[int, str] = {}         # src -> the problem status already reported

    def update(self, r: Roster) -> tuple[list[str], bool]:
        """(messages, whether one of them is a new problem) since the last update."""
        if r.bus_quiet:
            self._said = {src: st for src, st in self._said.items() if st != QUIET}
            return [], False
        out, bad = [], False
        quiet = [m for m in r.problems if m.status == QUIET and self._said.get(m.src) != QUIET]
        missing = [m for m in r.problems if m.status == MISSING and self._said.get(m.src) != MISSING]
        for m in quiet:
            out.append(f"{m.name} went quiet")
        if missing:
            out.append("Not heard: " + ", ".join(m.name for m in missing))
        for m in quiet + missing:
            self._said[m.src] = m.status
            bad = True
        for m in r.modules:
            if m.status == ON and m.src in self._said:
                del self._said[m.src]
                out.append(f"{m.name} is back")
        return out, bad


def remember(session, active: profiles.Profile | None, new_name: str = "") -> tuple[str, list[int]]:
    """Save the modules heard as the machine's (the profile's ``detect`` list): into ``active`` if
    it's your own profile, else into a new profile built on it (``new_name``, or 'My <name>').
    Returns (profile id, what was saved as source ints). Writes only the profile file."""
    addrs, nodes = heard_modules(session, active.protocol if active is not None else None)
    if labeling.is_editable(active):
        pid = active.id
    else:
        pid = labeling.create(new_name or (f"My {active.name}" if active is not None else "My machine"), active)
    return pid, labeling.set_detect(pid, addrs, nodes)
