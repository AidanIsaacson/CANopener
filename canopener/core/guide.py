"""The guided Profile Builder: it tells the technician what to do, watches the bus while they do
it, and names what followed.

A few questions (``QUESTIONS``: the kind of machine, its fuel and aftertreatment, how its
hydraulics are controlled, whether the mast reports its height) choose the script
(``build_script``). Control inputs come first, with
the key on and nothing running, so each is seen alone before the engine or pump adds its own traffic.

A ``Guide`` walks a script of steps ("Start the engine", "Raise the forks", "Press the horn").
For each one it wants the machine left alone for a moment (the *rest*), then the thing done and
held (the *do*). It keeps the frames of both, compares them with ``core.changes`` and offers what
followed under the step's own name, live: while the technician works the control again, each
offer counts its changes (from a ``core.fieldwatch.FieldWatch``) and the ones that follow come
first, so the right one is picked by watching rather than guessed from one sighting.

Steps that are one control's two directions (``axis`` / ``direction``: lift and lower, travel
forks-first and back) usually move the same field the other way. The second one offers that
field again, ticked, and keeping it makes one signal of the two: signed when it swings either side
of zero, named for both directions ("Lift/lower input (+ lower / - lift)").

Machines guard their controls with interlocks: a control worked with nobody on the seat or the
operator pedal, or held when they get on, raises a "release the control" warning (a CANopen EMCY,
a J1939 DM1 code) that clears when it's let go. The builder expects them: each stage says so
before it starts (``stage_note``), a warning raised while a step is held is reported with that
step and named after it in the profile (``emcy_names``), and the next step waits until the
machine has cleared it (``SETTLE``), so it doesn't spoil the next hands-off stretch.

Each step leaves three timestamps in the log ("Lift: get ready", "Lift", "Lift: let go") and a
record of what was offered and kept (``summary``), which the screen writes into the capture.

Left out: what a decoder already names (shown separately, live), what changed while nobody was
touching the machine (the opening hands-off stretch and every rest so far), and what the watcher
says can't be a control (the low bits of a reading, half of a 16-bit number, counters, noise).
Doing a step a second time and seeing the same field again marks it "seen again"; one that
moved with the control while picking is "checked".

It only listens: nothing here transmits. It is a state machine fed by ``feed(frame)`` (from the
session's receive thread) and ``tick(now)`` (from the screen); the screen shows ``prompt()`` and
calls ``begin`` / ``again`` / ``skip`` / ``toggle`` / ``accept`` (and ``touch`` on any key, so the
pick list holds still under a finger). ``entries()`` are the profile signals to save
(``core.observed``). No Qt, no Pygame.
"""

import threading
import time
from dataclasses import dataclass, field

from ..canopen import emcy as canopen_emcy
from ..j1939.dtc import decode_dm
from ..j1939.ids import parse_id
from . import changes, fieldwatch, observed

DM1 = 0xFECA
# Protocol housekeeping, never a control's own signal: NMT, SYNC / EMCY, SDO, heartbeat (CANopen);
# requests, transport protocol, acknowledgements and DM1 (J1939).
HOUSEKEEPING_PGNS = {0xEA00, 0xEB00, 0xEC00, 0xE800, DM1}


def housekeeping(stream) -> bool:
    if stream.kind == "j1939":
        return stream.ident in HOUSEKEEPING_PGNS
    i = stream.ident
    return i == 0 or 0x080 <= i <= 0x0FF or 0x580 <= i <= 0x67F or 0x700 <= i <= 0x77F

REST_SECONDS = 3.0          # hands off before each step
DO_SECONDS = 4.0            # do it and hold it
QUIET_SECONDS = 6.0         # the hands-off stretch at the very start
OFFER = 8                   # candidates shown per step (six fit on the handheld; the rest scroll)
WANDERS = 2                 # changes while hands-off that mark a field as moving by itself
CHECKED_MOVES = 4           # changes while picking (twice on and off) that count as checked by hand
HUMAN_MOVES = 40            # more changes than this while picking isn't a person working a control
WANDERS_BEFORE = 3          # changes in the minute before the builder started that mark a field as restless
BEFORE_SECONDS = 60.0
RESORT_IDLE = 4.0           # seconds after a key press before the pick list may re-sort
THIN_RATE = 10.0            # frames a second below which the bus is too quiet to learn from
SETTLE_MAX = 12.0           # seconds to wait for an interlock warning to clear before a step goes on anyway
WAITING, QUIET, REST, DO, LOOK, RESULT, DONE = "waiting", "quiet", "rest", "do", "look", "result", "done"
SETTLE = "settle"           # waiting for the machine to clear a warning before the next step


@dataclass
class Step:
    name: str               # the name its signal gets: "Lift"
    prompt: str             # what to do: "Raise the forks and hold"
    group: str = "Observed"
    unit: str = ""
    switch: bool = True     # expect an on / off (else a value that moves)
    caution: str = ""       # said first when the step moves the machine
    stage: str = ""         # the part of the walk-through it belongs to (shown as a heading)
    axis: str = ""          # one control's two directions share an axis: "Lift/lower input"
    direction: str = ""     # this step's direction on it: "lift"


