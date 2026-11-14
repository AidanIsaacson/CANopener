"""The guided Profile Builder: it tells the technician what to do, watches the bus while they do
it, and names what followed.

A few questions (``QUESTIONS``: the kind of machine, its fuel and aftertreatment, how its
hydraulics are controlled, whether the mast reports its height) choose the script
(``build_script``). Control inputs come first, with
the key on and nothing running, so each is seen alone before the engine or pump adds its own traffic.

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
    stage: str = ""         # the part of the walk-through it belongs to (shown as a heading)


@dataclass
class Question:
    id: str
    text: str
    options: list           # [(label, value)]
    when: object = None     # when(answers) -> bool: asked only then (None: always)


MOVES = "The machine will move: clear the area, and only do it if it's safe."
CONTROLS = "Controls: key on, engine / motors off"
POWER = "Power on"
WORK = "Working: engine or pump running"

IC, ELECTRIC, TELEHANDLER, CART, PALLET, OTHER = "ic", "electric", "telehandler", "cart", "pallet", "other"
ELECTRONIC, MANUAL, UNSURE = "electronic", "manual", "unsure"
MACHINES = {IC: "IC forklift", ELECTRIC: "Electric forklift", TELEHANDLER: "Telehandler", CART: "Golf cart",
            PALLET: "Pallet jack", OTHER: "Something else"}

QUESTIONS = [
    Question("machine", "What kind of machine?", [(label, key) for key, label in MACHINES.items()]),
    Question("fuel", "What does the engine run on?",
             [("LP gas (propane)", "lp"), ("Petrol / gasoline", "gas"), ("Diesel", "diesel"),
              ("Dual fuel (petrol and LP)", "dual")],
             when=lambda a: a.get("machine") in (IC, TELEHANDLER)),
    Question("aftertreatment", "Does it have exhaust aftertreatment (a DPF, DEF / AdBlue, a regen button)?",
             [("No", False), ("Yes", True), ("Not sure (look for it)", True)],
             when=lambda a: a.get("fuel") == "diesel"),
    Question("hydraulics", "How are the hydraulics controlled?",
             [("Electronic: levers or a joystick with sensors (the valve is driven electrically)", ELECTRONIC),
              ("Manual: the levers move the valve themselves", MANUAL),
              ("Not sure (try the levers both ways)", UNSURE)],
             when=lambda a: a.get("machine") in (IC, ELECTRIC, TELEHANDLER)),
    Question("height", "Does the mast report its height?",
             [("A height switch (free lift / staging)", "switch"), ("A height sensor or encoder", "encoder"),
              ("Both", "both"), ("Neither", "none"), ("Not sure (look for both)", "both")],
             when=lambda a: a.get("machine") in (IC, ELECTRIC)),
    Question("side_shift", "Side shift or an attachment fitted?", [("Yes", True), ("No", False)],
             when=lambda a: a.get("machine") in (IC, ELECTRIC)),
]


def next_question(answers: dict) -> Question | None:
    """The next question to ask, given the answers so far (None: ready to build the script)."""
    for q in QUESTIONS:
        if q.id not in answers and (q.when is None or q.when(answers)):
            return q
    return None


def _steps(stage: str, *rows) -> list[Step]:
    return [Step(name, prompt, group, unit, switch, caution, stage)
            for name, prompt, group, unit, switch, caution in rows]


def _lever_inputs(machine: str, side_shift: bool) -> list:
    """The control inputs of electronic hydraulics: each lever both ways, nothing running."""
    rows = [("Lift lever", "Pull the lift lever fully to RAISE and hold (nothing should move)", "Hydraulics", "%",
             False, ""),
            ("Lower lever", "Push the lift lever fully to LOWER and hold", "Hydraulics", "%", False, ""),
            ("Tilt back lever", "Pull the tilt lever fully BACK and hold", "Hydraulics", "%", False, ""),
            ("Tilt forward lever", "Push the tilt lever fully FORWARD and hold", "Hydraulics", "%", False, "")]
    if machine == TELEHANDLER:
        rows = [("Boom raise lever", "Move the joystick fully to RAISE the boom and hold (nothing should move)",
                 "Hydraulics", "%", False, ""),
                ("Boom lower lever", "Move the joystick fully to LOWER the boom and hold", "Hydraulics", "%", False,
                 ""),
                ("Boom extend control", "Work the EXTEND control fully and hold", "Hydraulics", "%", False, ""),
                ("Boom retract control", "Work the RETRACT control fully and hold", "Hydraulics", "%", False, ""),
                ("Tilt back lever", "Joystick fully to tilt the carriage BACK and hold", "Hydraulics", "%", False, ""),
                ("Tilt forward lever", "Joystick fully to tilt the carriage FORWARD and hold", "Hydraulics", "%",
                 False, ""),
                ("Auxiliary control", "Work the auxiliary control fully and hold", "Hydraulics", "%", False, "")]
    elif side_shift:
        rows += [("Side shift lever", "Move the side shift lever fully one way and hold", "Hydraulics", "%", False, ""),
                 ("Auxiliary lever", "Move the auxiliary lever fully and hold (skip if there is none)", "Hydraulics",
                  "%", False, "")]
    return rows


def _fuel_controls(fuel: str, aftertreatment: bool) -> list:
    """Key-on steps that depend on the fuel: nothing running yet."""
    rows = []
    if fuel in ("lp", "dual"):
        rows.append(("LP fuel pressure switch", "Close the LP tank valve, wait for the prompt, then open it and "
                     "leave it open", "Engine", "", True, ""))
    if fuel == "dual":
        rows.append(("Fuel select switch", "Change the fuel selector to the other fuel and leave it", "Engine", "",
                     True, ""))
    if fuel == "diesel" and aftertreatment:
        rows.append(("Regeneration inhibit switch", "Press and hold the regeneration INHIBIT switch", "Engine", "",
                     True, ""))
        rows.append(("Regeneration request switch", "Press the regeneration (parked regen) switch briefly and let "
                     "go", "Engine", "", True, "With the key on and the engine off this only reads the switch. "
                                               "Don't hold it with the engine running: that can start a "
                                               "regeneration."))
    return rows


def _fuel_work(fuel: str, aftertreatment: bool) -> list:
    """Engine-running steps that depend on the fuel."""
    rows = []
    if fuel in ("lp", "gas", "dual"):
        rows.append(("Throttle position", "Blip the accelerator from idle to about half and back, a few times",
                     "Engine", "%", False, ""))
    if fuel == "diesel":
        rows.append(("Engine load", "Hold tilt back against its stop with the revs up for a moment", "Engine", "%",
                     False, MOVES))
    if fuel == "diesel" and aftertreatment:
        rows.append(("Exhaust temperature", "Hold the revs at about half throttle for the whole count", "Engine",
                     "°C", False, ""))
    return rows


def _height(answer: str) -> list:
    rows = []
    if answer in ("switch", "both"):
        rows.append(("Mast height switch", "Raise the forks up past free lift (through the height switch) and hold "
                     "them there", "Hydraulics", "", True, MOVES))
    if answer in ("encoder", "both"):
        rows.append(("Mast height", "Raise the forks steadily to about head height and hold", "Hydraulics", "mm",
                     False, MOVES))
    return rows


def build_script(answers: dict) -> tuple[str, list[Step]]:
    """(a title, the steps) for the answers to QUESTIONS. Control inputs come first, with the key
    on and nothing running, so each one is seen alone before the engine or pump adds its own traffic."""
    machine = answers.get("machine", OTHER)
    hyd = answers.get("hydraulics", MANUAL)
    side_shift = bool(answers.get("side_shift"))
    levers = _lever_inputs(machine, side_shift) if hyd in (ELECTRONIC, UNSURE) else []
    title = MACHINES.get(machine, MACHINES[OTHER])
    fuel, after = answers.get("fuel", ""), bool(answers.get("aftertreatment"))
    if machine in (IC, TELEHANDLER) and fuel:
        title += {"lp": ", LP", "gas": ", petrol", "diesel": ", diesel", "dual": ", dual fuel"}[fuel]
        title += " with aftertreatment" if fuel == "diesel" and after else ""
    if machine in (IC, ELECTRIC, TELEHANDLER):
        title += {ELECTRONIC: ", electronic hydraulics", MANUAL: ", manual hydraulics"}.get(hyd, "")
    seat = ("Seat switch", "Stand up off the seat", "Operator", "", True, "")
    pedal = ("Accelerator pedal", "Press the accelerator pedal fully and hold", "Drive", "%", False, "")
    brake = ("Brake pedal", "Press and hold the brake pedal", "Drive", "", True, "")
    park = ("Parking brake", "Release the parking brake (set it again afterwards)", "Drive", "", True, "")
    fwd = ("Forward selected", "Select FORWARD with the brake held", "Drive", "", True, "")
    rev = ("Reverse selected", "Select REVERSE with the brake held", "Drive", "", True, "")
    horn = ("Horn", "Press and hold the horn", "Operator", "", True, "")
    lights = ("Lights", "Switch the lights on", "Operator", "", True, "")
    if machine in (IC, TELEHANDLER):
        work = [("Engine speed", "Hold the revs up, about half throttle", "Engine", "rpm", False, "")]
        if machine == IC:
            work += [("Lift", "Raise the forks and hold the lever", "Hydraulics", "", True, MOVES),
                     ("Lower", "Lower the forks and hold the lever", "Hydraulics", "", True, MOVES),
                     ("Tilt", "Tilt back and hold the lever", "Hydraulics", "", True, MOVES)]
            if side_shift:
                work.append(("Side shift", "Side shift and hold the lever", "Hydraulics", "", True, MOVES))
            work += _height(answers.get("height", "none"))
        else:
            work += [("Boom raise", "Raise the boom and hold", "Hydraulics", "", True, MOVES),
                     ("Boom angle", "Raise the boom steadily to about 45 degrees and hold", "Hydraulics", "°", False,
                      MOVES),
                     ("Boom extension", "Extend the boom about half way and hold", "Hydraulics", "mm", False, MOVES),
                     ("Tilt", "Tilt the carriage back and hold", "Hydraulics", "", True, MOVES),
                     ("Stabilisers", "Lower the stabilisers (skip if there are none)", "Hydraulics", "", True, MOVES),
                     ("Steering mode", "Change the steering mode (skip if there is one mode)", "Drive", "", True, "")]
        work += [("Hydraulic pressure", "Hold tilt back against its stop for a moment", "Hydraulics", "kPa", False,
                  MOVES)]
        work[1:1] = _fuel_work(fuel, after)             # straight after the engine speed step
        power = [("Engine running", "Start the engine and let it idle", "Engine", "", True, "")]
        if fuel == "diesel":
            power.insert(0, ("Glow plugs", "Turn the key from OFF to ON and wait for the wait-to-start lamp to go "
                             "out (engine cold if you can)", "Engine", "", True, ""))
        steps = (_steps(CONTROLS, seat, pedal, brake, park, fwd, rev, *levers, *_fuel_controls(fuel, after), horn,
                        lights)
                 + _steps(POWER, *power) + _steps(WORK, *work))
    elif machine == ELECTRIC:
        work = [("Pump motor", "Raise the forks and hold the lever", "Hydraulics", "", True, MOVES),
                ("Lower", "Lower the forks and hold the lever", "Hydraulics", "", True, MOVES),
                ("Tilt", "Tilt back and hold the lever", "Hydraulics", "", True, MOVES)]
        work += [("Side shift", "Side shift and hold the lever", "Hydraulics", "", True, MOVES)] if side_shift else []
        work += _height(answers.get("height", "none"))
        work += [("Motor speed", "Drive forward slowly in a clear area (or with the drive wheels off the floor)",
                  "Drive", "rpm", False, MOVES),
                 ("Battery current", "Hold tilt back against its stop for a moment", "Electrical", "A", False, MOVES)]
        steps = (_steps(CONTROLS, seat, pedal, brake, park, fwd, rev, *levers, horn, lights)
                 + _steps(WORK, *work)
                 + _steps("Charging", ("Charging", "Plug the charger in (skip if it can't be done here)",
                                       "Electrical", "", True, "")))
    elif machine == CART:
        steps = (_steps(CONTROLS,
                        ("Key switch", "Turn the key from OFF to ON", "Electrical", "", True, ""),
                        ("Forward selected", "Move the direction switch to FORWARD", "Drive", "", True, ""),
                        ("Reverse selected", "Move the direction switch to REVERSE", "Drive", "", True, ""),
                        pedal, brake, horn, lights)
                 + _steps(WORK, ("Motor speed", "Drive forward slowly in a clear area (or with the drive wheels "
                                 "off the ground)", "Drive", "rpm", False, MOVES),
                          ("Motor current", "Pull away briskly, then let off", "Drive", "A", False, MOVES))
                 + _steps("Charging", ("Charging", "Plug the charger in (skip if it can't be done here)",
                                       "Electrical", "", True, "")))
    elif machine == PALLET:
        steps = (_steps(CONTROLS,
                        ("Key switch", "Turn the key (or enter the code) from OFF to ON", "Electrical", "", True, ""),
                        ("Operator present", "Pull the tiller down into the driving position and hold", "Operator",
                         "", True, ""),
                        ("Accelerator pedal", "Tiller down: twist the throttle fully FORWARD and hold (drive wheel "
                         "off the floor, or brake on)", "Drive", "%", False, MOVES),
                        ("Reverse selected", "Tiller down: twist the throttle fully to REVERSE and hold", "Drive", "",
                         True, MOVES),
                        ("Switch", "Press and hold the belly (emergency reverse) button", "Operator", "", True, ""),
                        horn)
                 + _steps(WORK, ("Lift", "Press and hold LIFT", "Hydraulics", "", True, MOVES),
                          ("Lower", "Press and hold LOWER", "Hydraulics", "", True, MOVES),
                          ("Motor speed", "Drive forward slowly in a clear area", "Drive", "rpm", False, MOVES))
                 + _steps("Charging", ("Charging", "Plug the charger in (skip if it can't be done here)",
                                       "Electrical", "", True, "")))
    else:
        steps = _steps("", ("Switch", "Work one switch or control and hold it", "Other", "", True, ""),
                       ("Value", "Move one lever, pedal or dial fully and hold it", "Other", "", False, ""))
    return title, steps


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
        return s.prompt, s.caution or "Press Start, then wait for 'do it'"

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
