"""core.guide: the guided Profile Builder's state machine, with a made-up machine on a virtual clock."""

from canopener.can.frame import CanFrame
from canopener.core import guide, observed, profiles


class Machine:
    """Sends ID 288 (bit 2 of byte 1 = lift, bit 0 = horn), ID 310 (a value that drifts by itself)
    and the standard engine message; feeds a Guide frame by frame on a virtual clock."""

    def __init__(self, g):
        self.g, self.t, self.n = g, 1000.0, 0
        self.lift = self.horn = False
        self.rpm = 0

    def run(self, seconds, **state):
        for k, v in state.items():
            setattr(self, k, v)
        for _ in range(int(seconds * 20)):
            self.t += 0.05
            self.n += 1
            b1 = (4 if self.lift else 0) | (1 if self.horn else 0)
            self.g.feed(CanFrame(0x288, bytes([0, b1, 0, 0, 0, 0, 0, 0]), False, self.t))
            self.g.feed(CanFrame(0x310, bytes([(self.n // 7) % 256, 0, 0, 0, 0, 0, 0, 0]), False, self.t))
            raw = self.rpm * 8
            eec1 = bytes([0xF0, 0x7D, 0x7D, raw & 0xFF, raw >> 8, 0, 0xF0, 0xFF])
            self.g.feed(CanFrame(0x0CF00400, eec1, True, self.t))
            self.g.tick(self.t)


def _guide():
    steps = [guide.Step("Lift", "Raise the forks", "Hydraulics"), guide.Step("Horn", "Press the horn", "Operator"),
             guide.Step("Engine speed", "Hold the revs", "Engine", "rpm", switch=False)]
    return guide.Guide(steps, rest=2.0, do=3.0, quiet=4.0)


def test_guide_walks_the_steps_names_what_followed_and_ignores_what_drifts():
    g = _guide()
    m = Machine(g)
    assert g.state == guide.WAITING and g.prompt()[0] == "Raise the forks"
    g.begin(m.t)
    assert g.state == guide.QUIET and g.prompt()[0] == "Hands off the machine"
    m.run(4.1)                                              # learns that ID 310 moves by itself
    assert g.state == guide.REST and "ID 310 b0" in g.restless and 0 < g.remaining(m.t) <= 2.0
    m.run(2.05)
    assert g.state == guide.DO and g.prompt() == ("Raise the forks", "Hold it there...")
    m.run(3.05, lift=True)
    assert g.state == guide.RESULT
    assert [o.cand.key for o in g.offers] == ["ID 288 b1.2"] and not g.offers[0].again
    assert not g.offers[0].keep                             # nothing is ticked on one sighting alone
    assert "tick the one that follows" in g.message and g.prompt()[0] == "Lift: which one moved?"
    assert g.offers[0].label == "TPDO2: byte 2 bit 3" and g.offers[0].text == "off -> on"
    for lift in (False, True, False, True):                 # the technician works the lever while watching
        m.run(0.6, lift=lift)
    assert g.offers[0].moved == 4 and g.offers[0].keep and g.offers[0].auto
    g.accept()
    m.lift = False
    assert g.index == 1 and g.state == guide.WAITING and g.kept["ID 288 b1.2"]["name"] == "Lift"
    lift = g.kept["ID 288 b1.2"]
    assert lift["group"] == "Hydraulics" and lift["observed"]["sure"] == "checked"
    assert lift["observed"]["evidence"][0].startswith("Picked: it changed 4 times")

    g.begin(m.t)                                            # the horn: nothing pressed the first time
    m.run(2.05)
    m.run(3.05)
    assert g.state == guide.RESULT and not g.offers and "Nothing followed" in g.message
    g.again(m.t)
    m.run(2.05)
    m.run(3.05, horn=True)
    assert [o.cand.key for o in g.offers] == ["ID 288 b1.0"]
    g.again(m.t)                                            # once more: the same bit again
    m.run(2.05, horn=False)
    m.run(3.05, horn=True)
    assert g.offers[0].again
    g.accept()
    assert g.kept["ID 288 b1.0"]["observed"]["sure"] == "seen again"

    g.begin(m.t)                                            # a step whose value is already named
    m.run(2.05, horn=False)
    m.run(3.05, rpm=1500)
    assert not g.offers and "already named" in g.message and "Engine speed" in g.message
    g.skip()
    assert g.state == guide.DONE and g.prompt()[0] == "Finished"
    assert [label for _t, label in g.marks][:4] == ["Hands off (learning)", "Lift: get ready", "Lift", "Lift: let go"]

    doc = observed.build_profile("Guided", g.entries(), profiles.J1939_CANOPEN, "guide")
    p = profiles.parse_profile(doc)
    assert {s.name for s in p.signals} == {"Lift", "Horn"}


def test_guide_offers_a_second_thing_that_followed_and_numbers_it():
    g = guide.Guide([guide.Step("Lift", "Raise", "Hydraulics")], rest=2.0, do=3.0, quiet=4.0)
    m = Machine(g)
    g.begin(m.t)
    m.run(4.1)
    m.run(2.05)
    m.run(3.05, lift=True, horn=True)                       # two bits followed
    assert len(g.offers) == 2 and [o.keep for o in g.offers] == [False, False]
    g.toggle(0)
    g.toggle(1)
    g.accept()
    assert sorted(e["name"] for e in g.entries()) == ["Lift", "Lift 2"] and g.state == guide.DONE


def test_questions_narrow_the_script_and_controls_come_before_the_engine():
    q = guide.next_question({})
    assert q.id == "machine" and {v for _l, v in q.options} == {"ic", "electric", "standup", "telehandler", "cart",
                                                                 "pallet", "other"}
    assert guide.next_question({"machine": "cart"}) is None             # nothing more to ask about a golf cart
    assert guide.next_question({"machine": "pallet"}) is None
    a = {"machine": "ic"}
    asked = []
    while (q := guide.next_question(a)) is not None:
        asked.append(q.id)
        a[q.id] = {"fuel": "lp", "hydraulics": "electronic", "height": "both", "side_shift": True}[q.id]
    assert asked == ["fuel", "hydraulics", "height", "side_shift"]      # aftertreatment is only asked of a diesel
    title, steps = guide.build_script(a)
    names = [s.name for s in steps]
    assert title == "IC forklift, LP, electronic hydraulics" and len(set(names)) == len(names)
    assert names.index("LP fuel pressure switch") < names.index("Engine running") < names.index("Throttle position")
    assert guide.next_question({"machine": "ic", "fuel": "diesel"}).id == "aftertreatment"
    title, diesel = guide.build_script({"machine": "ic", "fuel": "diesel", "aftertreatment": True,
                                        "hydraulics": "manual", "height": "none", "side_shift": False})
    d = [s.name for s in diesel]
    assert title == "IC forklift, diesel with aftertreatment, manual hydraulics"
    assert d.index("Regeneration inhibit switch") < d.index("Engine running") and "Glow plugs" not in d
    assert "Exhaust temperature" in d and "LP fuel pressure switch" not in d and "Throttle position" not in d
    regen = diesel[d.index("Regeneration request switch")]
    assert regen.stage == guide.CONTROLS and "regeneration" in regen.caution
    plain = [s.name for s in guide.build_script({"machine": "ic", "fuel": "diesel", "aftertreatment": False})[1]]
    assert "Regeneration inhibit switch" not in plain and "Engine running" in plain
    # every control input is mapped with the key on, before the engine is started
    start = names.index("Engine running")
    for control in ("Seat switch", "Accelerator pedal", "Lift lever", "Lower lever", "Tilt back lever",
                    "Side shift lever", "Forward selected"):
        assert names.index(control) < start and steps[names.index(control)].stage == guide.CONTROLS
    assert not steps[names.index("Lift lever")].switch and not steps[names.index("Lift lever")].caution
    for after in ("Engine speed", "Lift", "Mast height switch", "Mast height", "Side shift"):
        assert names.index(after) > start and steps[names.index(after)].stage == guide.WORK
    assert steps[names.index("Mast height")].unit == "mm" and steps[names.index("Mast height")].caution
    # manual hydraulics: no lever sensors to look for; no height sensing: no height steps
    _t, manual = guide.build_script({"machine": "ic", "hydraulics": "manual", "height": "none", "side_shift": False})
    manual_names = [s.name for s in manual]
    assert "Lift lever" not in manual_names and "Mast height" not in manual_names and "Side shift" not in manual_names
    assert "Lift" in manual_names


def test_every_kind_of_machine_has_a_sound_script():
    for machine in guide.MACHINES:
        for hyd in ("electronic", "manual", "unsure"):
          for fuel in ("lp", "gas", "diesel", "dual"):
            title, steps = guide.build_script({"machine": machine, "hydraulics": hyd, "height": "both",
                                               "side_shift": True, "fuel": fuel, "aftertreatment": True})
            names = [s.name for s in steps]
            assert title and steps and len(set(names)) == len(names), (machine, hyd, fuel)
            assert all(s.prompt and s.group for s in steps)
    electric = [s.name for s in guide.build_script({"machine": "electric", "hydraulics": "electronic"})[1]]
    assert "Engine running" not in electric and electric.index("Lift lever") < electric.index("Pump motor")
    tele = [s.name for s in guide.build_script({"machine": "telehandler", "hydraulics": "electronic"})[1]]
    assert {"Boom raise lever", "Boom angle", "Boom extension"} <= set(tele)
    assert [s.name for s in guide.build_script({"machine": "pallet"})[1]][:2] == ["Key switch", "Operator present"]
    assert "Charging" in [s.name for s in guide.build_script({"machine": "cart"})[1]]
    g = guide.Guide(guide.build_script({"machine": "ic", "hydraulics": "manual"})[1])
    g.begin(0.0)
    g.tick(100.0)                                           # no frames at all
    g.tick(200.0)
    g.tick(300.0)
    assert g.state == guide.RESULT and "No frames arrived" in g.message


def test_a_quiet_bus_is_called_out_after_the_hands_off_stretch():
    g = _guide()
    g.begin(1000.0)
    for n in range(4):                                      # one frame a second: nearly silent
        g.feed(CanFrame(0x4FC, bytes([0, n, 0, 0, 0, 0, 0, 0]), False, 1000.5 + n))
    g.tick(1004.1)
    assert g.state == guide.REST and "very quiet" in g.bus_note and "1 ID(s)" in g.bus_note
    assert guide.thin_bus([], 4.0).startswith("The bus is very quiet: nothing at all")


def test_a_busy_bus_gets_no_quiet_note():
    g = _guide()
    m = Machine(g)
    g.begin(m.t)
    m.run(4.1)
    assert g.state == guide.REST and g.bus_note == ""


class Truck:
    """A made-up CANopen stand-up: a control handle (ID 1A2, byte 3 = lift / lower as a signed
    percent, byte 0 bit 6 = horn) and a CAN I/O node 20 that raises EMCY 00C8 while the lift input
    is held with nobody on the pedal and clears it (EMCY 0000) when it's let go."""

    def __init__(self, g):
        self.g, self.t = g, 1000.0
        self.lift, self.horn, self.emcy = 0, False, 0

    def run(self, seconds, **state):
        for k, v in state.items():
            setattr(self, k, v)
        for _ in range(int(seconds * 20)):
            self.t += 0.05
            self.g.feed(CanFrame(0x1A2, bytes([0x40 if self.horn else 0, 0, 0, self.lift & 0xFF, 0, 0, 0, 0]),
                                 False, self.t))
            want = 0x00C8 if self.lift else 0
            if want != self.emcy:
                self.emcy = want
                self.g.feed(CanFrame(0x0A0, bytes([want & 0xFF, want >> 8, 1 if want else 0, 0, 0, 0, 0, 0]),
                                     False, self.t))
            self.g.tick(self.t)


def _axis_guide(**kw):
    steps = [guide.Step("Lift input", "Lift", "Hydraulics", "%", False, axis="Lift/lower input", direction="lift"),
             guide.Step("Lower input", "Lower", "Hydraulics", "%", False, axis="Lift/lower input", direction="lower"),
             guide.Step("Horn", "Horn", "Operator")]
    return guide.Guide(steps, rest=2.0, do=3.0, quiet=4.0, **kw)


def test_two_directions_of_one_control_make_one_signed_signal():
    g = _axis_guide()
    m = Truck(g)
    g.begin(m.t)
    m.run(4.1)
    m.run(2.05)
    m.run(3.05, lift=-100)                                  # lift: the byte goes to 0x9C
    assert [o.key for o in g.offers] == ["ID 1A2 b3"]
    g.toggle(0)
    m.run(0.5, lift=0)
    g.accept()
    first = g.kept["ID 1A2 b3"]
    assert "signed" not in first and first["observed"]["held"] == 0x9C and first["observed"]["rest"] == 0
    while g.warnings_now():                                 # (the truck clears its interlock on release)
        m.run(0.1)
    g.begin(m.t)
    m.run(2.05)
    m.run(3.05, lift=100)                                   # lower: the same byte, the other way
    o = g.offers[0]
    assert o.mirror == "Lift input" and o.keep and "moved the other way" in g.message
    m.run(0.5, lift=0)
    g.accept()
    assert len(g.kept) == 1
    e = g.kept["ID 1A2 b3"]
    assert e["name"] == "Lift/lower input (+ lower / - lift)" and e["signed"] and e["observed"]["sure"] == "seen again"
    p = profiles.parse_profile(observed.build_profile("Axis", g.entries(), profiles.CANOPEN, "t"))
    assert p.signals[0].decode(bytes([0, 0, 0, 0x9C, 0, 0, 0, 0])) == -100


def test_interlock_warnings_are_expected_named_and_waited_for():
    g = _axis_guide(title="Electric stand-up, reach truck")
    m = Truck(g)
    g.begin(m.t)
    m.run(4.1)
    m.run(2.05)
    m.run(3.05, lift=-100)                                  # held with nobody on the pedal: EMCY 00C8
    assert g.step_warnings == ["EMCY 00C8 from node 20"] and "interlock warning, expected" in g.message
    assert [o.key for o in g.offers] == ["ID 1A2 b3"]       # the EMCY message itself is never offered
    assert g.emcy_names() == {"0x00C8": "Interlock: raised by Lift input with nobody on the machine; clears when "
                                        "the control is let go?"}
    assert g.emcy_names(known={0x00C8}) == {}
    g.toggle(0)
    g.accept()                                              # the input is still held: the warning stands
    assert g.warnings_now() == ["EMCY 00C8 from node 20"]
    g.begin(m.t)
    assert g.state == guide.SETTLE and g.prompt()[0] == "Let go of everything" and "EMCY 00C8" in g.prompt()[1]
    m.run(1.0)
    assert g.state == guide.SETTLE                          # still held
    m.run(0.5, lift=0)                                      # let go: it clears, and the rest starts
    assert g.state == guide.REST
    s = g.summary()
    assert s["script"] == "Electric stand-up, reach truck" and s["interlocks"]["0x00C8"]["steps"] == ["Lift input"]
    assert s["steps"][0]["step"] == "Lift input" and s["steps"][0]["result"] == "kept"
    assert s["steps"][0]["warnings"] == ["EMCY 00C8 from node 20"]
    assert s["steps"][0]["kept"] == [{"key": "ID 1A2 b3", "name": "Lift input"}]


def test_a_warning_that_wont_clear_doesnt_hold_the_builder_forever():
    g = _axis_guide()
    m = Truck(g)
    g.begin(m.t)
    m.run(4.1)
    m.run(2.05)
    m.run(3.05, lift=-100)
    g.skip()
    assert g.journal[-1]["result"] == "skipped"
    g.begin(m.t)
    assert g.state == guide.SETTLE
    m.run(guide.SETTLE_MAX + 0.5)                           # still held: goes on after a while, and says so
    assert g.state in (guide.REST, guide.DO) and "didn't clear" in g.settle_note


def test_a_field_another_profile_knows_is_offered_first_and_ticked():
    g = _axis_guide(hints={"ID 1A2 b0.6": ("Horn switch", "Some truck")})
    m = Truck(g)
    g.index = 2                                             # straight to the horn
    g.begin(m.t)
    m.run(4.1)
    m.run(2.05)
    m.run(3.05, horn=True)
    o = g.offers[0]
    assert o.key == "ID 1A2 b0.6" and o.hint == ("Horn switch", "Some truck") and o.keep
    assert "Some truck already knows" in g.message
    g.accept()
    assert g.kept["ID 1A2 b0.6"]["observed"]["evidence"][0] == "Some truck calls the same field 'Horn switch'"
    assert g.journal[-1]["offered"][0]["also_known_as"] == ["Horn switch", "Some truck"]


def test_stand_up_scripts_reach_and_counterbalance():
    assert guide.next_question({"machine": "standup"}).id == "standup"
    a = {"machine": "standup", "standup": "reach"}
    asked = []
    while (q := guide.next_question(a)) is not None:        # no hydraulics question: a stand-up's are electronic
        asked.append(q.id)
        a[q.id] = {"height": "encoder", "side_shift": True}[q.id]
    assert asked == ["height", "side_shift"]
    title, steps = guide.build_script(a)
    names = [s.name for s in steps]
    assert title == "Electric stand-up, reach truck" and len(set(names)) == len(names)
    assert names[0] == "Operator pedal" and steps[0].stage == guide.OFF_PEDAL
    for pair in (("Travel forks first", "Travel power unit first"), ("Lift input", "Lower input"),
                 ("Reach out input", "Reach in input"), ("Tilt back input", "Tilt forward input"),
                 ("Side shift left input", "Side shift right input")):
        a1, a2 = (steps[names.index(n)] for n in pair)
        assert a1.axis == a2.axis and a1.direction != a2.direction and not a1.switch and a1.stage == guide.OFF_PEDAL
    assert {"Reach", "Mast height", "Steer angle", "Motor speed"} <= set(names)
    assert steps[names.index("Reach")].stage == guide.ON_PEDAL
    cb = [s.name for s in guide.build_script({"machine": "standup", "standup": "counterbalance", "height": "none",
                                              "side_shift": False})[1]]
    assert not any("Reach" in n for n in cb) and "Side shift left input" not in cb
    assert {"Left drive motor speed", "Right drive motor speed"} <= set(cb) and "Motor speed" not in cb


def test_each_stage_says_what_to_expect_before_its_first_step():
    _t, steps = guide.build_script({"machine": "standup", "standup": "reach", "height": "none", "side_shift": False})
    g = guide.Guide(steps)
    assert "can't drive or lift" in g.stage_note() and "interlock" in g.stage_note()
    g.skip()
    assert g.stage_note() == ""                             # the same stage goes on
    while g.step.stage == guide.OFF_PEDAL:
        g.skip()
    assert "operator pedal" in g.stage_note()


def test_a_warning_standing_before_the_first_step_is_waited_out_once():
    g = _axis_guide()
    m = Truck(g)
    m.run(0.5, lift=-100)                                   # already held when the builder starts
    g.begin(m.t)
    assert g.state == guide.SETTLE
    m.run(guide.SETTLE_MAX + 0.5)
    assert g.state in (guide.QUIET, guide.REST) and "didn't clear" in g.settle_note