@dataclass
class Question:
    id: str
    text: str
    options: list           # [(label, value)]
    when: object = None     # when(answers) -> bool: asked only then (None: always)


MOVES = "The machine will move: clear the area, and only do it if it's safe."
CONTROLS = "Controls: key on, engine / motors off"
OFF_PEDAL = "Controls: key on, standing off the operator pedal"
POWER = "Power on"
WORK = "Working: engine or pump running"
ON_PEDAL = "Working: standing on the operator pedal"

# Said before the first step of each stage: what state the machine is in, and what to expect.
INTERLOCK = ("Working a control like this can make the machine show a 'release the control' or interlock "
             "warning. That's expected: it clears when you let go, and the builder waits for it before the next "
             "step.")
STAGE_NOTES = {
    CONTROLS: "Key on, engine and motors off, nobody on the seat. " + INTERLOCK,
    OFF_PEDAL: "Key on, standing beside the truck or with both feet off the operator pedal, so it can't drive or "
               "lift. " + INTERLOCK,
    POWER: "Now the engine or motors run.",
    WORK: "Engine or pump running. Start each function from rest.",
    ON_PEDAL: "Stand on the operator pedal for these. Start each control from rest: one already held when you step "
              "on can lock that function until it's released (the same interlock).",
}

IC, ELECTRIC, STANDUP, TELEHANDLER, CART, PALLET, OTHER = ("ic", "electric", "standup", "telehandler", "cart",
                                                           "pallet", "other")
ELECTRONIC, MANUAL, UNSURE = "electronic", "manual", "unsure"
REACH, COUNTERBALANCE = "reach", "counterbalance"
MACHINES = {IC: "IC forklift", ELECTRIC: "Electric forklift (sit-down)", STANDUP: "Electric stand-up",
            TELEHANDLER: "Telehandler", CART: "Golf cart", PALLET: "Pallet jack", OTHER: "Something else"}

QUESTIONS = [
    Question("machine", "What kind of machine?", [(label, key) for key, label in MACHINES.items()]),
    Question("fuel", "What does the engine run on?",
             [("LP gas (propane)", "lp"), ("Petrol / gasoline", "gas"), ("Diesel", "diesel"),
              ("Dual fuel (petrol and LP)", "dual")],
             when=lambda a: a.get("machine") in (IC, TELEHANDLER)),
    Question("aftertreatment", "Does it have exhaust aftertreatment (a DPF, DEF / AdBlue, a regen button)?",
             [("No", False), ("Yes", True), ("Not sure (look for it)", True)],
             when=lambda a: a.get("fuel") == "diesel"),
    Question("standup", "Reach truck or counterbalance?",
             [("Reach truck: the forks reach out from the mast", REACH),
              ("Counterbalance: no reach; usually two drive motors, one each side", COUNTERBALANCE)],
             when=lambda a: a.get("machine") == STANDUP),
    Question("pedals", "Which pedals does the operator stand on?",
             [("An operator presence pedal and a separate brake pedal", "both"),
              ("One pedal: standing on it releases the brake", "one"),
              ("Not sure (ask for both)", "both")],
             when=lambda a: a.get("machine") == STANDUP),
    Question("gate", "Is there an operator gate or gate bar (closes the operator compartment)?",
             [("Yes", True), ("No", False), ("Not sure (ask for it)", True)],
             when=lambda a: a.get("machine") == STANDUP),
    Question("hydraulics", "How are the hydraulics controlled?",
             [("Electronic: levers or a joystick with sensors (the valve is driven electrically)", ELECTRONIC),
              ("Manual: the levers move the valve themselves", MANUAL),
              ("Not sure (try the levers both ways)", UNSURE)],
             when=lambda a: a.get("machine") in (IC, ELECTRIC, TELEHANDLER)),
    Question("height", "Does the mast report its height?",
             [("A height switch (free lift / staging)", "switch"), ("A height sensor or encoder", "encoder"),
              ("Both", "both"), ("Neither", "none"), ("Not sure (look for both)", "both")],
             when=lambda a: a.get("machine") in (IC, ELECTRIC, STANDUP)),
    Question("side_shift", "Side shift or an attachment fitted?", [("Yes", True), ("No", False)],
             when=lambda a: a.get("machine") in (IC, ELECTRIC, STANDUP)),
]


def next_question(answers: dict) -> Question | None:
    """The next question to ask, given the answers so far (None: ready to build the script)."""
    for q in QUESTIONS:
        if q.id not in answers and (q.when is None or q.when(answers)):
            return q
    return None


def _steps(stage: str, *rows) -> list[Step]:
    """Steps from rows of (name, prompt, group, unit, switch, caution[, axis, direction])."""
    return [Step(*row[:6], stage, *row[6:]) for row in rows]


def _axis(axis: str, unit: str = "%", group: str = "Hydraulics", *dirs) -> list:
    """Two value steps for one control's two directions: dirs are (name, direction, prompt)."""
    return [(name, prompt, group, unit, False, "", axis, direction) for name, direction, prompt in dirs]


