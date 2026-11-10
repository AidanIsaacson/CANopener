"""Meter calibration: the correction itself, the hub applying it, and the Full / Quick flows."""

import pytest

from canopener.core import calibration as calib
from canopener.core.measure import Channel, MeasurementHub, Sample
from canopener.core.settings import Settings


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def feed(session, clock, value, secs=1.2, rate=20, noise=0.0):
    """``secs`` of readings at ``rate`` per second, moving the clock along and updating."""
    for i in range(int(secs * rate)):
        clock.t += 1 / rate
        session.feed(clock.t, value + (noise if i % 2 else -noise))
        session.update()


def session(mode=calib.FULL, n=2, ref=None, cal=None):
    clock = Clock()
    cal = cal or calib.Calibration(Settings())
    s = calib.CalSession(cal, [(f"pico.p{i + 1}", f"CH{i + 1}") for i in range(n)], mode, ref=ref,
                         clock=clock, today=lambda: "2026-11-10")
    return s, clock, cal


def test_channel_cal_applies_and_survives_bad_settings():
    c = calib.ChannelCal(offset=0.01, gain=1.02)
    assert c.apply(12.01) == pytest.approx(12.24)
    assert calib.ChannelCal.from_dict({"offset": "x", "gain": 99}) == calib.ChannelCal()   # nonsense -> neutral
    assert calib.ChannelCal.from_dict("nope") is None
    back = calib.ChannelCal.from_dict(calib.ChannelCal(0.004, 1.0123, 12.0, "2026-11-10").to_dict())
    assert back == calib.ChannelCal(0.004, 1.0123, 12.0, "2026-11-10")
    assert back.describe() == "gain 1.0123, zero +0.004 V, 2026-11-10"


def test_calibration_saves_to_settings_and_resets():
    s = Settings()
    cal = calib.Calibration(s)
    cal.save("iio.a0", calib.ChannelCal(0.002, 1.01))
    cal.save("pico.p1", calib.ChannelCal(0.0, 0.99))
    assert calib.Calibration(Settings()).get("iio.a0").gain == 1.01        # written to settings.json
    assert cal.apply("iio.a0", 1.002) == pytest.approx(1.01) and cal.apply("other", 5.0) == 5.0
    assert cal.apply("iio.a0", None) is None
    assert cal.reset_source("iio") == 1 and cal.get("iio.a0") is None and cal.get("pico.p1")
    cal.reset()
    assert cal.channels() == {} and Settings().get(calib.SETTING) == {}


def test_hub_corrects_samples_and_raw_listeners_see_the_raw_ones():
    cal = calib.Calibration(Settings())
    cal.save("m.p1", calib.ChannelCal(0.1, 2.0))
    hub = MeasurementHub(calibration=cal)
    seen, raw = [], []
    hub.add_listener(seen.append)
    hub.add_listener(raw.append, raw=True)
    hub.push(Sample("m.p1", 1.0, 1.1))
    hub.push(Sample("m.p2", 1.0, 3.0))
    hub.push(Sample("m.p1", 1.1, None, "OL"))
    assert [x.value for x in seen] == [pytest.approx(2.0), 3.0, None]
    assert [x.value for x in raw] == [1.1, 3.0, None]
    assert hub.latest("m.p1").flag == "OL" and hub.history("m.p1") == [(1.0, pytest.approx(2.0))]
    hub.remove_listener(raw.append)
    hub.push(Sample("m.p1", 1.2, 1.1))
    assert len(raw) == 3


def test_calibratable_picks_the_voltage_channels():
    chans = [Channel("a.v", "V1", "V"), Channel("a.t", "Temp", "°C"), Channel("a.x", "X", "", kind="voltage")]
    assert [c.id for c in calib.calibratable(chans)] == ["a.v", "a.x"]


def test_steady_needs_enough_settled_readings():
    st = calib.Steady(window=1.0, min_samples=5)
    for i in range(4):
        st.add(i * 0.25, 5.0)
    assert not st.steady                                 # only four readings
    st.add(1.0, 5.0)
    assert st.steady and st.mean == pytest.approx(5.0)
    st.add(1.3, 5.5)
    assert not st.steady                                 # still moving


