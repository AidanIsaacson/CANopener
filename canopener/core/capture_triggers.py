"""Timestamps the bus sets: a fault code appearing, or a button you picked being pressed.

Chasing an intermittent fault means driving the machine with both hands busy. These add a
timestamp to the running log by themselves, each named after what set it:

* ``FAULT``: a fault code goes active: a CANopen EMCY with a new code from a node, or a new SPN /
  FMI in a J1939 DM1 (single-frame) from an address (codes already on in its first DM1 aren't
  news). The note says which code, from where.
* ``SIGNAL``: a value from the profile in use (or the standard tables) reaches a value: a button
  turns on (the default: from 0 to anything else), or an enum reaches the state you picked. Map a
  spare button on the machine (a reach truck's function button, say) and press it when the fault
  happens.

``Watch(triggers)`` is fed every received frame (``feed``, from the session's receive thread)
and queues what fired; the screen calls ``take()`` and adds the timestamps (``apply``), so the
capture is only ever changed from the UI thread. One trigger fires at most once per ``HOLDOFF``
seconds, so a bouncing contact or a code that flickers doesn't bury the log.

The triggers live in the shared settings (``log.triggers``: a list of ``Trigger.to_dict()``).
Only listens: nothing is sent. No Qt, no Pygame.
"""

import threading
from dataclasses import dataclass

from ..canopen import emcy as canopen_emcy
from ..j1939 import signals as sigtable
from ..j1939.dtc import decode_dm
from ..j1939.ids import parse_id

FAULT, SIGNAL = "fault", "signal"
HOLDOFF = 2.0               # seconds before the same trigger can set another timestamp
DM1 = 0xFECA
SETTING = "log.triggers"


@dataclass(frozen=True)
class Trigger:
    kind: str                       # FAULT or SIGNAL
    signal: str = ""                # SIGNAL: the value's name, as Live shows it
    value: float | None = None      # SIGNAL: the value that sets it (None: turns on, i.e. leaves 0)

    @property
    def label(self) -> str:
        if self.kind == FAULT:
            return "A fault code appears"
        return f"{self.signal} turns on" if self.value is None else f"{self.signal} = {self.value:g}"

    def to_dict(self) -> dict:
        d = {"kind": self.kind}
        if self.kind == SIGNAL:
            d["signal"] = self.signal
            if self.value is not None:
                d["value"] = self.value
        return d

    @classmethod
    def from_dict(cls, d) -> "Trigger | None":
        if not isinstance(d, dict) or d.get("kind") not in (FAULT, SIGNAL):
            return None
        if d["kind"] == SIGNAL and not d.get("signal"):
            return None
        value = d.get("value")
        return cls(d["kind"], str(d.get("signal") or ""), None if value is None else float(value))


def load(settings) -> list[Trigger]:
    out = [Trigger.from_dict(d) for d in (settings.get(SETTING) or [])]
    return [t for t in out if t is not None]


def save(settings, triggers: list[Trigger]):
    settings.set(SETTING, [t.to_dict() for t in triggers])


def candidates(signals=None) -> list[sigtable.Signal]:
    """Values a button trigger can watch: switches and states (bit fields and enums), from the
    profile in use first, then the standard tables. One per name."""
    pool = list(signals) if signals is not None else list(reversed(sigtable.all_signals()))
    seen, out = set(), []
    for s in pool:
        if s.name in seen or s.is_text or not (s.enum or 0 < s.bits <= 4):
            continue
        seen.add(s.name)
        out.append(s)
    return out


