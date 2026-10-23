"""Connection lamps, the signal finder, and sound cues (speaker / buzzer)."""

import os
import struct
import time

from canopener.can.frame import CanFrame
from canopener.core import connection, interfaces, link, sound
from canopener.core.session import Session


class Bus:
    """A fake harness: which bitrate the machine talks at, whether the probes are on it."""

    def __init__(self, bitrate=500000):
        self.bitrate = bitrate
        self.configured = None
        self.contact = True
        self.present = True
        self.steps = []

    def runner(self, steps):
        self.steps.append(steps)
        self.configured = int(steps[1][7])
        return True, "ok"

    def ifaces(self):
        return [interfaces.CanInterface("can0", "up")] if self.present else []

    def backend(self, name):
        bus = self

        class B:
            def open(self):
                if not bus.present:
                    raise OSError("No such device")

            def close(self):
                pass

            def recv(self):
                time.sleep(0.001)
                if not bus.contact:
                    return None
                if bus.configured == bus.bitrate:
                    return CanFrame(0x18FEF100, b"\x00" * 8)
                return CanFrame(0x80, b"", error="bus error")
        return B()


def finder(bus, events, **kw):
    kw.setdefault("bitrates", link.bitrate_order(250000))
    kw.setdefault("runner", bus.runner)
    return link.SignalFinder("can0", kw.pop("bitrates"), events.append, listen_secs=0.05, confirm_secs=0.05,
                             open_backend=bus.backend, list_ifaces=bus.ifaces, hint=lambda: "plug it in", **kw)


def test_finder_cycles_bitrates_listen_only_and_locks():
    bus, events = Bus(500000), []
    f = finder(bus, events)
    f.run()
    assert f.state == "locked" and f.result == 500000
    assert [int(s[1][7]) for s in bus.steps] == [250000, 500000]
    assert all(s[1][9] == "on" for s in bus.steps)               # never out of listen-only
    states = [e.state for e in events]
    assert states[0] == "searching" and "errors only" in events[0].text
    assert states[-2:] == ["heard", "locked"]


def test_finder_keeps_going_until_the_probes_land():
    bus, events = Bus(250000), []
    bus.contact = False
    bus.present = False
    f = finder(bus, events)

    def on_event(ev):
        events.append(ev)
        if len(events) == 1:
            assert ev.state == "no_adapter" and "plug it in" in ev.text
            bus.present = True                     # adapter plugged in
        elif len(events) == 6:
            bus.contact = True                     # probes on the right wires

    f.on_event = on_event
    f._stop.wait = lambda secs: False              # don't actually wait between attempts
    f.run()
    assert f.result == 250000
    assert sum(e.state == "searching" and "nothing heard" in e.text for e in events) >= 4


def test_finder_retries_then_gives_up_when_it_cant_configure():
    bus, events = Bus(), []
    calls = []

    def runner(steps):                            # fails once, then works
        calls.append(steps)
        return bus.runner(steps) if len(calls) > 1 else (False, "transient")
    f = finder(bus, events, runner=runner)
    f._stop.wait = lambda secs: False
    f.run()
    assert events[0].state == "error" and f.state == "locked"

    events.clear()
    f = finder(bus, events, runner=lambda steps: (False, "Operation not permitted"))
    f._stop.wait = lambda secs: False
    f.run()
    assert [e.state for e in events] == ["error", "error", "failed"]
    assert "not permitted" in events[-1].text


def test_finder_switches_to_normal_mode_only_when_asked():
    bus, events, ups = Bus(250000), [], []
    f = finder(bus, events, bitrates=[250000], listen_only=False,
               bring_up=lambda name, br, lo: (ups.append((name, br, lo)) or (True, "ok")))
    f.run()
    assert ups == [("can0", 250000, False)]
    f = finder(bus, events, bitrates=[250000], bring_up=lambda *a: ups.append(a))
    f.run()
    assert len(ups) == 1                          # listen-only kept: no second bring-up


def test_finder_stays_on_a_bitrate_with_some_clean_frames():
    bus, events = Bus(250000), []
    f = finder(bus, events, min_frames=10_000)    # never "steady", but clean frames at 250k
    seen = []

    def on_event(ev):
        seen.append(ev)
        if len(seen) >= 3:
            f._stop.set()
    f.on_event = on_event
    f.run()
    assert bus.steps and all(int(s[1][7]) == 250000 for s in bus.steps)


def test_finder_stop_from_another_thread():
    bus, events = Bus(125000), []
    bus.contact = False
    f = finder(bus, events)
    f.start()
    time.sleep(0.2)
    assert f.running
    f.stop()
    assert not f.running and f.result is None


def test_judge_and_bitrate_order():
    assert link.judge(5, 2, 5) and not link.judge(5, 3, 5) and not link.judge(4, 0, 5)
    assert link.bitrate_order(500000) == [500000, 250000, 125000, 1000000]
    assert link.bitrate_order(None) == interfaces.BITRATES


