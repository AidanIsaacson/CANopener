"""User labels (core.labeling), profile 'extends', and 'Find what changed' (core.analyze)."""

import json
import os

import pytest

from canopener.can.frame import CanFrame
from canopener.core import analyze, labeling, profiles
from canopener.j1939 import addresses, signals
from canopener.j1939.ids import build_id


def _profile(pid):
    return next(p for p in profiles.available_profiles()[0] if p.id == pid)


def test_new_profile_extends_builtin_and_adds_labels():
    base = _profile("sim-telehandler")
    before = open(base.path, encoding="utf-8").read()
    pid = labeling.create("My telehandler", base)
    labeling.set_module_name(pid, 0x2B, "Traction controller")
    labeling.put_signal(pid, {"name": "Pedal", "pgn": 0xFF10, "sa": 0x27, "start": 5, "length": 1,
                              "scale": 0.5, "unit": "%", "group": "Mine"})
    p = _profile(pid)
    assert p.extends == "sim-telehandler" and profiles.origin(p) == "user" and labeling.is_editable(p)
    assert not labeling.is_editable(base)
    assert p.sources[0x27] == base.sources[0x27] and p.sources[0x2B] == "Traction controller"
    names = {s.name for s in p.signals}
    assert {"Pedal", "Seat switch", "Boom position raw"} <= names
    profiles.apply(p)
    assert addresses.sa_name(0x2B, with_hex=False) == "Traction controller"
    pedal = next(s for s in signals.all_signals() if s.name == "Pedal")
    assert pedal.decode(bytes([0, 0, 0, 0, 0, 100, 0, 0])) == 50.0
    assert open(base.path, encoding="utf-8").read() == before           # built-in untouched
    doc = labeling.read_doc(pid)
    assert doc["signals"][0] == {"name": "Pedal", "pgn": "0xFF10", "sa": "0x27", "start": 5, "length": 1,
                                 "scale": 0.5, "unit": "%", "group": "Mine"}


def test_override_and_delete_restores_base():
    base = _profile("sim-telehandler")
    pid = labeling.create("Mine", base)
    labeling.put_signal(pid, {"name": "Boom angle", "pgn": "0xFF10", "sa": "0x27", "start": 1, "length": 2,
                              "scale": 0.1, "unit": "deg"})
    p = _profile(pid)
    at = [s for s in p.signals if s.location == (0xFF10, 1, 0, 16)]
    assert [s.name for s in at] == ["Boom angle"]
    labeling.put_signal(pid, {"name": "Boom angle 2", "pgn": "0xFF10", "sa": "0x27", "start": 1, "length": 2})
    assert [s.name for s, _ in labeling.own_signals(pid)] == ["Boom angle 2"]    # same place: replaced
    key = labeling.own_signals(pid)[0][0].key
    assert labeling.delete_signal(pid, key)
    at = [s.name for s in _profile(pid).signals if s.location == (0xFF10, 1, 0, 16)]
    assert at == ["Boom position raw"]
    labeling.set_module_name(pid, 0x27, "Renamed")
    assert _profile(pid).sources[0x27] == "Renamed"
    labeling.set_module_name(pid, 0x27, "")
    assert _profile(pid).sources[0x27] == base.sources[0x27]


def test_save_as_keeps_several_profiles():
    a = labeling.create("Truck A", _profile("generic"))
    labeling.set_module_name(a, 0x2B, "Pump controller")
    b = labeling.save_as(_profile(a), "Truck B")
    assert a != b
    labeling.set_module_name(b, 0x2B, "Traction controller")
    assert _profile(a).sources[0x2B] == "Pump controller"
    assert _profile(b).sources[0x2B] == "Traction controller" and _profile(b).name == "Truck B"
    c = labeling.save_as(_profile("obd2"), "My car")            # from a built-in: built on it
    assert _profile(c).extends == "obd2" and _profile(c).protocol == profiles.OBD2
    labeling.rename(a, "Truck A (yard)")
    assert _profile(a).name == "Truck A (yard)"
    labeling.delete_profile(c)
    assert c not in {p.id for p in profiles.available_profiles()[0]}


def test_unknown_keys_survive_and_bad_signals_are_refused():
    pid = labeling.create("Keep", None)
    doc = labeling.read_doc(pid)
    doc["my_notes"] = {"keep": True}
    labeling.write_doc(pid, doc)
    labeling.set_module_name(pid, 0x10, "X")
    assert labeling.read_doc(pid)["my_notes"] == {"keep": True}
    with pytest.raises(ValueError):
        labeling.put_signal(pid, {"name": "Bad", "pgn": "0xFF00", "start": 0, "length": 3})
    with pytest.raises(ValueError):
        labeling.put_signal(pid, {"name": " ", "pgn": "0xFF00", "start": 0, "length": 1})
    assert not os.path.exists(labeling.path_for(pid) + ".tmp")


