"""The missing-module check (core.roster): modules a profile expects vs. what's heard, modules that
went quiet, the strict 'heard from it' rule for CANopen, and saving the list from the bus."""

import time

from canopener.can.backends import VirtualBackend, VirtualBus
from canopener.can.frame import CanFrame
from canopener.core import canopen as co
from canopener.core import labeling, profiles, roster
from canopener.core.session import Session
from canopener.j1939.ids import build_id
from canopener.sim.canopen import PUMP, TRACTION, CanopenSimulator

T0 = 1_000_000.0


def _profile(doc: dict) -> profiles.Profile:
    return profiles.parse_profile({"name": "Test truck", **doc}, "test-truck")


def _j1939(s: Session, sa: int, t0: float, t1: float, step: float = 0.1, pgn: int = 0xF004):
    t = t0
    while t <= t1:
        s.feed(CanFrame(build_id(pgn, sa), bytes(8), True, t))
        t += step


def _std(s: Session, can_id: int, t0: float, t1: float, step: float = 0.1, data: bytes = bytes(8)):
    t = t0
    while t <= t1:
        s.feed(CanFrame(can_id, data, False, t))
        t += step


def test_expected_list_comes_from_detect_or_expect():
    p = _profile({"detect": {"addresses": ["0x00", "0x27"]}})
    assert p.expect_addresses == {0x00, 0x27}
    p = _profile({"detect": {"addresses": ["0x00", "0x27"]}, "expect": {"addresses": ["0x00", "0x03"]}})
    assert p.detect_addresses == {0x00, 0x27} and p.expect_addresses == {0x00, 0x03}
    co_p = _profile({"protocol": "canopen", "detect": {"nodes": ["0x08", "0x60"]}})
    assert roster.expected_modules(co_p) == [co.node_src(0x08), co.node_src(0x60)]
    # a profile built on another inherits its list, and its own detect list replaces it
    base = _profile({"expect": {"addresses": ["0x00"]}})
    assert profiles.merged(base, profiles.parse_profile({"name": "mine", "extends": "x"}, "mine")).expect_addresses \
        == {0x00}
    own = profiles.parse_profile({"name": "mine", "detect": {"addresses": ["0x27"]}}, "mine")
    assert profiles.merged(base, own).expect_addresses == {0x27}


def test_missing_and_quiet_j1939_modules():
    p = _profile({"sources": {"0x27": "Handle"}, "detect": {"addresses": ["0x00", "0x27", "0x03"]}})
    profiles.apply(p)
    try:
        s = Session(None)
        _j1939(s, 0x00, T0, T0 + 10)            # engine: the whole time
        _j1939(s, 0x27, T0, T0 + 4)             # the handle: stops after 4 s
        r = roster.check(s, p)
        by = {m.src: m for m in r.modules}
        assert by[0x00].status == roster.ON
        assert by[0x27].status == roster.QUIET and by[0x27].age > 5 and abs(by[0x27].period - 0.1) < 0.01
        assert by[0x03].status == roster.MISSING and by[0x03].expected
        assert [m.src for m in r.problems] == [0x27, 0x03]      # quiet first, then missing
        text = r.summary()
        assert text.startswith("1 of 3 expected modules on the bus.")
        assert "Went quiet: 27 Handle." in text and "Not heard: 03" in text
        assert "only answers when asked" in r.hint()
    finally:
        profiles.apply(None)


def test_unlisted_modules_and_no_list():
    s = Session(None)
    _j1939(s, 0x00, T0, T0 + 10)
    _j1939(s, 0x17, T0, T0 + 3)
    r = roster.check(s, _profile({}))
    assert not r.has_list and [m.src for m in r.problems] == [0x17]       # quiet even without a list
    assert r.summary().startswith("1 module(s) on the bus.") and "Remember these modules" in r.hint()
    assert next(m for m in r.modules if m.src == 0x00).text == "On the bus"              # no list: no remark
    listed = roster.check(s, _profile({"detect": {"addresses": ["0x17"]}}))
    assert next(m for m in listed.modules if m.src == 0x00).text == "On the bus (not in the profile's list)"
    assert "(it sent every 100 ms)" in next(m for m in listed.modules if m.src == 0x17).text


def test_slow_and_one_off_modules_are_not_quiet():
    s = Session(None)
    _j1939(s, 0x00, T0, T0 + 10)
    _j1939(s, 0x21, T0, T0 + 9, step=1.0)       # 1 Hz, last heard 1 s ago: fine
    s.feed(CanFrame(build_id(0xEE00, 0x33), bytes(8), True, T0 + 1))   # answered one request: no rhythm
    r = roster.check(s, _profile({}))
    assert not r.problems


def test_waiting_then_missing_and_whole_bus_quiet():
    p = _profile({"detect": {"addresses": ["0x00", "0x27"]}})
    s = Session(None)
    _j1939(s, 0x00, T0, T0 + 1)
    r = roster.check(s, p)
    assert r.listening and next(m for m in r.modules if m.src == 0x27).status == roster.WAITING
    assert not r.problems and "Still listening" in r.summary()
    _j1939(s, 0x00, T0 + 1, T0 + 5)
    assert [m.status for m in roster.check(s, p).problems] == [roster.MISSING]
    r = roster.check(s, p, now=T0 + 30)         # nothing for 25 s: the bus, not every module
    assert r.bus_quiet and not r.problems and "is the machine on?" in r.summary()
    assert roster.check(Session(None), p).summary() == "Nothing heard on the bus yet."


