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
    assert [o.cand.key for o in g.offers] == ["ID 288 b1.2"] and g.offers[0].keep and not g.offers[0].again
    assert "off -> on" in g.message and g.prompt()[0] == "Lift: found"
    g.accept()
    m.lift = False
    assert g.index == 1 and g.state == guide.WAITING and g.kept["ID 288 b1.2"]["name"] == "Lift"
    assert g.kept["ID 288 b1.2"]["group"] == "Hydraulics" and g.kept["ID 288 b1.2"]["observed"]["sure"] == "seen once"

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
    assert [label for _t, label in g.marks][:2] == ["Lift", "(rest)"]

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
    assert len(g.offers) == 2 and [o.keep for o in g.offers] == [True, False]
    g.toggle(1)
    g.accept()
    assert sorted(e["name"] for e in g.entries()) == ["Lift", "Lift 2"] and g.state == guide.DONE


def test_questions_narrow_the_script_and_controls_come_before_the_engine():
    q = guide.next_question({})
    assert q.id == "machine" and {v for _l, v in q.options} == {"ic", "electric", "telehandler", "cart", "pallet",
                                                                 "other"}
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
    assert d.index("Regeneration inhibit switch") < d.index("Glow plugs") < d.index("Engine running")
    assert "Exhaust temperature" in d and "LP fuel pressure switch" not in d and "Throttle position" not in d
    regen = diesel[d.index("Regeneration request switch")]
    assert regen.stage == guide.CONTROLS and "regeneration" in regen.caution
    plain = [s.name for s in guide.build_script({"machine": "ic", "fuel": "diesel", "aftertreatment": False})[1]]
    assert "Regeneration inhibit switch" not in plain and "Glow plugs" in plain
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
