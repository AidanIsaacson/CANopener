"""The capture engine: tagging rules, save / recover, and the generated summary."""

import csv
import json
import os
import time

import pytest

from canopener.can.backends import VirtualBackend, VirtualBus
from canopener.core import capture as cap
from canopener.core.capture import Capture
from canopener.core.session import Session
from canopener.sim import MachineSimulator

PLAN = ["key on", "start", "drive", "shutdown"]


def test_tag_next_then_generic_after_plan_runs_out():
    c = Capture(plan=PLAN)
    assert [c.tag_next(t) for t in (1, 2, 3, 4)] == [0, 1, 2, 3]
    assert c.tag_next(5) == 4 and c.title(4) == "5 - Timestamp 5" and not c.markers[4].planned
    assert c.next_title() == "6 - Timestamp 6"


def test_number_key_tags_directly_and_never_overwrites():
    c = Capture(plan=PLAN)
    assert c.tag(3, 10.0) == 3                          # "4" pressed first
    assert c.tag_next(11.0) == 0                        # Space still goes to the first untagged
    assert c.tag(3, 12.0) == 4                          # "4" again: a repeat, not an overwrite
    assert c.markers[3].t == 10.0 and c.title(4) == "5 - shutdown (2)"
    assert c.tag(3, 13.0) == 5 and c.markers[5].label == "shutdown (3)"
    assert c.tag(20, 14.0) is None                      # no timestamp 21: ignored


def test_undo_last_untags_planned_and_removes_extras():
    c = Capture(plan=PLAN)
    c.tag_next(1)
    c.tag(0, 2)                                          # repeat -> extra marker
    assert c.undo_last() == 1 and len(c.markers) == 4
    assert c.undo_last() == 0 and not c.markers[0].tagged
    assert c.undo_last() is None


def test_phase_and_editing():
    c = Capture(plan=PLAN)
    c.started = 0
    c.tag(0, 1)
    c.tag(1, 5)
    assert c.phase_at(0.5) == "" and c.phase_at(3) == "1 - key on" and c.phase_at(9) == "2 - start"
    c.rename(1, "crank")
    c.move(1, 0)
    assert c.title(0) == "1 - crank"
    c.delete(0)
    assert c.plan_labels() == ["key on", "drive", "shutdown"]


def test_idle_add_and_fresh_copy_keeps_plan_only():
    c = Capture("job", "notes", PLAN)
    assert c.add_planned() == 4 and c.markers[4].label == "Timestamp 5"
    c.tag(0, 1)
    c.tag(0, 2)
    f = c.fresh_copy()
    assert f.plan_labels() == PLAN + ["Timestamp 5"] and not any(m.tagged for m in f.markers)
    assert (f.name, f.notes, f.state) == ("job", "notes", cap.IDLE)


def test_templates_round_trip():
    assert "Intermittent shutdown" in cap.load_templates()
    cap.save_template("Mine", ["a", "b"])
    assert cap.load_templates()["Mine"] == ["a", "b"]
    assert cap.delete_template("Mine") and "Mine" not in cap.load_templates()
    assert not cap.delete_template("Intermittent shutdown")      # built-ins stay


@pytest.fixture
def bus_and_session():
    bus = VirtualBus()
    sim = MachineSimulator(VirtualBackend(bus), seed=1)
    sim.start()
    s = Session(VirtualBackend(bus))
    s.start()
    yield bus, sim, s
    s.stop()
    sim.stop()


def test_record_stop_edit_save(bus_and_session, tmp_path):
    bus, sim, session = bus_and_session
    c = Capture("Unit 14", "dies after 10 min", PLAN)
    c.start(session, str(tmp_path), {"source": "sim"})
    assert c.pending and c.state == cap.RECORDING
    time.sleep(0.6)
    c.tag_next()
    time.sleep(0.6)
    c.tag(3)                                    # shutdown
    sim.stop()                                  # the machine goes quiet ...
    time.sleep(2.8)
    sim2 = MachineSimulator(VirtualBackend(bus), seed=2)
    sim2.start()                                # ... and comes back
    time.sleep(1.2)
    c.stop()
    sim2.stop()
    assert c.state == cap.STOPPED and c.frames > 50
    c.rename(0, "key on (cold)")
    folder = c.save()
    assert c.state == cap.SAVED and not c.pending
    assert os.path.basename(folder).endswith("_Unit_14")
    assert sorted(os.listdir(folder)) == ["capture.json", "capture.log", "live_data.csv", "markers.csv",
                                          "summary.txt"]
    summary = open(os.path.join(folder, "summary.txt")).read()
    assert "1 - key on (cold)" in summary and "dies after 10 min" in summary
    # "went silent" is stamped with the ECU's last frame, a few ms either side of the marker
    assert "00 Engine #1 went silent   (during " in summary
    assert "00 Engine #1 back on the bus" in summary
    assert "SPN 3719 FMI 16 already active" in summary
    rows = list(csv.DictReader(open(os.path.join(folder, "live_data.csv"))))
    assert any(r["event"] == "1 - key on (cold)" for r in rows)
    assert rows[-1]["phase"] == "4 - shutdown" and "Engine speed [rpm]" in rows[0]
    markers = list(csv.DictReader(open(os.path.join(folder, "markers.csv"))))
    assert [m["title"] for m in markers][:2] == ["1 - key on (cold)", "2 - start"]
    assert markers[1]["elapsed"] == "not tagged"
    # relabel after saving: outputs are regenerated in place
    c.rename(1, "crank")
    assert c.save() == folder
    assert "2 - crank" in open(os.path.join(folder, "summary.txt")).read()


def test_crash_recovery(bus_and_session, tmp_path):
    _, _, session = bus_and_session
    c = Capture("crashy", plan=PLAN)
    c.start(session, str(tmp_path))
    time.sleep(1.2)
    c.tag_next()
    time.sleep(1.2)                             # LogWriter flushes about once a second
    session.stop_recording()                    # simulate the app dying without stop()/save()
    pending = cap.pending_captures(str(tmp_path))
    assert pending == [c.folder]
    r = Capture.load(pending[0])
    assert r.state == cap.STOPPED and r.name == "crashy" and r.markers[0].tagged
    assert r.stopped and r.stopped > r.started
    folder = r.save()
    assert cap.pending_captures(str(tmp_path)) == [] and cap.saved_captures(str(tmp_path)) == [folder]
    assert json.load(open(os.path.join(folder, "capture.json")))["state"] == "saved"


def test_discard(bus_and_session, tmp_path):
    _, _, session = bus_and_session
    c = Capture(plan=PLAN)
    c.start(session, str(tmp_path))
    c.stop()
    folder = c.folder
    c.discard()
    assert not os.path.exists(folder) and c.state == cap.IDLE
