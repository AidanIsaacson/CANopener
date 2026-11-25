"""core.suggest: undecoded traffic matched against other profiles' values, each checked against the
data. The machines and profiles here are made up."""

from canopener.can.frame import CanFrame
from canopener.core import fieldwatch, profiles, suggest

HANDLE = {"name": "Truck A", "protocol": "canopen", "detect": {"nodes": ["0x22", "0x20"]}, "signals": [
    {"name": "Lift input", "id": "0x1A2", "start": 3, "length": 1, "signed": True, "unit": "%", "low": -100,
     "high": 100, "group": "Handle"},
    {"name": "Horn", "id": "0x1A2", "start": 0, "bit": 6, "bits": 1, "enum": {"0": "Off", "1": "On"}},
    {"name": "Temperature", "id": "0x290", "start": 4, "length": 1, "unit": "°C", "low": -20, "high": 90},
    {"name": "Speed", "id": "0x190", "start": 6, "length": 2, "signed": True},      # past a 6-byte frame
]}
WRONG = {"name": "Truck B", "protocol": "canopen", "signals": [
    {"name": "Mode", "id": "0x1A2", "start": 3, "length": 1, "enum": {"0": "Off", "1": "Low", "2": "High"}},
]}


def _p(doc, pid):
    return profiles.parse_profile(doc, pid)


def _samples():
    handle = [bytes([0x40 if i % 50 > 40 else 0, 0, 0, (-100 + i * 2) & 0xFF, 0, 0, 0, 0]) for i in range(101)]
    return {("11-bit", 0x1A2, None): handle,
            ("11-bit", 0x290, None): [bytes([0, 0, 0, 0, 20 + i % 3, 0, 0, 0]) for i in range(50)],
            ("11-bit", 0x190, None): [bytes(6)] * 20,
            ("11-bit", 0x722, None): [b"\x05"] * 20, ("11-bit", 0x720, None): [b"\x05"] * 20}


def test_values_that_fit_are_borrowed_and_ones_that_dont_are_left_out():
    a, b = _p(HANDLE, "truck-a"), _p(WRONG, "truck-b")
    out = suggest.suggest(_samples(), [a, b], None, nodes_seen={0x22, 0x20})
    by = {x.label: x for x in out}
    handle = by["ID 1A2"]
    assert handle.profile_id == "truck-a" and [c.signal.name for c in handle.good] == ["Lift input", "Horn"]
    assert all(c.moved for c in handle.good) and handle.nodes == 2
    assert by["ID 290"].good[0].signal.name == "Temperature"
    assert "ID 190" not in by                               # the only value doesn't fit a 6-byte frame
    # Truck B's enum doesn't cover -100..100: judged and refused
    check = suggest.judge(b.signals[0], _samples()[("11-bit", 0x1A2, None)])
    assert not check.ok and "doesn't name" in check.note
    entries = handle.entries()
    assert entries[0]["signed"] and entries[0]["low"] == -100 and entries[0]["observed"]["from"] == "Truck A"
    p = profiles.parse_profile({"name": "Borrowed", "protocol": "canopen", "signals": entries})
    assert p.signals[0].decode(bytes([0, 0, 0, 0x9C, 0, 0, 0, 0])) == -100
    assert suggest.hints(out)["ID 1A2 b3"] == ("Lift input", "Truck A")
    assert suggest.hints(out)["ID 1A2 b0.6"] == ("Horn", "Truck A")


def test_out_of_range_values_count_against_a_profile():
    hot = _p({"name": "Hot", "protocol": "canopen", "signals": [
        {"name": "Coolant", "id": "0x290", "start": 4, "length": 1, "low": 200, "high": 250}]}, "hot")
    assert suggest.suggest(_samples(), [hot]) == []


def test_streams_the_profile_in_use_decodes_and_generic_profiles_are_not_borrowed():
    a = _p(HANDLE, "truck-a")
    mine = _p({"name": "Mine", "protocol": "canopen", "signals": [
        {"name": "Handle byte", "id": "0x1A2", "start": 1, "length": 1}]}, "mine")
    out = suggest.suggest(_samples(), [a, mine], mine)
    assert "ID 1A2" not in {x.label for x in out} and "ID 290" in {x.label for x in out}
    assert suggest.suggest(_samples(), [a], a) == []        # nothing to borrow from yourself
    generic = _p(dict(HANDLE, name="Generic"), "generic-canopen")
    assert suggest.suggest(_samples(), [generic]) == []


def test_the_profile_that_fits_more_of_the_bus_wins_a_stream():
    a = _p(HANDLE, "truck-a")
    one = _p({"name": "One-off", "protocol": "canopen", "signals": [
        {"name": f"Value {i}", "id": "0x290", "start": i, "length": 1} for i in range(8)]}, "one-off")
    out = {x.label: x for x in suggest.suggest(_samples(), [a, one])}
    assert out["ID 290"].profile_id == "truck-a" and out["ID 290"].coverage == 2


def test_from_a_live_watch_and_from_a_log():
    a = _p(HANDLE, "truck-a")
    w = fieldwatch.FieldWatch()
    frames = []
    for i, data in enumerate(_samples()[("11-bit", 0x1A2, None)]):
        frames.append(CanFrame(0x1A2, data, False, 1000 + i * 0.05))
        frames.append(CanFrame(0x722, b"\x05", False, 1000 + i * 0.05))
    for f in frames:
        w.feed(f)
    live = suggest.from_watch(w, [a])
    assert [x.label for x in live] == ["ID 1A2"] and live[0].nodes == 1      # node 22's heartbeat
    assert [x.label for x in suggest.from_frames(frames, [a])] == ["ID 1A2"]


def test_signal_doc_round_trips():
    sig = _p({"name": "J", "signals": [
        {"name": "Pressure", "pgn": "0xFF10", "sa": "0x27", "start": 2, "length": 2, "order": "big", "scale": 0.5,
         "offset": -10, "unit": "kPa", "decimals": 1, "na_check": False, "group": "Hydraulics"}]}, "j").signals[0]
    again = profiles.parse_profile({"name": "J2", "signals": [suggest.signal_doc(sig)]}).signals[0]
    assert again == sig
    assert suggest.field_key(("j1939", 0xFF10, 0x27), sig) == "PGN FF10 from 27 w2be"
