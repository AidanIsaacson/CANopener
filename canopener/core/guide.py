"""The guided Profile Builder: it tells the technician what to do, watches the bus while they do
it, and names what followed.

A ``Guide`` walks a script of steps ("Start the engine", "Raise the forks", "Press the horn").
For each one it wants the machine left alone for a moment (the *rest*), then the thing done and
held (the *do*). It keeps the frames of both, compares them with ``core.changes`` and offers what
followed under the step's own name. Things that change by themselves are learnt in a hands-off
stretch at the start (and whenever a step is skipped untouched) and are never offered. Doing a
step a second time and seeing the same field again marks it "seen again".

It only listens: nothing here transmits. It is a state machine fed by ``feed(frame)`` (from the
session's receive thread) and ``tick(now)`` (from the screen); the screen shows ``prompt()`` and
calls ``begin`` / ``again`` / ``skip`` / ``keep``. ``entries()`` are the profile signals to save
(``core.observed``). No Qt, no Pygame.
"""

import threading
import time
from dataclasses import dataclass, field

from . import changes, observed

REST_SECONDS = 3.0          # hands off before each step
DO_SECONDS = 4.0            # do it and hold it
QUIET_SECONDS = 6.0         # the hands-off stretch at the very start
OFFER = 3                   # candidates shown per step
WAITING, QUIET, REST, DO, LOOK, RESULT, DONE = "waiting", "quiet", "rest", "do", "look", "result", "done"


@dataclass
class Step:
    name: str               # the name its signal gets: "Lift"
    prompt: str             # what to do: "Raise the forks and hold"
    group: str = "Observed"
    unit: str = ""
    switch: bool = True     # expect an on / off (else a value that moves)
    caution: str = ""       # said first when the step moves the machine


def _s(name, prompt, group, unit="", switch=True, caution=""):
    return Step(name, prompt, group, unit, switch, caution)


MOVES = "The machine will move: clear the area, and only do it if it's safe."
SCRIPTS = {
    "Engine truck": [
        _s("Engine running", "Start the engine and let it idle", "Engine"),
        _s("Engine speed", "Hold the revs up, about half throttle", "Engine", "rpm", switch=False),
        _s("Accelerator pedal", "Press the accelerator pedal fully (engine off is fine)", "Drive", "%", switch=False),
        _s("Brake pedal", "Press and hold the brake pedal", "Drive"),
        _s("Parking brake", "Release the parking brake (then set it again)", "Drive"),
        _s("Seat switch", "Stand up off the seat", "Operator"),
        _s("Forward selected", "Select forward (brake held)", "Drive", caution=MOVES),
        _s("Reverse selected", "Select reverse (brake held)", "Drive", caution=MOVES),
        _s("Lift", "Raise the forks and hold the lever", "Hydraulics", caution=MOVES),
        _s("Lower", "Lower the forks and hold the lever", "Hydraulics", caution=MOVES),
        _s("Tilt", "Tilt back and hold the lever", "Hydraulics", caution=MOVES),
        _s("Side shift", "Side shift and hold the lever", "Hydraulics", caution=MOVES),
        _s("Horn", "Press and hold the horn", "Operator"),
        _s("Lights", "Switch the lights on", "Operator"),
    ],
    "Electric truck": [
        _s("Key switch", "Turn the key on (from off)", "Electrical"),
        _s("Seat switch", "Stand up off the seat (or release the operator-present control)", "Operator"),
        _s("Accelerator pedal", "Press the accelerator / twist the control fully, drive wheel off the floor", "Drive",
           "%", switch=False, caution=MOVES),
        _s("Forward selected", "Select forward", "Drive", caution=MOVES),
        _s("Reverse selected", "Select reverse", "Drive", caution=MOVES),
        _s("Brake pedal", "Press and hold the brake", "Drive"),
        _s("Lift", "Raise the forks and hold", "Hydraulics", caution=MOVES),
        _s("Lower", "Lower the forks and hold", "Hydraulics", caution=MOVES),
        _s("Tilt", "Tilt and hold", "Hydraulics", caution=MOVES),
        _s("Side shift", "Side shift and hold", "Hydraulics", caution=MOVES),
        _s("Horn", "Press and hold the horn", "Operator"),
        _s("Charging", "Plug the charger in", "Electrical"),
    ],
    "Something else": [
        _s("Switch", "Work one switch or control and hold it", "Other"),
        _s("Value", "Move one lever, pedal or dial fully and hold it", "Other", switch=False),
    ],
}


@dataclass
class Offer:
    cand: observed.Candidate
    keep: bool = False
    again: bool = False     # the same field followed this step before

    @property
    def text(self) -> str:
        c = self.cand
        return c.evidence[0].split(": ", 1)[-1] if c.evidence else ""


