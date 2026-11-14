"""core.changes: what changed between the phases of a capture, with no decoder."""

import json
import math

from canopener.can.frame import CanFrame
from canopener.can.logfile import write_log
from canopener.core import changes


def _frames():
    """A made-up machine, 60 s at 20 Hz, three phases of 20 s: key on, idling, working.

    * PGN F004 from 00 (the standard engine message): engine speed in bytes 3-4, about 0 / 800 / 1500 rpm,
      wandering as a real engine does
    * PGN FF50 from 00 (unnamed): bytes 2-3 the same speed in whole rpm, byte 0 a rolling counter,
      byte 7 noise (a checksum), byte 5 a slow temperature creeping 40 -> 43
    * ID 288 (unnamed, 11-bit): bit 2 of byte 1 set only while working; byte 4 goes 0x40 -> 0x1B
      (several bits at once) on the same step
    * ID 701: a CANopen heartbeat (housekeeping: never listed)
    * PGN FE00 from 27: only present while working
    """
    out = []
    for n in range(1200):
        t = 1000.0 + n * 0.05
        phase = int(n / 400)
        rpm = [0, 800, 1500][phase] + (0 if phase == 0 else 150 * math.sin(n / 25))
        raw = int(rpm * 8)
        out.append(CanFrame(0x0CF00400, bytes([0xF0, 0x7D, 0x7D, raw & 0xFF, raw >> 8, 0, 0xF0, 0xFF]), True, t))
        whole = int(rpm)
        out.append(CanFrame(0x18FF5000, bytes([n & 0xFF, 0, whole & 0xFF, whole >> 8, 0, 40 + n // 400,
                                                0, (n * 97 + 13) % 251]), True, t + 0.001))
        out.append(CanFrame(0x288, bytes([0, 0x04 if phase == 2 else 0, 0, 0, 0x1B if phase == 2 else 0x40, 0, 0, 0]),
                            False, t + 0.002))
        if n % 20 == 0:
            out.append(CanFrame(0x701, b"\x05", False, t + 0.003))
        if phase == 2:
            out.append(CanFrame(0x18FE0027, bytes(8), True, t + 0.004))
    return out


PHASES = [changes.Phase("Key on", 1000.0, 1020.0), changes.Phase("Idling", 1020.0, 1040.0),
          changes.Phase("Working", 1040.0, 1060.0)]


def _report():
    return changes.analyze(_frames(), PHASES)


def _stream(report, label):
    return next(s for s in report.streams if s.label == label)


def _field(stream, where):
    return next(f for f in stream.fields if f.where == where)


def test_fields_are_split_and_classified():
    r = _report()
    eec1 = _stream(r, "PGN F004 from 00")
    speed = _field(eec1, "w3le")
    assert speed.known == "Engine speed" and speed.kind == changes.VALUE and speed.scale[2] == "rpm"
    assert [round(p.mean / 8, -2) for p in speed.by_phase] == [0, 800, 1500]
    ff50 = _stream(r, "PGN FF50 from 00")
    assert _field(ff50, "w2le").kind == changes.VALUE            # two bytes that carry: one number
    assert _field(ff50, "b0").kind == changes.COUNTER
    assert _field(ff50, "b7").kind == changes.NOISE
    assert _field(ff50, "b5").kind == changes.VALUE              # 40, 41, 42: a slow value, not flags
    assert not [f for f in ff50.fields if f.where.startswith("b5.")]
    assert _field(_stream(r, "ID 288"), "b1.2").kind == changes.SWITCH
    assert _stream(r, "ID 701").fields == []                    # housekeeping: nothing inside to list


def test_changes_between_phases_rank_what_moved_and_leave_out_counters_and_noise():
    r = _report()
    start = r.changes(0, 1)
    assert {(c.stream.label, c.field.where) for c in start} == {("PGN F004 from 00", "w3le"),
                                                               ("PGN FF50 from 00", "w2le")}
    assert "0 -> 6" in start[0].text or "0 -> 8" in start[0].text
    work = r.changes(1, 2)
    got = {(c.stream.label, c.field.where if c.field else "") for c in work}
    assert ("ID 288", "b1.2") in got and ("PGN FE00 from 27", "") in got
    assert ("ID 288", "b4") in got and not [w for _s, w in got if w.startswith("b4.")]   # 5 bits: shown as the byte
    assert next(c.text for c in work if c.field and c.field.where == "b4").startswith("40 -> 1B")
    assert next(c.text for c in work if c.field is None) == "started"
    assert not [c for c in start + work if c.field and c.field.where in ("b0", "b7")]
    assert not [c for c in start + work if c.stream.label == "ID 701"]


def test_an_unnamed_value_that_follows_a_named_one_is_given_its_scale():
    rel = _report().relations()
    r = next(x for x in rel if x.stream.label == "PGN FF50 from 00" and x.field.where == "w2le")
    assert r.other.known == "Engine speed"
    k, c, unit = r.formula()
    assert unit == "rpm" and abs(k - 1.0) < 0.02 and abs(c) < 5
    assert "Engine speed (rpm) = 1" in r.text()


def test_report_text_json_and_capture_loading(tmp_path):
    r = _report()
    text = "\n".join(r.lines())
    assert "== Key on  ->  Idling ==" in text and "Engine speed" in text and "move together" in text
    d = json.loads(json.dumps(r.to_dict()))
    assert [p["label"] for p in d["phases"]] == ["Key on", "Idling", "Working"]
    assert any(c["known"] == "Engine speed" for c in d["changes"]) and d["relations"]
    log = tmp_path / "capture.log"
    write_log(str(log), _frames())
    whole = changes.analyze_capture(str(log))
    assert len(whole.phases) == 1 and whole.frames == r.frames
    parts = changes.analyze_capture(str(log), parts=3)
    assert len(parts.phases) == 3 and any(c.field and c.field.where == "w3le" for c in parts.changes(0, 1))


def test_phases_from_markers():
    ph = changes.phases_from_markers([(15.0, "B"), (5.0, "A")], 0.0, 30.0)
    assert [(p.label, p.start, p.end) for p in ph] == [(changes.BEFORE, 0.0, 5.0), ("A", 5.0, 15.0), ("B", 15.0, 30.0)]
    assert changes.phases_from_markers([], 0.0, 30.0)[0].label == "The whole capture"