class Watch:
    """The triggers, watching a bus. ``feed`` from the receive thread, ``take`` from the UI."""

    def __init__(self, triggers: list[Trigger], signals=None, holdoff: float = HOLDOFF):
        self.triggers = list(triggers)
        self.holdoff = holdoff
        pool = list(signals) if signals is not None else list(reversed(sigtable.all_signals()))
        self._sigs = {}                 # trigger -> Signal
        for t in self.triggers:
            if t.kind == SIGNAL:
                s = next((s for s in pool if s.name == t.signal), None)
                if s is not None:
                    self._sigs[t] = s
        self.missing = [t for t in self.triggers if t.kind == SIGNAL and t not in self._sigs]
        self._fault = any(t.kind == FAULT for t in self.triggers)
        self._lock = threading.Lock()
        self._events: list = []         # (t, label, note)
        self._last: dict = {}           # trigger -> time it last fired
        self._state: dict = {}          # trigger -> was it set at the last frame
        self._emcy: dict = {}           # node -> active code
        self._dm1: dict = {}            # sa -> {(spn, fmi)}

    def feed(self, frame):
        if getattr(frame, "error", None) or getattr(frame, "tx", False):
            return
        data, t = bytes(frame.data), frame.timestamp
        if self._fault:
            self._faults(frame, data, t)
        for trig, s in self._sigs.items():
            if s.can_id is not None:
                if frame.extended or frame.can_id != s.can_id:
                    continue
            else:
                if not frame.extended:
                    continue
                j = parse_id(frame.can_id)
                if j.pgn != s.pgn or not s.accepts(j.sa):
                    continue
            v = s.decode(data)
            if v is None or isinstance(v, str):
                continue
            on = v != 0 if trig.value is None else abs(v - trig.value) < 1e-9
            was = self._state.get(trig, on)         # the first frame only sets the state: no edge yet
            self._state[trig] = on
            if on and not was:
                text = s.text(v)
                self._fire(trig, t, f"{s.name} pressed" if trig.value is None and s.bits == 1 else
                           f"{s.name}: {text or f'{v:g}'}",
                           f"Set by the bus: {s.name} went to {text or f'{v:g}'} ({trig.label})")

    def _faults(self, frame, data, t):
        trig = next(x for x in self.triggers if x.kind == FAULT)
        if frame.extended:
            j = parse_id(frame.can_id)
            if j.pgn != DM1 or len(data) < 6:
                return
            _lamps, dtcs = decode_dm(data)
            now = {(d.spn, d.fmi) for d in dtcs if d.spn}
            if j.sa not in self._dm1:               # the first DM1 from it: what was already on isn't news
                self._dm1[j.sa] = now
                return
            new = now - self._dm1[j.sa]
            self._dm1[j.sa] = now
            for spn, fmi in sorted(new):
                self._fire(trig, t, f"Fault: SPN {spn} FMI {fmi}",
                           f"Set by the bus: DM1 from {j.sa:02X} went active with SPN {spn} FMI {fmi}", force=True)
            return
        if not 0x081 <= frame.can_id <= 0x0FF or len(data) != 8:
            return
        e = canopen_emcy.decode(data)
        if e is None:
            return
        node = frame.can_id - 0x80
        if e.reset:
            self._emcy.pop(node, None)
        elif self._emcy.get(node) != e.code:
            self._emcy[node] = e.code
            self._fire(trig, t, f"Fault: EMCY {e.code:04X} node {node:02X}",
                       f"Set by the bus: node {node:02X} raised EMCY {e.code:04X} ({canopen_emcy.describe(e.code)})",
                       force=True)

    def _fire(self, trig, t, label, note, force=False):
        """Queue a timestamp. A new fault code always gets one; the same trigger otherwise waits
        out the hold-off."""
        with self._lock:
            last = self._last.get(trig)
            if not force and last is not None and t - last < self.holdoff:
                return
            if force and last is not None and t - last < 0.05:
                return                              # (one burst of new codes: one timestamp)
            self._last[trig] = t
            self._events.append((t, label, note))

    def take(self) -> list[tuple[float, str, str]]:
        with self._lock:
            out, self._events = self._events, []
        return out


def apply(capture, events) -> list[int]:
    """Add each fired trigger as a timestamp in a recording capture, named after what set it.
    Returns the new markers' indexes."""
    out = []
    for t, label, note in events:
        i = capture.add_marker(label, t)
        capture.set_note(i, note)
        out.append(i)
    return out