def test_canopen_node_counts_only_what_it_sends():
    """A master keeps sending RPDOs to a dead tiller head: that doesn't make it present."""
    p = _profile({"protocol": "canopen", "nodes": {"0x60": "Tiller head"},
                  "detect": {"nodes": ["0x08", "0x60", "0x70"]}})
    profiles.apply(p)
    try:
        s = Session(None)
        _std(s, 0x388, T0, T0 + 10)             # TPDO3 of node 08: no heartbeat, still counts
        _std(s, 0x1E0, T0, T0 + 3)              # the tiller head's TPDO1 stops at 3 s ...
        _std(s, 0x260, T0, T0 + 10)             # ... the master's RPDO1 to it carries on
        _std(s, 0x270, T0, T0 + 10)             # the display is only ever sent to
        by = {m.node: m for m in roster.check(s, p).modules}
        assert by[0x08].status == roster.ON
        assert by[0x60].status == roster.QUIET and by[0x60].addressed and "still on the bus" in by[0x60].text
        assert by[0x70].status == roster.MISSING and "nothing from it" in by[0x70].text
        assert by[0x60].name == "Node 60 Tiller head"
        # a PDO-range ID from no listed node is plain 11-bit traffic, not a module
        _std(s, 0x190, T0, T0 + 10)
        assert 0x10 not in {m.node for m in roster.check(s, p).modules}
        assert roster.heard_modules(s, profiles.CANOPEN) == ([], [0x08, 0x10, 0x60])
    finally:
        profiles.apply(None)


def test_j1939_profile_ignores_canopen_and_back():
    s = Session(None)
    _j1939(s, 0x00, T0, T0 + 5)
    _std(s, 0x701, T0, T0 + 5, data=b"\x05")     # node 01 heartbeat
    j = roster.check(s, _profile({"detect": {"addresses": ["0x00"], "nodes": ["0x01", "0x02"]}}))
    assert [m.src for m in j.modules] == [0x00]
    c = roster.check(s, _profile({"protocol": "canopen", "detect": {"addresses": ["0x00"], "nodes": ["0x01"]}}))
    assert [m.src for m in c.modules] == [co.node_src(1)]


def test_recognise_this_machine_saves_the_check_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    pid = labeling.create("My truck", _profile({"protocol": "canopen"}))
    doc = labeling.read_doc(pid)
    doc["expect"] = {"nodes": ["0x05"]}
    labeling.write_doc(pid, doc)
    saved = labeling.set_detect(pid, [], [0x08, 0x60])
    assert saved == [co.node_src(0x08), co.node_src(0x60)]
    p = profiles.load_profile(labeling.path_for(pid))
    assert p.detect_nodes == p.expect_nodes == {0x08, 0x60}       # its own expect list follows


def test_live_session_against_the_canopen_simulator():
    bus = VirtualBus()
    sim = CanopenSimulator(VirtualBackend(bus))
    s = Session(VirtualBackend(bus))
    p = _profile({"protocol": "canopen", "detect": {"nodes": [f"0x{TRACTION:02X}", f"0x{PUMP:02X}", "0x7E"]}})
    sim.start()
    s.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            r = roster.check(s, p)
            if not r.listening and {m.node for m in r.modules if m.status == roster.ON} >= {TRACTION, PUMP}:
                break
            time.sleep(0.1)
        r = roster.check(s, p)
        assert [m.node for m in r.problems] == [0x7E], r.summary()
    finally:
        s.stop()
        sim.stop()


def test_alerts_say_each_change_once():
    p = _profile({"sources": {"0x27": "Handle"}, "detect": {"addresses": ["0x00", "0x27", "0x03"]}})
    profiles.apply(p)
    try:
        s = Session(None)
        a = roster.Alerts()
        _j1939(s, 0x00, T0, T0 + 1)
        _j1939(s, 0x27, T0, T0 + 1)
        assert a.update(roster.check(s, p)) == ([], False)                 # still listening
        _j1939(s, 0x00, T0 + 1, T0 + 5)
        _j1939(s, 0x27, T0 + 1, T0 + 2)
        said, bad = a.update(roster.check(s, p))
        assert said == ["27 Handle went quiet", "Not heard: 03 Transmission #1"] and bad
        assert a.update(roster.check(s, p)) == ([], False)                 # once
        _j1939(s, 0x27, T0 + 5, T0 + 5.5)
        _j1939(s, 0x00, T0 + 5, T0 + 5.5)
        assert a.update(roster.check(s, p)) == (["27 Handle is back"], False)
        # the whole bus going quiet (key off) isn't every module's fault, and no 'back' for all after
        assert a.update(roster.check(s, p, now=T0 + 60)) == ([], False)
    finally:
        profiles.apply(None)


def test_remember_saves_into_your_profile_or_a_new_one():
    s = Session(None)
    _j1939(s, 0x00, T0, T0 + 2)
    _j1939(s, 0x27, T0, T0 + 2)
    base = profiles.find("generic")
    pid, found = roster.remember(s, base)
    mine = profiles.load_profile(labeling.path_for(pid))
    assert mine.name == "My Generic J1939" and mine.extends == "generic" and found == [0x00, 0x27]
    assert mine.expect_addresses == {0x00, 0x27}
    _j1939(s, 0x03, T0 + 2, T0 + 3)
    mine.path = labeling.path_for(pid)
    assert roster.remember(s, mine) == (pid, [0x00, 0x03, 0x27])          # your own: saved in place


def test_settles_on_any_traffic_even_when_none_of_it_is_the_profiles():
    """A CANopen profile on a J1939-only bus: the listed nodes are missing, not 'still listening'."""
    s = Session(None)
    _j1939(s, 0x00, T0, T0 + 6)
    r = roster.check(s, _profile({"protocol": "canopen", "detect": {"nodes": ["0x01"]}}))
    assert not r.listening and [m.status for m in r.modules] == [roster.MISSING]