def _standup(kind: str, height: str, side_shift: bool, pedals: str = "both", gate: bool = False
             ) -> tuple[list, list]:
    """(the controls, the working steps) of an electric stand-up: a reach truck or a counterbalance.

    The controls are read with the key on and the operator off the pedal (it can't drive or lift
    then); most stand-ups put travel, lift / lower, tilt, reach and side shift on one control handle,
    each a signed value either side of rest, so each is asked for both ways."""
    if pedals == "one":
        controls = [("Operator pedal", "Stand on the operator (brake) pedal and hold, then step off at 'let go'",
                     "Operator", "", True, "")]
    else:
        controls = [("Operator presence pedal", "Stand on the operator presence pedal and hold, then step off at "
                     "'let go'", "Operator", "", True, ""),
                    ("Brake pedal", "Press the brake pedal and hold, then let it up at 'let go'", "Operator", "",
                     True, "")]
    if gate:
        controls.append(("Gate bar", "Close the operator gate or gate bar (open it if it's closed) and hold it there",
                         "Operator", "", True, ""))
    controls += _axis("Travel input", "%", "Drive",
                      ("Travel forks first", "forks first",
                       "Off the pedal: move the control handle fully for travel FORKS FIRST and hold"),
                      ("Travel power unit first", "power unit first",
                       "Off the pedal: move the control handle fully for travel POWER UNIT FIRST and hold"))
    controls += _axis("Lift/lower input", "%", "Hydraulics",
                      ("Lift input", "lift", "Move the control fully to LIFT and hold (nothing should move)"),
                      ("Lower input", "lower", "Move the control fully to LOWER and hold"))
    if kind == REACH:
        controls += _axis("Reach input", "%", "Hydraulics",
                          ("Reach out input", "out", "Move the control fully to REACH OUT and hold"),
                          ("Reach in input", "in", "Move the control fully to REACH IN and hold"))
    controls += _axis("Tilt input", "%", "Hydraulics",
                      ("Tilt back input", "back", "Move the control fully to TILT BACK (up) and hold"),
                      ("Tilt forward input", "forward", "Move the control fully to TILT FORWARD (down) and hold"))
    if side_shift:
        controls += _axis("Side shift input", "%", "Hydraulics",
                          ("Side shift left input", "left", "Move the control fully to SIDE SHIFT LEFT and hold"),
                          ("Side shift right input", "right", "Move the control fully to SIDE SHIFT RIGHT and hold"))
    controls += [("Function button", "Press and hold the function / mode button on the handle (skip if there is "
                  "none)", "Operator", "", True, ""),
                 ("Horn", "Press and hold the horn", "Operator", "", True, "")]
    work = [("Pump motor", "On the pedal: raise the forks a little and hold the control", "Hydraulics", "", True,
             MOVES),
            ("Lower", "Lower the forks and hold the control", "Hydraulics", "", True, MOVES)]
    if kind == REACH:
        work.append(("Reach", "Reach out and hold the control", "Hydraulics", "", True, MOVES))
    work.append(("Tilt", "Tilt back and hold the control", "Hydraulics", "", True, MOVES))
    if side_shift:
        work.append(("Side shift", "Side shift and hold the control", "Hydraulics", "", True, MOVES))
    work += _height(height)
    work.append(("Steer angle", "Without travelling, turn the steering fully one way and hold", "Drive", "°", False,
                 MOVES))
    if kind == REACH:
        work.append(("Motor speed", "Drive slowly forks first in a clear area (or with the drive wheel off the "
                     "floor)", "Drive", "rpm", False, MOVES))
    else:
        work += [("Left drive motor speed", "Drive slowly while turning fully RIGHT, and hold: the left motor runs "
                  "faster (tick the bigger number)", "Drive", "rpm", False, MOVES),
                 ("Right drive motor speed", "Drive slowly while turning fully LEFT, and hold: the right motor runs "
                  "faster (tick the bigger number)", "Drive", "rpm", False, MOVES)]
    work.append(("Battery current", "Hold tilt back against its stop for a moment", "Electrical", "A", False, MOVES))
    return controls, work