def test_adapter_status():
    can0 = interfaces.CanInterface("can0", "up")
    assert link.adapter_status(connection.SIMULATOR, "can0").level == link.SIM
    assert link.adapter_status(connection.SOCKETCAN, "can0", [can0], []).level == link.OK
    s = link.adapter_status(connection.SOCKETCAN, "can0", [interfaces.CanInterface("can1", "down")], [])
    assert s.level == link.WAIT and "can1" in s.detail
    s = link.adapter_status(connection.SOCKETCAN, "can0", [], [interfaces.UsbAdapter("dfu", "CANable")])
    assert s.level == link.WAIT and "DFU" in s.detail
    assert link.adapter_status(connection.SOCKETCAN, "can0", [], []).level == link.BAD


def test_bus_status():
    class Backend:
        can_transmit = True
    s = Session(Backend())
    now = 1000.0
    assert link.bus_status(None).level == link.OFF
    assert link.bus_status(Session()).level == link.OFF               # offline log
    assert link.bus_status(s, now).level == link.WAIT                 # connected, silent
    s.health.last_error, s.health.last_error_t = "bus error", now - 1
    assert link.bus_status(s, now).level == link.BAD
    s.last_rx_t = now - 0.5
    assert link.bus_status(s, now).level == link.OK
    s.health.link_error = "Network is down"
    assert link.bus_status(s, now).level == link.BAD


def test_session_tracks_last_good_frame():
    s = Session()
    s.feed(CanFrame(0x80, b"", error="bus error"))
    assert s.last_rx_t == 0.0
    s.feed(CanFrame(0x18FEF100, b"\x00" * 8))
    assert s.last_rx_t > 0


def test_count_traffic_stops_early():
    frames = iter([CanFrame(1, b"", error="x"), None] + [CanFrame(1, b"\x00")] * 10)

    class B:
        def recv(self):
            return next(frames)
    assert interfaces.count_traffic(B(), 5.0, enough=3) == (3, 1)


# ----------------------------------------------------------------------
# sound
# ----------------------------------------------------------------------
def test_cues_have_distinct_rhythms_and_valid_wav(tmp_path):
    rhythms = {tuple(round(s, 2) for _, s in notes) for c, notes in sound.CUES.items() if c != "test"}
    assert len(rhythms) == 3
    path = sound.cue_path("found", str(tmp_path))
    import wave
    with wave.open(path) as w:
        assert w.getframerate() == sound.RATE and w.getnframes() > sound.RATE * 0.5


def test_pwm_buzzer_plays_each_note(tmp_path):
    chip = tmp_path / "pwmchip0"
    chip.mkdir()
    (chip / "export").write_text("")
    pwm = chip / "pwm0"
    pwm.mkdir()                                    # as if export created it
    writes = []
    real_write = sound.PwmBuzzer._write

    def spy(path, value):
        writes.append((os.path.basename(path), int(value)))
        real_write(path, value)
    b = sound.PwmBuzzer(0, 0, sys_pwm=str(tmp_path))
    b._write = spy
    b.play([(1000, 0.1), (2000, 0.1)], sleep=lambda s: None)
    periods = [v for k, v in writes if k == "period"]
    assert periods == [1_000_000, 500_000]
    assert ("duty_cycle", 500_000) in writes and ("duty_cycle", 250_000) in writes
    assert writes[-1] == ("enable", 0)


def test_input_beeper_sends_tones(tmp_path):
    dev = tmp_path / "event3"
    dev.write_bytes(b"")
    sound.InputBeeper(str(dev)).play([(880, 0.1)], sleep=lambda s: None)
    size = struct.calcsize("llHHi")
    data = dev.read_bytes()
    events = [struct.unpack("llHHi", data[i:i + size]) for i in range(0, len(data), size)]
    assert [(t, c, v) for _, _, t, c, v in events] == [(0x12, 2, 880), (0x12, 2, 0), (0x12, 2, 0)]


def test_find_beeper(tmp_path):
    sys_input, dev = tmp_path / "sys", tmp_path / "dev"
    dev.mkdir()
    for ev, name in (("event0", "AT Translated Set 2 keyboard"), ("event1", "PC Speaker"),
                     ("event2", "pwm-beeper")):
        (sys_input / ev / "device").mkdir(parents=True)
        (sys_input / ev / "device" / "name").write_text(name + "\n")
        (dev / ev).write_bytes(b"")
    assert sound.find_beeper(str(sys_input), str(dev)) == str(dev / "event2")
    (dev / "event2").chmod(0o444)
    if not os.access(dev / "event2", os.W_OK):   # (root can write anyway)
        assert sound.find_beeper(str(sys_input), str(dev)) is None


def test_buzzer_spec_and_nothing_to_play(monkeypatch):
    assert sound.buzzer_for("off") is None and sound.buzzer_for("") is None
    assert isinstance(sound.buzzer_for("pwm:0:1"), sound.PwmBuzzer)
    assert sound.buzzer_for("pwm:x") is None
    assert isinstance(sound.buzzer_for("/dev/input/event5"), sound.InputBeeper)
    monkeypatch.setattr(sound, "player", lambda: None)
    monkeypatch.setattr(sound, "find_beeper", lambda *a: None)
    assert sound.play("found") is False                    # front-end falls back to its own beep
    assert sound.play("nope") is False
