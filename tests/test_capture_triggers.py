"""core.capture_triggers: timestamps the bus sets (a fault code appearing, a picked button pressed).
The machine and its profile are made up."""

from canopener.can.frame import CanFrame
from canopener.core import capture_triggers as trig
from canopener.core import profiles
from canopener.core.capture import Capture
from canopener.core.settings import Settings

PROFILE = profiles.parse_profile({"name": "Truck", "protocol": "canopen", "signals": [
    {"name": "Function button", "id": "0x1A2", "start": 0, "bit": 7, "bits": 1, "enum": {"0": "Off", "1": "On"}},
    {"name": "Mode", "id": "0x1A2", "start": 1, "length": 1, "enum": {"0": "Idle", "1": "Drive", "2": "Lift"}},
    {"name": "Speed", "id": "0x188", "start": 0, "length": 2},
]})


def _handle(t, button=False, mode=0):
    return CanFrame(0x1A2, bytes([0x80 if button else 0, mode, 0, 0, 0, 0, 0, 0]), False, t)


def test_a_picked_button_sets_a_timestamp_on_each_press_with_a_hold_off():
    w = trig.Watch([trig.Trigger(trig.SIGNAL, "Function button")], PROFILE.signals)
    w.feed(_handle(100.0, button=True))                     # already held at the start: not a press
    assert w.take() == []
    for t, on in ((100.5, False), (101.0, True), (101.5, False), (102.0, True), (104.0, False), (104.5, True)):
        w.feed(_handle(t, button=on))
    got = w.take()
    assert [t for t, _l, _n in got] == [101.0, 104.5]        # 102.0 fell inside the 2 s hold-off
    assert got[0][1] == "Function button pressed" and "went to On" in got[0][2]


def test_a_state_trigger_fires_when_it_reaches_that_value():
    w = trig.Watch([trig.Trigger(trig.SIGNAL, "Mode", 2.0)], PROFILE.signals)
    for t, mode in ((10.0, 0), (11.0, 1), (12.0, 2), (12.5, 2), (13.0, 0)):
        w.feed(_handle(t, mode=mode))
    assert [(t, label) for t, label, _n in w.take()] == [(12.0, "Mode: Lift")]
    assert trig.Trigger(trig.SIGNAL, "Mode", 2.0).label == "Mode = 2"


def test_fault_codes_appearing_set_timestamps():
    w = trig.Watch([trig.Trigger(trig.FAULT)], PROFILE.signals)
    emcy = lambda code, t: CanFrame(0x0A0, bytes([code & 0xFF, code >> 8, 1, 0, 0, 0, 0, 0]), False, t)  # noqa: E731
    w.feed(emcy(0x00C8, 1.0))
    w.feed(emcy(0x00C8, 1.5))                               # the same code again: nothing new
    w.feed(emcy(0x0000, 2.0))                               # cleared
    w.feed(emcy(0x00C8, 3.0))                               # back: new again
    got = w.take()
    label = "Fault: EMCY 00C8 node 20"
    assert [(t, lb) for t, lb, _n in got] == [(1.0, label), (3.0, label)]
    assert "node 20 raised EMCY 00C8" in got[0][2]
    # J1939 DM1 (broadcast every second): codes on in the first one aren't news, a new one is
    dm1 = lambda spns, t: CanFrame(0x18FECA00, bytes([0, 0xFF]) + b"".join(  # noqa: E731
        bytes([spn & 0xFF, spn >> 8 & 0xFF, (spn >> 16 & 7) << 5 | 3, 1]) for spn in spns)[:6].ljust(6, b"\0"),
        True, t)
    w.feed(dm1([100], 10.0))
    w.feed(dm1([100], 11.0))
    assert w.take() == []
    w.feed(dm1([110], 12.0))
    assert [label for _t, label, _n in w.take()] == ["Fault: SPN 110 FMI 3"]


def test_settings_round_trip_missing_signals_and_candidates(tmp_path):
    s = Settings(str(tmp_path / "settings.json"))
    triggers = [trig.Trigger(trig.FAULT), trig.Trigger(trig.SIGNAL, "Function button"),
                trig.Trigger(trig.SIGNAL, "Mode", 2.0)]
    trig.save(s, triggers)
    assert trig.load(s) == triggers
    s.set(trig.SETTING, [{"kind": "signal"}, {"kind": "bogus"}, "x"])     # junk is dropped, not fatal
    assert trig.load(s) == []
    w = trig.Watch([trig.Trigger(trig.SIGNAL, "Not on this profile")], PROFILE.signals)
    assert [t.signal for t in w.missing] == ["Not on this profile"]
    assert [x.name for x in trig.candidates(PROFILE.signals)] == ["Function button", "Mode"]


def test_apply_adds_named_timestamps_with_what_set_them():
    c = Capture()
    trig.apply(c, [(5.0, "Function button pressed", "Set by the bus: Function button went to On")])
    m = c.markers[-1]
    assert (m.label, m.t, m.note) == ("Function button pressed", 5.0, "Set by the bus: Function button went to On")