def _lever_inputs(machine: str, side_shift: bool) -> list:
    """The control inputs of electronic hydraulics: each lever both ways, nothing running."""
    rows = (_axis("Lift/lower lever", "%", "Hydraulics",
                  ("Lift lever", "lift", "Pull the lift lever fully to RAISE and hold (nothing should move)"),
                  ("Lower lever", "lower", "Push the lift lever fully to LOWER and hold"))
            + _axis("Tilt lever", "%", "Hydraulics",
                    ("Tilt back lever", "back", "Pull the tilt lever fully BACK and hold"),
                    ("Tilt forward lever", "forward", "Push the tilt lever fully FORWARD and hold")))
    if machine == TELEHANDLER:
        rows = (_axis("Boom raise/lower control", "%", "Hydraulics",
                      ("Boom raise lever", "raise",
                       "Move the joystick fully to RAISE the boom and hold (nothing should move)"),
                      ("Boom lower lever", "lower", "Move the joystick fully to LOWER the boom and hold"))
                + _axis("Boom extend/retract control", "%", "Hydraulics",
                        ("Boom extend control", "extend", "Work the EXTEND control fully and hold"),
                        ("Boom retract control", "retract", "Work the RETRACT control fully and hold"))
                + _axis("Tilt control", "%", "Hydraulics",
                        ("Tilt back lever", "back", "Joystick fully to tilt the carriage BACK and hold"),
                        ("Tilt forward lever", "forward", "Joystick fully to tilt the carriage FORWARD and hold"))
                + [("Auxiliary control", "Work the auxiliary control fully and hold", "Hydraulics", "%", False, "")])
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
    if machine == STANDUP:
        kind = answers.get("standup", REACH)
        title += {REACH: ", reach truck", COUNTERBALANCE: ", counterbalance"}.get(kind, "")
        controls, work = _standup(kind, answers.get("height", "none"), side_shift, answers.get("pedals", "both"),
                                  bool(answers.get("gate")))
        return title, (_steps(OFF_PEDAL, *controls) + _steps(ON_PEDAL, *work)
                       + _steps("Charging", ("Charging", "Plug the charger in (skip if it can't be done here)",
                                             "Electrical", "", True, "")))
    seat = ("Seat switch", "Stand up off the seat", "Operator", "", True, "")
    pedal = ("Accelerator pedal", "Press the accelerator pedal fully and hold", "Drive", "%", False, "")
    brake = ("Brake pedal", "Press and hold the brake pedal", "Drive", "", True, "")
    park = ("Parking brake", "Release the parking brake (set it again afterwards)", "Drive", "", True, "")
    fwd = ("Forward selected", "Select FORWARD with the brake held", "Drive", "", True, "", "Direction", "forward")
    rev = ("Reverse selected", "Select REVERSE with the brake held", "Drive", "", True, "", "Direction", "reverse")
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
        # (no glow plug step: it needs the key turned off and on mid-run, which silences the bus,
        # and the wait-to-start lamp is a standard J1939 signal that's decoded already)
        power = [("Engine running", "Start the engine and let it idle", "Engine", "", True, "")]
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
                        ("Throttle forward", "Tiller down: twist the throttle fully FORWARD and hold (drive wheel "
                         "off the floor, or brake on)", "Drive", "%", False, MOVES, "Throttle", "forward"),
                        ("Throttle reverse", "Tiller down: twist the throttle fully to REVERSE and hold", "Drive", "%",
                         False, MOVES, "Throttle", "reverse"),
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


def thin_bus(frames: list, seconds: float) -> str:
    """A warning when the hands-off stretch heard too little to learn from ("" when it's busy enough).

    Most machines broadcast their state many times a second with the key on. A bus that's nearly
    silent usually means the key is off, this connector isn't the machine's main bus, or the
    controllers only answer a service tool that asks (the builder only listens, so it can't see that)."""
    rate = len(frames) / seconds if seconds > 0 else 0.0
    if rate >= THIN_RATE:
        return ""
    ids = len({(f.can_id, f.extended) for f in frames})
    heard = f"about {rate:.0f} frame(s) a second from {ids} ID(s)" if frames else "nothing at all"
    return (f"The bus is very quiet: {heard}. Little here can follow the controls. Check the key is on, or try "
            "the machine's other connector. Some machines only answer a service tool that asks, and this only "
            "listens.")