@dataclass
class Guide:
    steps: list
    rest: float = REST_SECONDS
    do: float = DO_SECONDS
    quiet: float = QUIET_SECONDS
    index: int = 0
    state: str = WAITING
    offers: list = field(default_factory=list)      # the current step's
    kept: dict = field(default_factory=dict)        # field key -> profile entry
    restless: set = field(default_factory=set)      # field keys that change by themselves
    seen: dict = field(default_factory=dict)        # (step name, field key) -> times seen
    marks: list = field(default_factory=list)       # [(time, label)] for the log's timestamps
    message: str = ""
    frames_seen: int = 0

    def __post_init__(self):
        self._lock = threading.Lock()
        self._frames: list = []
        self._t = [0.0, 0.0, 0.0]       # rest start, do start, do end
        self._learnt = False

    # --- what the screen shows ---
    @property
    def step(self) -> Step | None:
        return self.steps[self.index] if 0 <= self.index < len(self.steps) else None

    def remaining(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        end = {QUIET: self._t[0] + self.quiet, REST: self._t[1], DO: self._t[2]}.get(self.state)
        return max(0.0, end - now) if end else 0.0

    def prompt(self) -> tuple[str, str]:
        """(the big line, the small line under it)."""
        s = self.step
        if self.state == DONE or s is None:
            return "Finished", f"{len(self.kept)} value(s) named. Save makes the profile."
        if self.state == QUIET:
            return "Hands off the machine", "Learning what changes by itself"
        if self.state == REST:
            return "Get ready: don't touch anything yet", s.prompt
        if self.state == DO:
            return s.prompt, "Hold it there..."
        if self.state == LOOK:
            return "Let go", "Looking at what followed..."
        if self.state == RESULT:
            return (f"{s.name}: found" if any(o.keep for o in self.offers) else f"{s.name}: nothing kept"), self.message
        return s.prompt, s.caution or ("Press Start, then wait for the prompt" if not self.frames_seen
                                       else "Press Start when you're ready")

    # --- frames in ---
    def feed(self, frame):
        """Every received frame, from the receive thread."""
        if getattr(frame, "error", None) or getattr(frame, "tx", False):
            return
        self.frames_seen += 1
        if self.state in (QUIET, REST, DO):
            with self._lock:
                self._frames.append(frame)

    # --- the technician's keys ---
    def begin(self, now: float | None = None):
        """Start (or redo) the current step."""
        now = time.time() if now is None else now
        if self.state in (QUIET, REST, DO, LOOK) or self.step is None:
            return
        with self._lock:
            self._frames = []
        self.offers, self.message = [], ""
        if not self._learnt:
            self.state, self._t = QUIET, [now, 0.0, 0.0]
            return
        self.state = REST
        self._t = [now, now + self.rest, now + self.rest + self.do]

    again = begin

    def skip(self):
        if self.state in (WAITING, RESULT) and self.step is not None:
            self._next()

    def toggle(self, i: int):
        if self.state == RESULT and 0 <= i < len(self.offers):
            self.offers[i].keep = not self.offers[i].keep

    def accept(self):
        """Keep what's ticked for this step and move on."""
        if self.state != RESULT:
            return
        s = self.step
        ticked = [o for o in self.offers if o.keep]
        for n, o in enumerate(ticked):
            name = s.name if n == 0 else f"{s.name} {n + 1}"
            hint = o.cand.suggestion()
            same_unit = hint and hint.get("unit") == s.unit
            self.kept[o.cand.key] = observed.signal_entry(
                o.cand, name, s.unit, hint["scale"] if same_unit else 1.0, hint["offset"] if same_unit else 0.0,
                s.group, observed.SURE[1] if o.again else observed.SURE[0])
        self._next()

    def _next(self):
        self.index += 1
        self.offers, self.message = [], ""
        self.state = DONE if self.index >= len(self.steps) else WAITING

    def finish(self):
        self.state = DONE

    def entries(self) -> list[dict]:
        return list(self.kept.values())

    # --- time passing ---
    def tick(self, now: float | None = None):
        now = time.time() if now is None else now
        if self.state == QUIET and now >= self._t[0] + self.quiet:
            self._learn(now)
        elif self.state == REST and now >= self._t[1]:
            self.state = DO
            self.marks.append((self._t[1], self.step.name))
        elif self.state == DO and now >= self._t[2]:
            self.state = LOOK
            self.marks.append((self._t[2], "(rest)"))
            self._look()

    def _take(self) -> list:
        with self._lock:
            frames, self._frames = self._frames, []
        return frames

    def _learn(self, now: float):
        """The hands-off stretch: whatever differs between its two halves changes by itself."""
        frames = self._take()
        start, mid, end = self._t[0], self._t[0] + self.quiet / 2, self._t[0] + self.quiet
        if frames:
            r = changes.analyze(frames, [changes.Phase("a", start, mid), changes.Phase("b", mid, end + 1)])
            self.restless |= {c.key for c in observed.candidates(r, include_known=True)}
        self._learnt = True
        self.state = REST
        self._t = [now, now + self.rest, now + self.rest + self.do]

    def _look(self):
        s = self.step
        frames = self._take()
        r = changes.analyze(frames, [changes.Phase("Rest", self._t[0], self._t[1]),
                                     changes.Phase(s.name, self._t[1], self._t[2] + 1)])
        self.report = r
        cands = [c for c in observed.candidates(r) if c.key not in self.restless and c.key not in self.kept]
        cands.sort(key=lambda c: (c.is_switch != s.switch, -c.score, c.key))     # the kind the step expects first
        self.offers = []
        for c in cands[:OFFER]:
            times = self.seen.get((s.name, c.key), 0)
            self.seen[(s.name, c.key)] = times + 1
            self.offers.append(Offer(c, keep=False, again=times > 0))
        if self.offers:
            self.offers[0].keep = True
            more = len(cands) - 1
            self.message = f"{self.offers[0].cand.key}  {self.offers[0].text}" + (f"  (+{more} more)" if more else "")
        elif not frames:
            self.message = "No frames arrived. Is the bus connected?"
        else:
            named = [c for c in observed.candidates(r, include_known=True) if c.field.known]
            self.message = (f"Only values already named changed ({named[0].field.known})." if named
                            else "Nothing followed. Try again, a little slower, or skip.")
        self.state = RESULT
