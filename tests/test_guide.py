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


def test_scripts_are_complete_and_cautious():
    assert set(guide.SCRIPTS) == {"Engine truck", "Electric truck", "Something else"}
    for steps in guide.SCRIPTS.values():
        assert len({s.name for s in steps}) == len(steps)
    assert all(s.caution for s in guide.SCRIPTS["Engine truck"] if s.name in ("Lift", "Forward selected"))
    g = guide.Guide(list(guide.SCRIPTS["Engine truck"]))
    g.begin(0.0)
    g.tick(100.0)                                           # no frames at all
    g.tick(200.0)
    g.tick(300.0)
    assert g.state == guide.RESULT and "No frames arrived" in g.message
