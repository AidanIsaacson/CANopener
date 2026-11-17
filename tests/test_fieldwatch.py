"""core.fieldwatch (every field live), core.profile_match (which profile fits this bus) and the
Profile builder's saving into the profile in use (core.observed)."""

import json
import math
import os

from canopener.can.frame import CanFrame
from canopener.core import changes, fieldwatch, guide, observed, profile_match, profiles


def _bus(fw, seconds=20.0, t0=1000.0, rate=20, **state):
    """A made-up machine: PGN FF10 from 27 carries a seat bit (byte 7 bit 6) and a battery-voltage
    low byte that wobbles between neighbouring numbers (byte 4); EEC1 carries engine speed."""
    seat = state.get("seat", False)
    volt = state.get("volt", (140, 141, 142))
    n = int(seconds * rate)
    for i in range(n):
        t = t0 + i / rate
        prop = bytes([0, 0, 0, 0, volt[(i // 7) % len(volt)], 0, 0, 0x40 if seat else 0])
        fw.feed(CanFrame(0x18FF1027, prop, True, t))
        rpm = int(1000 + 400 * math.sin(i / 30)) * 8         # engine speed going up and down
        fw.feed(CanFrame(0x0CF00400, bytes([0xF0, 0x7D, 0x7D, rpm & 0xFF, rpm >> 8, 0, 0xF0, 0xFF]), True, t))
    return t0 + n / rate


def test_place_words_and_extract():
    assert fieldwatch.place_words("b1.2") == "byte 2 bit 3" and fieldwatch.place_words("w2le") == "bytes 3-4"
    assert fieldwatch.place_words("w0be").endswith("(high byte first)") and fieldwatch.place_words("b0") == "byte 1"
    data = bytes([0x12, 0x34, 0x04])
    assert fieldwatch.extract("w0le", data) == 0x3412 and fieldwatch.extract("w0be", data) == 0x1234
    assert fieldwatch.extract("b2.2", data) == 1 and fieldwatch.extract("b2.1", data) == 0
    assert fieldwatch.extract("b5", data) is None and fieldwatch.extract("nonsense", data) is None
    assert fieldwatch.split_key("PGN FF04 from 27 b1.2") == ("PGN FF04 from 27", "b1.2")


def test_values_history_and_moves():
    fw = fieldwatch.FieldWatch(clock=lambda: 1030.0)
    end = _bus(fw, 10.0)
    end = _bus(fw, 5.0, t0=end, seat=True)
    key = "PGN FF10 from 27 b7.6"
    assert fw.value(key) == 1 and fw.value("PGN FF10 from 27 b7") == 0x40
    assert fw.transitions(key) == 1 and fw.transitions(key, since=end) == 0
    assert fw.transitions(key, intervals=[(1000.0, 1009.0)]) == 0       # nothing happened then
    assert abs(fw.last_change(key) - 1010.0) < 0.1
    assert fw.recent_changes(key, 30) == 1 and fw.age(key) is not None
    assert fw.value("PGN FFFF from 99 b0") is None and fw.last_change("PGN FFFF from 99 b0") is None
    assert [v for _t, v in fw.series(key)][-1] == 1
    assert {k for k, _n, _r in fw.streams()} == {("j1939", 0xFF10, 0x27), ("j1939", 0xF004, 0x00)}


def test_refresh_judges_fields_over_the_window_and_knows_the_standard_names():
    fw = fieldwatch.FieldWatch()
    end = _bus(fw, 10.0)
    _bus(fw, 5.0, t0=end, seat=True)
    assert not fw.judged
    fw.refresh(end + 5)
    keys = {f.key: f for f in fw.fields()}
    assert keys["PGN F004 from 00 w3le"].known == "Engine speed"
    assert keys["PGN FF10 from 27 b7.6"].kind == changes.SWITCH and keys["PGN FF10 from 27 b7.6"].is_bit
    assert keys["PGN FF10 from 27 b4"].kind == changes.VALUE        # three neighbouring numbers: a reading
    assert fw.judged and fw.report() is not None
    stream, f = fw.stream_field("PGN FF10 from 27 b7.6")
    assert stream.label == "PGN FF10 from 27" and f.where == "b7.6"
    assert keys["PGN FF10 from 27 b7.6"].describe() == "PGN FF10 from 27, byte 8 bit 7"


def test_bits_of_a_wobbling_reading_are_not_plausible_switches():
    """The battery voltage's low byte flicking between 140 and 142 is not three switches; the seat
    bit is a switch; a counter is nobody's control."""
    fw = fieldwatch.FieldWatch()
    end = _bus(fw, 10.0)
    fw.refresh(end)
    assert not fw.plausible("PGN FF10 from 27 b4.0") and not fw.plausible("PGN FF10 from 27 b4.1")
    assert fw.plausible("PGN FF10 from 27 b7.6") and fw.plausible("PGN FF10 from 27 b4")
    assert fw.plausible("PGN 1234 from 99 b0.0")              # not heard yet: the benefit of the doubt
    assert not fw.plausible("PGN FF10 from 27 nonsense")
    for i in range(100):                                    # a rolling counter in byte 0 of another message
        fw.feed(CanFrame(0x18FF2027, bytes([i % 256, 0, 0, 0, 0, 0, 0, 0]), True, end + i / 20))
    fw.refresh(end + 5)
    assert not fw.plausible("PGN FF20 from 27 b0")
    fw.clear()
    assert not fw.fields() and fw.frames == 0


def test_refresh_soon_runs_once_in_the_background():
    fw = fieldwatch.FieldWatch()
    _bus(fw, 2.0)
    assert fw.refresh_soon()
    for _ in range(200):
        if fw.judged:
            break
        import time
        time.sleep(0.01)
    assert fw.judged and not fw.refresh_soon(every=60.0)


def test_the_builder_leaves_out_what_changes_while_nobody_touches_the_machine():
    """A bit that flicks during the hands-off stretches isn't offered, however well it lines up
    with one step; a step where only named values moved says so."""
    steps = [guide.Step("Seat switch", "Stand up", "Operator"), guide.Step("Brake pedal", "Press", "Drive")]
    g = guide.Guide(steps, rest=2.0, do=3.0, quiet=4.0)
    t = 1000.0

    def run(seconds, seat=False, flick=False, held=False):
        nonlocal t
        for _i in range(int(seconds * 20)):
            t += 0.05
            b6 = 0x01 if held or (flick and int(t * 4) % 4 == 0) else 0x00     # flicks on by itself now and then
            g.feed(CanFrame(0x18FF1027, bytes([0, 0, 0, 0, 0, 0, b6, 0x40 if seat else 0]), True, t))
            g.feed(CanFrame(0x0CF00400, bytes([0xF0, 0x7D, 0x7D, 0x40, 0x1A, 0, 0xF0, 0xFF]), True, t))
            g.tick(t)
    g.begin(t)
    run(4.1)
    run(2.05)
    run(3.05, seat=True)
    assert [o.key for o in g.offers] == ["PGN FF10 from 27 b7.6"]
    assert g.offers[0].label == "Proprietary B (27 Management computer #1): byte 8 bit 7"
    g.toggle(0)
    assert g.sorting_paused() and g.offers[0].keep and not g.offers[0].auto
    g.accept()
    g.begin(t)
    run(2.05, flick=True)                                   # the 'get ready' sees byte 6 flicking by itself
    run(3.05, held=True)                                    # then it happens to sit on while the pedal is pressed
    assert not g.offers and g.hidden >= 1 and "change by themselves" in g.message
    assert "PGN FF10 from 27 b7.6" in g.kept


def test_profile_match_ranks_profiles_by_their_own_messages_on_the_bus(tmp_path):
    present = {0xFF10: {0x27}, 0xFF04: {0x27}, 0xF004: {0x00}, ("id", 0x181): {0x181}}
    mk = lambda pid, sigs, **kw: profiles.parse_profile({"name": pid, "signals": sigs, **kw}, pid)  # noqa: E731
    fits = mk("fits", [{"name": "a", "pgn": "0xFF10", "sa": "0x27", "start": 0},
                       {"name": "b", "pgn": "0xFF04", "start": 1},
                       {"name": "c", "pgn": "0xF004", "start": 3, "length": 2}])        # standard: doesn't count
    half = mk("half", [{"name": "a", "pgn": "0xFF10", "start": 0}, {"name": "x", "pgn": "0xFF77", "start": 0},
                       {"name": "y", "id": "0x181", "start": 0}])
    other = mk("other", [{"name": "x", "pgn": "0xFF77", "start": 0}, {"name": "y", "pgn": "0xFF78", "start": 0}])
    wrong_sa = mk("wrong-sa", [{"name": "a", "pgn": "0xFF10", "sa": "0x05", "start": 0},
                               {"name": "b", "pgn": "0xFF04", "sa": "0x05", "start": 0}])
    generic = mk("generic", [{"name": "a", "pgn": "0xFF10", "start": 0}, {"name": "b", "pgn": "0xFF04", "start": 0}])
    built = tmp_path / "built.json"
    built.write_text(json.dumps({"name": "built", "signals": [{"name": "a", "pgn": "0xFF10", "start": 7, "bit": 6,
                                                               "bits": 1}],
                                 "observed": {"bus": [["j1939", "0xFF04", "0x27"], ["11-bit", "0x181", None],
                                                      ["j1939", "0xF004", "0x00"]]}}))
    from_builder = profiles.load_profile(str(built))
    got = profile_match.similar([fits, half, other, wrong_sa, generic, from_builder], present)
    assert [m.profile.id for m in got] == ["built", "fits", "half"]
    assert (got[1].matched, got[1].total, round(got[2].share, 2)) == (2, 2, 0.67)
    assert got[1].describe() == "fits: 2 of its 2 messages are on this bus"
    assert profile_match.better_than_active(got, None).profile.id == "built"
    assert profile_match.better_than_active(got, from_builder) is None
    assert profile_match.better_than_active(got, half).profile.id == "built"
    layer = mk("built-additions", [], extends="built")
    assert profile_match.better_than_active(got, layer) is None
    assert profile_match.better_than_active([], fits) is None


def test_names_go_into_the_profile_in_use_or_a_layer_on_top_or_a_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "user_dir", lambda: str(tmp_path))
    entry = {"name": "Seat switch", "pgn": "0xFF10", "sa": "0x27", "start": 7, "bit": 6, "bits": 1,
             "enum": {"0": "Off", "1": "On"}, "observed": {"sure": "checked", "evidence": []}}
    # a built-in profile: a profile on top of it, which extends it
    builtin = profiles.find("sim-telehandler")
    target = observed.in_use_target(builtin)
    assert target.path.endswith("observed-sim-telehandler-additions.json") and target.note
    path = observed.save_entries([entry], builtin, source="test", bus=[["j1939", "0xFF10", "0x27"]])
    doc = json.loads(open(path).read())
    assert doc["extends"] == "sim-telehandler" and doc["signals"][0]["name"] == "Seat switch"
    assert doc["observed"]["bus"] == [["j1939", "0xFF10", "0x27"]]
    mine = profiles.find(os.path.splitext(os.path.basename(path))[0])
    assert {s.name for s in mine.signals} >= {"Seat switch"} | {s.name for s in builtin.signals}
    # that one is now the user's own: the next name goes straight into it
    again = dict(entry, name="Horn", start=6, bit=0)
    assert observed.save_entries([again], mine) == path
    assert [s["name"] for s in json.loads(open(path).read())["signals"]] == ["Seat switch", "Horn"]
    # a new profile is built on the one in use; a name taken gets a number
    new = observed.save_entries([entry], mine, new_name="Hyster test")
    new2 = observed.save_entries([entry], mine, new_name="Hyster test")
    assert json.loads(open(new).read())["extends"] == mine.id and new2.endswith("hyster-test-2.json")
    assert json.loads(open(new2).read())["name"] == "Hyster test 2"
    # nothing worth adding to (no profile, or a generic one): a new profile on its own
    assert observed.in_use_target(None) is None and observed.in_use_target(profiles.find("generic")) is None
    lone = observed.save_entries([entry], profiles.find("generic"))
    assert json.loads(open(lone).read())["extends"] == "generic"


def test_a_bit_is_only_named_when_a_decoder_names_that_bit_or_its_whole_value(monkeypatch):
    p = profiles.parse_profile({"name": "x", "signals": [
        {"name": "Park brake", "pgn": "0xFF10", "start": 7, "bit": 2, "bits": 1},
        {"name": "Battery", "pgn": "0xFF10", "start": 4, "length": 2, "scale": 0.05, "unit": "V"}]}, "x")
    profiles.apply(p)
    try:
        frames = [CanFrame(0x18FF1027, bytes([0, 0, 0, 0, 140 + (i % 3), 0, 0, (i % 2) * 0x14]), True, 1000 + i / 10)
                  for i in range(40)]
        r = changes.analyze(frames, [changes.Phase("all", 1000, 1005)])
        st = next(s for s in r.streams if s.label == "PGN FF10 from 27")
        f = lambda where: changes.Field(where, changes.SWITCH, [])  # noqa: E731
        assert observed.known_name(st, f("b7.2")) == "Park brake"
        assert observed.known_name(st, f("b7.4")) == ""             # the other bits of that byte are unnamed
        assert observed.known_name(st, f("b4.0")) == "Battery" and observed.known_name(st, f("b5")) == "Battery"
        assert observed.known_name(st, f("w4le")) == "Battery" and observed.known_name(st, f("b0")) == ""
    finally:
        profiles.apply(None)


def test_a_bouncing_bit_is_flagged_and_not_ticked_for_you():
    """A person holds each position; a contact that bounces (on, off, on within a tenth of a
    second) is shown with 'bounces' and never ticked for the technician."""
    fw = fieldwatch.FieldWatch(clock=lambda: 1010.0)
    pattern = [(1000.0, 0), (1001.0, 1), (1001.05, 0), (1001.1, 1), (1001.15, 0), (1001.2, 1), (1003.0, 0)]
    for t, v in pattern:
        fw.feed(CanFrame(0x18FF5527, bytes([0, 0, 0, 0, 0, 0, 0, v]), True, t))
    key = "PGN FF55 from 27 b7.0"
    assert fw.transitions(key, since=1000.5) == 6 and fw.held_transitions(key, 1000.5) == 2

    g = guide.Guide([guide.Step("Seat switch", "Stand up", "Operator")], rest=1.0, do=1.0, quiet=2.0)
    t = 2000.0

    def run(seconds, value_at):
        nonlocal t
        for _i in range(int(seconds * 50)):
            t += 0.02
            g.feed(CanFrame(0x18FF5527, bytes([0, 0, 0, 0, 0, 0, 0, value_at(t)]), True, t))
            g.tick(t)
    g.begin(t)
    run(2.05, lambda t: 0)
    run(1.0, lambda t: 0)
    run(1.05, lambda t: 1)
    assert g.state == guide.RESULT and [o.key for o in g.offers] == [key]
    run(2.0, lambda t: int(t * 25) % 2)                      # flicking every 40 ms
    assert g.offers[0].chatters and not g.offers[0].keep and g.offers[0].moved >= 4