def test_missing_base_or_corrupt_file_degrades_with_a_message():
    pid = labeling.create("Orphan", _profile("generic"))
    doc = labeling.read_doc(pid)
    doc["extends"] = "not-installed"
    labeling.write_doc(pid, doc)
    with open(os.path.join(profiles.user_dir(), "broken.json"), "w") as f:
        f.write("{not json")
    found, errors = profiles.available_profiles()
    assert pid in {p.id for p in found} and _profile(pid).protocol == profiles.J1939
    assert any("not-installed" in e for e in errors) and any("broken.json" in e for e in errors)


def test_detect_export_import_and_preview(tmp_path):
    pid = labeling.create("Exported", _profile("generic"))
    assert labeling.set_detect(pid, [0x27, 0x00, 0x2B]) == [0x00, 0x27, 0x2B]
    p = _profile(pid)
    assert p.detect_addresses == {0x00, 0x27, 0x2B} and p.detect_min == 2
    path = labeling.export_to(pid, str(tmp_path))
    assert json.load(open(path))["name"] == "Exported"
    new = labeling.import_file(path)
    assert new != pid and _profile(new).name == "Exported"
    v, text = labeling.preview({"pgn": "0xFF10", "start": 1, "length": 2, "scale": 0.1, "unit": "bar",
                                "decimals": 1}, bytes([0, 0x10, 0x02, 0, 0, 0, 0, 0]))
    assert v == pytest.approx(52.8) and text == "52.8 bar"
    assert labeling.preview({"pgn": "0xFF10", "start": 0, "bit": 3, "bits": 1, "enum": {"0": "Off", "1": "On"}},
                            bytes([0x08]))[1] == "On"


def _frames(cid, payloads, t0, ext=True):
    return [CanFrame(cid, bytes(p), ext, t0 + i * 0.1) for i, p in enumerate(payloads)]


def test_find_what_changed_ranks_the_moved_data_first():
    cid = build_id(0xFF10, 0x27, 0xFF, priority=6)
    other = build_id(0xFF22, 0x27, 0xFF, priority=6)
    frames = []
    for i in range(20):                                     # window A: pedal up
        frames += _frames(cid, [[i % 256, 0x10, 0, 0x00, 7, i * 13 % 256, 0, 0]], 100 + i * 0.1)
        frames += _frames(other, [[i, i * 7 % 256, 0, 0, 0, 0, 0, 0]], 100.05 + i * 0.1)
        frames += _frames(0x1A3, [[0x20 + (i % 2), 0x05]], 100.07 + i * 0.1, ext=False)
    for i in range(20):                                     # window B: pedal down
        frames += _frames(cid, [[(20 + i) % 256, 0xC8, 0, 0x04, 7, i * 13 % 256, 0, 0]], 200 + i * 0.1)
        frames += _frames(other, [[20 + i, i * 7 % 256, 0, 0, 0, 0, 0, 0]], 200.05 + i * 0.1)
        frames += _frames(0x1A3, [[0x80 + (i % 2), 0x05]], 200.07 + i * 0.1, ext=False)
    frames.sort(key=lambda f: f.timestamp)
    found = analyze.find_changes(frames, (100, 102), (200, 202))
    top = {(c.can_id, c.start, c.bit, c.bits) for c in found[:3]}
    assert (cid, 1, 0, 0) in top                            # 0x10 -> 0xC8, steady both times
    assert (cid, 3, 2, 1) in top                            # one switch: offered as a bit
    assert any(c.can_id == 0x1A3 and not c.extended and c.start == 0 for c in found)   # 11-bit too
    assert not any(c.start == 5 and c.can_id == cid and not c.bits for c in found)     # noise: not ranked
    assert not any(c.start == 0 and c.extended for c in found)                        # rolling counters

    best = found[0]
    assert best.pgn == 0xFF10 and best.sa == 0x27 and "pgn" in best.signal_draft()


def test_tag_windows():
    w = analyze.tag_windows([("2 - down", 50.0), ("1 - up", 10.0)], end=55.0)
    assert w == [("1 - up", 10.0, 10.0 + analyze.WINDOW_MAX), ("2 - down", 50.0, 55.0)]