def test_full_flow_zero_hold_adjust_save_then_next_channel():
    s, clock, cal = session(calib.FULL)
    assert s.step == calib.ZERO and s.current.label == "CH1"
    feed(s, clock, 0.012, noise=0.002)
    assert s.step == calib.ZERO                          # Full waits for the push
    s.select()
    assert s.step == calib.SPAN and s.new_offset == pytest.approx(0.012, abs=2e-4)
    s.new_offset = 0.012                                 # (exact, for the arithmetic below)
    assert s.pop_events() == ["cal_step"]
    feed(s, clock, 12.31)
    s.select()                                           # hold
    assert s.step == calib.CONFIRM and s.value == pytest.approx(12.298)
    s.toggle_confirm()
    s.select()                                           # "Adjust"
    assert s.step == calib.ADJUST
    s.adjust(-30)                                        # 0.01 V a click
    s.cycle_step()
    s.adjust(2)                                          # then 0.001 V a click
    assert s.value == pytest.approx(11.998 + 0.002)
    s.select()                                           # save
    assert s.step == calib.SAVED and "cal_saved" in s.pop_events()
    saved = cal.get("pico.p1")
    assert saved.gain == pytest.approx(12.0 / 12.298) and saved.offset == pytest.approx(0.012)
    assert saved.ref == pytest.approx(12.0) and saved.when == "2026-11-10"
    clock.t += calib.SAVED_SECS + 0.1
    s.update()
    assert s.step == calib.ZERO and s.current.label == "CH2"      # straight on to the next channel


def test_full_looks_right_keeps_the_gain_and_back_steps_out():
    s, clock, cal = session(calib.FULL, n=1)
    feed(s, clock, 0.0)
    s.select()
    feed(s, clock, 5.0)
    s.select()
    s.choose_adjust()
    s.adjust(5)
    assert s.back() and s.step == calib.CONFIRM and s.value == pytest.approx(5.0)    # adjust thrown away
    s.select()                                           # looks right
    assert cal.get("pico.p1").gain == pytest.approx(1.0)
    s.select()                                           # skip the pause
    assert s.step == calib.DONE and [c.state for c in s.channels] == ["saved"]


def test_zero_refuses_a_probe_that_isnt_on_com_and_says_why():
    s, clock, _ = session(calib.FULL)
    feed(s, clock, 3.2)
    assert "Reads 3.20 V" in s.problem
    s.select()
    assert s.step == calib.ZERO and s.pop_events() == ["error"]
    clock.t += calib.NO_DATA_SECS + 1
    s.update()
    assert "No readings from CH1" in s.problem


def test_quick_flow_takes_each_step_by_itself():
    s, clock, cal = session(calib.QUICK, n=3, ref=12.0)
    assert s.step == calib.REF
    s.adjust(5)                                          # 12.05
    s.select()
    assert s.step == calib.ZERO and s.ref == pytest.approx(12.05)
    for ch in ("pico.p1", "pico.p2"):
        feed(s, clock, 0.005)
        assert s.step == calib.SPAN
        feed(s, clock, 0.005, secs=0.5)                  # still on COM: not taken as the supply
        assert s.step == calib.SPAN
        feed(s, clock, 11.8)
        assert s.step == calib.SAVED
        assert cal.get(ch).gain == pytest.approx(12.05 / (11.8 - 0.005))
        clock.t += calib.SAVED_SECS + 0.1
        s.update()
    s.skip()
    assert s.step == calib.DONE and [c.state for c in s.channels] == ["saved", "saved", "skipped"]
    assert s.pop_events().count("cal_saved") == 2


def test_quick_asks_before_using_a_reading_far_from_the_typed_voltage():
    s, clock, cal = session(calib.QUICK, n=1, ref=12.0)
    s.select()
    feed(s, clock, 0.0)
    feed(s, clock, 5.0)                                  # a 5 V supply, but 12 V typed in
    assert s.step == calib.SPAN and "expected 12.00 V" in s.problem
    s.select()                                           # use it anyway
    assert s.step == calib.SAVED and cal.get("pico.p1").gain == pytest.approx(12.0 / 5.0)


def test_a_gain_far_from_one_is_refused_as_a_divider_problem():
    s, clock, cal = session(calib.QUICK, n=1, ref=12.0)
    s.select()
    feed(s, clock, 0.0)
    feed(s, clock, 0.57)                                 # 12 V through a 21:1 divider with the ratio left at 1
    s.select()
    assert s.step == calib.SPAN and "divider" in s.problem and cal.get("pico.p1") is None
    events = s.pop_events()
    feed(s, clock, 0.57)                                 # doesn't keep complaining every frame
    assert events.count("error") == 1 and s.pop_events() == []