@dataclass
class Offer:
    """One field offered for a step: ticked to keep it."""

    cand: observed.Candidate
    keep: bool = False
    again: bool = False     # the same field followed this step before
    also: list = field(default_factory=list)    # other steps it was offered for (one that follows all is suspect)
    moved: int = 0          # times it changed while the technician worked the control (the pick)
    chatters: bool = False  # most of those changes didn't hold (a bouncing contact, or noise)
    auto: bool = False      # ticked by the builder because it moved with the control, not by hand
    rank: int = 0           # its place when first offered (ties keep this order)
    mirror: str = ""        # already kept for this axis's other direction (that step's name): keeping it merges
    hint: tuple = ()        # (name, profile) when another profile on the unit knows this field (core.suggest)

    @property
    def key(self) -> str:
        return self.cand.key

    @property
    def text(self) -> str:
        c = self.cand
        return c.evidence[0].split(": ", 1)[-1] if c.evidence else ""

    @property
    def label(self) -> str:
        """In words: 'Proprietary B (Management computer #1): byte 2 bit 3'."""
        s = self.cand.stream
        return f"{s.name or s.label}: {fieldwatch.place_words(self.cand.field.where)}"


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
    bus_note: str = ""                              # set when the bus is too quiet to learn from
    watch: object = None                            # a fieldwatch.FieldWatch (the app's, fed elsewhere) or its own
    known_moved: list = field(default_factory=list) # names already decoded that moved in this step
    known_keys: list = field(default_factory=list)  # [(name, field key)] of those, to show them live
    hidden: int = 0                                 # fields left out of this step: they change by themselves
    offered_at: dict = field(default_factory=dict)  # field key -> [step names it was offered for]
    picking_since: float = 0.0                      # when the current step's pick began
    title: str = ""                                 # the script's title, for the summary
    hints: dict = field(default_factory=dict)       # field key -> (name, profile): what other profiles call it
    journal: list = field(default_factory=list)     # one record per attempt at a step (``summary``)
    step_warnings: list = field(default_factory=list)  # warnings raised while the current step was held
    interlocks: dict = field(default_factory=dict)  # EMCY code -> {"node", "steps"}: raised by a held control
    settle_note: str = ""                           # set when a warning didn't clear in time

    def __post_init__(self):
        self._lock = threading.Lock()
        self._frames: list = []
        self._t = [0.0, 0.0, 0.0]       # rest start, do start, do end
        self._learnt = False
        self._own_watch = self.watch is None
        if self._own_watch:
            self.watch = fieldwatch.FieldWatch()
        self._hands_off: list = []      # [(start, end)]: stretches nobody touched the machine
        self._before = None             # (start, end): the minute before the builder started
        self._last_touch = 0.0          # the technician's last key press (nothing re-sorts under a finger)
        self._last_sort = 0.0
        self._user_ticked = False
        self.report = None
        self._active: dict = {}         # ("emcy", node) / ("dm1", sa) -> (text, code, since): warnings now
        self._raised: list = []         # [(time, key, text, code)]: every warning as it went active
        self._settle_since = 0.0
        self._after_settle = REST

    # --- what the screen shows ---
    @property
    def step(self) -> Step | None:
        return self.steps[self.index] if 0 <= self.index < len(self.steps) else None

    def remaining(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        end = {QUIET: self._t[0] + self.quiet, REST: self._t[1], DO: self._t[2],
               SETTLE: self._settle_since + SETTLE_MAX}.get(self.state)
        return max(0.0, end - now) if end else 0.0

    def prompt(self) -> tuple[str, str]:
        """(the big line, the small line under it)."""
        s = self.step
        if self.state == DONE or s is None:
            return "Finished", f"{len(self.kept)} value(s) named. Save makes the profile."
        if self.state == QUIET:
            return "Hands off the machine", "Learning what changes by itself"
        if self.state == SETTLE:
            return ("Let go of everything", f"Waiting for the machine to clear {self.warning_text()}: a control "
                    "still held, or the operator pedal (an interlock)")
        if self.state == REST:
            return "Get ready: don't touch anything yet", s.prompt
        if self.state == DO:
            return s.prompt, "Hold it there..."
        if self.state == LOOK:
            return "Let go", "Looking at what followed..."
        if self.state == RESULT:
            if self.offers:
                return f"{s.name}: which one moved?", self.message
            return f"{s.name}: nothing new found", self.message
        if self.settle_note:
            return s.prompt, self.settle_note
        return s.prompt, s.caution or "Press Start, then wait for 'do it'"

    def stage_note(self) -> str:
        """What to expect from the stage the next step starts ("" unless it's the stage's first step)."""
        s = self.step
        if s is None or self.state != WAITING or not s.stage:
            return ""
        prev = self.steps[self.index - 1] if self.index > 0 else None
        return STAGE_NOTES.get(s.stage, "") if prev is None or prev.stage != s.stage else ""

    def warnings_now(self) -> list[str]:
        """The machine's warnings active now (EMCY per node, DM1 codes per address)."""
        with self._lock:
            return [text for text, _c, _t in self._active.values()]

    def warning_text(self) -> str:
        now = self.warnings_now()
        return ", ".join(now[:2]) + (f" and {len(now) - 2} more" if len(now) > 2 else "") if now else "its warning"

    def sorting_paused(self, now: float | None = None) -> bool:
        """True for a few seconds after a key press: the list holds still while it's being touched."""
        now = time.time() if now is None else now
        return now - self._last_touch < RESORT_IDLE

    # --- frames in ---
    def feed(self, frame):
        """Every received frame, from the receive thread."""
        if getattr(frame, "error", None) or getattr(frame, "tx", False):
            return
        self.frames_seen += 1
        self._note_warning(frame)
        if self._own_watch:
            self.watch.feed(frame)
        if self.state in (QUIET, REST, DO):
            with self._lock:
                self._frames.append(frame)

    def _note_warning(self, frame):
        """Track the machine's warnings: CANopen EMCY (per node: a code, or 0 for cleared) and
        single-frame J1939 DM1 (per address: the active codes)."""
        data = bytes(frame.data)
        if frame.extended:
            j = parse_id(frame.can_id)
            if j.pgn != DM1 or len(data) < 6:
                return
            _lamps, dtcs = decode_dm(data)
            key = ("dm1", j.sa)
            codes = sorted((d.spn, d.fmi) for d in dtcs if d.spn)
            text = ", ".join(f"SPN {spn} FMI {fmi}" for spn, fmi in codes[:2]) + f" (DM1 from {j.sa:02X})"
            code = tuple(codes)
        else:
            if not 0x081 <= frame.can_id <= 0x0FF or len(data) < 2:
                return
            e = canopen_emcy.decode(data)
            if e is None:
                return
            key, code = ("emcy", frame.can_id - 0x80), e.code
            text = f"EMCY {e.code:04X} from node {frame.can_id - 0x80:02X}"
            codes = [] if e.reset else [code]
        with self._lock:
            old = self._active.get(key)
            if not codes:
                self._active.pop(key, None)
            elif old is None or old[1] != code:
                self._active[key] = (text, code, frame.timestamp)
                self._raised.append((frame.timestamp, key, text, code))

    # --- the technician's keys ---
    def touch(self, now: float | None = None):
        """A key was pressed: hold the list still for a moment."""
        self._last_touch = time.time() if now is None else now

    def begin(self, now: float | None = None, settled: bool = False):
        """Start (or redo) the current step (``settled``: the wait for warnings is over)."""
        now = time.time() if now is None else now
        if self.state in (QUIET, REST, DO, LOOK, SETTLE) or self.step is None:
            return
        with self._lock:
            self._frames = []
        self.offers, self.message, self.known_moved, self.known_keys, self.hidden = [], "", [], [], 0
        self.step_warnings, self.settle_note = [], ""
        if not settled and self.warnings_now():
            self._after_settle = REST if self._learnt else QUIET
            self.state, self._settle_since = SETTLE, now
            return
        if not self._learnt:
            self.state, self._t = QUIET, [now, 0.0, 0.0]
            self._before = (now - BEFORE_SECONDS, now)      # the minute before the builder started
            self.marks.append((now, "Hands off (learning)"))
            return
        self._start_rest(now)

    def _start_rest(self, now: float):
        self.state = REST
        self._t = [now, now + self.rest, now + self.rest + self.do]
        self.marks.append((now, f"{self.step.name}: get ready"))

    again = begin

    def skip(self):
        if self.state in (WAITING, RESULT, SETTLE) and self.step is not None:
            if self.state == RESULT and self.journal:
                self.journal[-1]["result"] = "skipped"
            else:
                self.journal.append({"step": self.step.name, "stage": self.step.stage, "result": "skipped"})
            self._next()

    def toggle(self, i: int):
        if self.state == RESULT and 0 <= i < len(self.offers):
            o = self.offers[i]
            o.keep, o.auto = not o.keep, False
            self._user_ticked = True
            self.touch()

    def accept(self):
        """Keep what's ticked for this step and move on."""
        if self.state != RESULT:
            return
        s = self.step
        ticked = [o for o in self.offers if o.keep]
        for o in [o for o in ticked if o.mirror]:
            self._merge_axis(o)
        ticked = [o for o in ticked if not o.mirror]
        for n, o in enumerate(ticked):
            name = s.name if n == 0 else f"{s.name} {n + 1}"
            hint = o.cand.suggestion()
            same_unit = hint and hint.get("unit") == s.unit
            if o.moved >= CHECKED_MOVES:
                o.cand.evidence.insert(0, f"Picked: it changed {o.moved} times while the control was worked")
            sure = observed.SURE[2] if o.moved >= CHECKED_MOVES else observed.SURE[1] if o.again else observed.SURE[0]
            if o.hint:
                o.cand.evidence.insert(0, f"{o.hint[1]} calls the same field '{o.hint[0]}'")
            entry = observed.signal_entry(
                o.cand, name, s.unit, hint["scale"] if same_unit else 1.0, hint["offset"] if same_unit else 0.0,
                s.group, sure)
            rest, held = self._rest_and_held(o.key)
            entry["observed"].update(step=s.name, **({"axis": s.axis, "direction": s.direction} if s.axis else {}),
                                     **({"rest": rest, "held": held} if held is not None else {}))
            self.kept[o.cand.key] = entry
        if self.journal:
            self.journal[-1].update(result="kept" if ticked or any(o.mirror for o in self.offers if o.keep)
                                    else "nothing kept",
                                    kept=[{"key": o.key, "name": self.kept[o.key]["name"]}
                                          for o in self.offers if o.keep and o.key in self.kept])
        self._next()

    def _rest_and_held(self, key: str) -> tuple:
        """(the field's raw value at rest before the step, the one furthest from it while held)."""
        rest_vals = [v for t, v in self.watch.series(key, since=self._t[0]) if t < self._t[1]]
        held_vals = [v for t, v in self.watch.series(key, since=self._t[1]) if t <= self._t[2] + 0.5]
        if not rest_vals or not held_vals:
            return None, None
        rest = max(set(rest_vals), key=rest_vals.count)
        return rest, max(held_vals, key=lambda v: abs(v - rest))

    def _merge_axis(self, o: "Offer"):
        """Keep the other direction of an axis already kept: one signal for both, signed when it
        swings either side of a rest at zero."""
        s, entry = self.step, self.kept[o.key]
        note = entry["observed"]
        rest, held = self._rest_and_held(o.key)
        width = 16 if entry.get("length") == 2 else 8
        first, other = (note.get("direction") or note.get("step", "")), s.direction or s.name
        h1 = note.get("held")
        if rest == 0 and held is not None and h1 is not None and (h1 >> (width - 1)) != (held >> (width - 1)):
            entry["signed"] = True
            pos, neg = (other, first) if held >> (width - 1) == 0 else (first, other)
            entry["name"] = f"{s.axis} (+ {pos} / - {neg})"
        elif held is not None and h1 is not None:
            lo, hi = (first, other) if h1 < held else (other, first)
            entry["name"] = f"{s.axis} ({lo} low / {hi} high)"
        else:
            entry["name"] = f"{s.axis} ({first} / {other})"
        sure = observed.SURE
        rank = sure.index(note["sure"]) if note.get("sure") in sure else 0
        note["sure"] = sure[max(rank, 1)]
        note.setdefault("evidence", []).insert(0, f"Moved the other way for {s.name}")
        note["other"] = {"step": s.name, "direction": s.direction, "held": held}

    def _next(self):
        self.settle_note = ""
        self.index += 1
        self.offers, self.message, self.known_moved, self.known_keys, self.hidden = [], "", [], [], 0
        self.state = DONE if self.index >= len(self.steps) else WAITING

    def finish(self):
        self.state = DONE

    def entries(self) -> list[dict]:
        return list(self.kept.values())

    def emcy_names(self, known=()) -> dict:
        """Profile ``emcy`` entries for the interlock warnings the steps raised: each named after the
        controls it came with (codes in ``known`` are left alone: the profile already names them)."""
        out = {}
        for code, info in sorted(self.interlocks.items()):
            if code in known:
                continue
            steps = ", ".join(info["steps"][:3]) + (" and more" if len(info["steps"]) > 3 else "")
            out[f"0x{code:04X}"] = (f"Interlock: raised by {steps} with nobody on the machine; clears when the "
                                    "control is let go?")
        return out

    def summary(self) -> dict:
        """What the builder did, for the capture: the script, each attempt at a step (what was
        offered and kept, the warnings it raised) and the interlock codes it named."""
        return {"script": self.title, "steps": list(self.journal), "named": len(self.kept),
                "interlocks": {f"0x{c:04X}": {"node": i["node"], "steps": list(i["steps"])}
                               for c, i in sorted(self.interlocks.items())}}

    # --- time passing ---
    def tick(self, now: float | None = None):
        now = time.time() if now is None else now
        if self.state == QUIET and now >= self._t[0] + self.quiet:
            self._hands_off.append((self._t[0], self._t[0] + self.quiet))
            self._learn(now)
        elif self.state == SETTLE:
            if not self.warnings_now() or now - self._settle_since >= SETTLE_MAX:
                if self.warnings_now():
                    self.settle_note = (f"{self.warning_text()} didn't clear: going on anyway (it may be a real "
                                        "fault, not an interlock)")
                if self._after_settle == QUIET:
                    self.state = WAITING
                    keep = self.settle_note
                    self.begin(now, settled=True)
                    self.settle_note = keep
                else:
                    self._start_rest(now)
        elif self.state == REST and now >= self._t[1]:
            self._hands_off.append((self._t[0], self._t[1]))
            self.state = DO
            self.marks.append((self._t[1], self.step.name))
        elif self.state == DO and now >= self._t[2]:
            self.state = LOOK
            self.marks.append((self._t[2], f"{self.step.name}: let go"))
            self._look(now)
        elif self.state == RESULT:
            self._follow_pick(now)

    def _take(self) -> list:
        with self._lock:
            frames, self._frames = self._frames, []
        return frames

    def _learn(self, now: float):
        """The hands-off stretch: whatever differs between its two halves changes by itself."""
        frames = self._take()
        self.bus_note = thin_bus(frames, self.quiet)
        start, mid, end = self._t[0], self._t[0] + self.quiet / 2, self._t[0] + self.quiet
        if frames:
            r = changes.analyze(frames, [changes.Phase("a", start, mid), changes.Phase("b", mid, end + 1)])
            self.restless |= {c.key for c in observed.candidates(r, include_known=True)}
        self._learnt = True
        self._start_rest(now)

    def wanders(self, key: str) -> bool:
        """Did it change by itself, while nobody was touching the machine (the hands-off stretch
        and the 'get ready' of every step so far)?"""
        if self.watch.transitions(key, intervals=self._hands_off[-12:]) >= WANDERS:
            return True
        # the first steps have little hands-off behind them: the minute before the builder started
        # (menus, not controls) says which values keep changing by themselves
        return self._before is not None and self.watch.transitions(key, intervals=[self._before]) >= WANDERS_BEFORE

    def _look(self, now: float):
        s = self.step
        frames = self._take()
        self.watch.refresh(now)
        r = changes.analyze(frames, [changes.Phase("Rest", self._t[0], self._t[1]),
                                     changes.Phase(s.name, self._t[1], self._t[2] + 1)])
        self.report = r
        every = observed.candidates(r, include_known=True)
        named = {}
        for c in every:
            if c.known and c.known not in named:
                # a bit of a named reading: show the reading itself (its byte), not the bit
                whole = c.is_switch and not c.field.known
                named[c.known] = f"{c.stream.label} {c.field.where.split('.')[0]}" if whole else c.key
        words = {w for w in s.name.lower().split() if len(w) > 3}
        ranked = sorted(named.items(), key=lambda kv: not words & set(kv[0].lower().replace("(", " ").split()))
        self.known_moved = [n for n, _k in ranked]      # the ones that sound like the step first
        self.known_keys = ranked
        fresh, mirrors, self.hidden = [], [], 0
        for c in every:
            if housekeeping(c.stream):
                continue
            if c.key in self.kept and s.axis and not c.is_switch:
                note = self.kept[c.key]["observed"]
                if note.get("axis") == s.axis and note.get("step") != s.name and "other" not in note:
                    mirrors.append((c, note.get("step", "")))
                continue
            if c.known or c.key in self.kept:
                continue
            if c.key in self.restless or not self.watch.plausible(c.key) or self.wanders(c.key):
                self.hidden += 1
                continue
            fresh.append(c)
        # the kind the step expects first; a field already offered for another step after the rest
        fresh.sort(key=lambda c: (c.is_switch != s.switch, any(n != s.name for n in self.offered_at.get(c.key, [])),
                                  -c.score, c.key))
        self.offers = [Offer(c, keep=True, mirror=step, rank=-1) for c, step in mirrors]
        for rank, c in enumerate(fresh[:OFFER]):
            times = self.seen.get((s.name, c.key), 0)
            self.seen[(s.name, c.key)] = times + 1
            also = [n for n in self.offered_at.get(c.key, []) if n != s.name]
            if s.name not in self.offered_at.setdefault(c.key, []):
                self.offered_at[c.key].append(s.name)
            o = Offer(c, again=times > 0, also=also, rank=rank, hint=tuple(self.hints.get(c.key, ())))
            o.keep = o.again or bool(c.suggestion()) or bool(o.hint)   # ticked only on good evidence
            self.offers.append(o)
        self.offers.sort(key=lambda o: (not o.hint and not o.mirror, o.rank))
        self._note_step_warnings()
        self.picking_since, self._last_sort, self._last_touch, self._user_ticked = now, now, 0.0, False
        self.message = self._explain(frames)
        self.journal.append({"step": s.name, "stage": s.stage, "at": round(self._t[1], 3), "result": "looked",
                             "offered": [{"key": o.key, "label": o.label, "text": o.text,
                                          **({"also_known_as": list(o.hint)} if o.hint else {}),
                                          **({"other_direction_of": o.mirror} if o.mirror else {})}
                                         for o in self.offers],
                             "already_named": list(self.known_moved[:6]), "left_out": self.hidden,
                             "warnings": list(self.step_warnings)})
        self.state = RESULT

    def _note_step_warnings(self):
        """Warnings that went active while this step was held: interlocks, named after the step."""
        s = self.step
        with self._lock:
            raised = [r for r in self._raised if self._t[1] - 0.2 <= r[0] <= self._t[2] + 0.5]
        self.step_warnings = []
        for _t, key, text, code in raised:
            if text not in self.step_warnings:
                self.step_warnings.append(text)
            if key[0] == "emcy":
                info = self.interlocks.setdefault(code, {"node": f"0x{key[1]:02X}", "steps": []})
                if s.name not in info["steps"]:
                    info["steps"].append(s.name)

    def _explain(self, frames) -> str:
        text = self._explain_found(frames)
        if self.step_warnings:
            text += (f" While it was held the machine raised {', '.join(self.step_warnings[:2])}: an interlock "
                     "warning, expected here. It's named after this step in the profile, and the next step waits "
                     "for it to clear.")
        return text

    def _explain_found(self, frames) -> str:
        left = f" ({self.hidden} that change by themselves left out)" if self.hidden else ""
        if any(o.mirror for o in self.offers):
            o = next(o for o in self.offers if o.mirror)
            return (f"The value kept for {o.mirror} moved the other way: it's one control, so it's ticked to make one "
                    f"signal of both directions. Work it and watch, then Keep.{left}")
        if any(o.hint for o in self.offers):
            o = next(o for o in self.offers if o.hint)
            return (f"{o.hint[1]} already knows one of these ('{o.hint[0]}'): it's ticked. Work it and watch, "
                    f"then Keep.{left}")
        if self.offers:
            return f"Work it a few more times and watch the values: tick the one that follows, then Keep.{left}"
        if not frames:
            return "No frames arrived. Is the bus connected?"
        if self.known_moved:
            return f"Nothing new: only values already named moved ({', '.join(self.known_moved[:3])}).{left}"
        if self.hidden:
            return (f"Nothing followed: only values that change by themselves moved{left}. "
                    "Try again a little slower, or skip.")
        return "Nothing followed. " + ("The bus is very quiet (see above)." if self.bus_note
                                       else "Try again, a little slower, or skip.")

    def _follow_pick(self, now: float):
        """While picking: count how often each offer changes as the control is worked, put the
        ones that follow it first (only when no key was pressed for a moment) and tick a clear
        winner unless the technician has ticked something themselves."""
        if not self.offers:
            return
        for o in self.offers:                       # letting go after 'do it' counts: it's the control too
            o.moved = self.watch.transitions(o.key, since=self.picking_since)
            held = self.watch.held_transitions(o.key, self.picking_since) if o.cand.is_switch else o.moved
            o.chatters = o.moved >= CHECKED_MOVES and held * 2 < o.moved
        if not self.sorting_paused(now) and now - self._last_sort >= 1.0:
            order = sorted(self.offers, key=lambda o: (-o.moved, o.rank))
            if order != self.offers:
                self.offers = order
            self._last_sort = now
        if not self._user_ticked:
            top = max(self.offers, key=lambda o: (o.moved, -o.rank))
            clear = (CHECKED_MOVES <= top.moved <= HUMAN_MOVES and not top.chatters
                     and all(o is top or o.moved * 2 <= top.moved for o in self.offers))
            for o in self.offers:
                if o.auto and o is not top:
                    o.keep = o.auto = False
            if clear and not top.keep:
                top.keep = top.auto = True
