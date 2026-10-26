"""Measurements (meters) beside CAN: the hub, sources, the shared settings file, and captures that
record both onto one timeline."""

import csv
import json
import os
import time

from canopener.can.backends import VirtualBackend, VirtualBus
from canopener.core import capture as cap
from canopener.core import measure, meters
from canopener.core.capture import Capture
from canopener.core.measure import Channel, MeasurementHub, MeasurementSource, Sample
from canopener.core.session import Session
from canopener.core.settings import DEFAULTS, Settings
from canopener.sim import MachineSimulator


class FakeMeter(MeasurementSource):
    """Pushes only what the test tells it to."""

    id = "fake"
    name = "Fake meter"

    def __init__(self, chans=None):
        super().__init__()
        self.chans = chans or [Channel("fake.v", "Probe V", "V", "fake", 2, "voltage", 0.5, 4.5)]

    def channels(self):
        return list(self.chans)


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# --- settings ----------------------------------------------------------------
def test_settings_defaults_persist_and_survive_a_bad_file(tmp_path):
    path = str(tmp_path / "cfg" / "settings.json")
    s = Settings(path)
    assert s.get("can.bitrate") == DEFAULTS["can.bitrate"] and s["device.softkeys_edge"] == "top"
    seen = []
    s.on_change(lambda k, v: seen.append((k, v)))
    s.set("can.bitrate", 500000)
    s.set("can.bitrate", 500000)                 # unchanged: no write, no callback
    s.update({"units": "imperial", "meters.sim": True})
    assert seen == [("can.bitrate", 500000), ("units", "imperial"), ("meters.sim", True)]
    assert json.load(open(path))["can.bitrate"] == 500000
    assert Settings(path).get("units") == "imperial"       # a fresh reader sees it
    s.reset("units")
    assert Settings(path).get("units") == "metric"
    open(path, "w").write("{not json")
    assert Settings(path).get("can.iface") == "can0"       # corrupt file: defaults, no crash


def test_settings_default_path_is_under_the_config_dir():
    s = Settings()
    s.set("profile", "obd2")
    assert os.path.exists(os.path.join(os.environ["XDG_CONFIG_HOME"], "canopener", "settings.json"))


# --- hub and live rows --------------------------------------------------------
def test_hub_keeps_latest_history_and_feeds_listeners():
    hub = MeasurementHub(history=3)
    src = FakeMeter()
    hub.add_source(src)
    got = []
    hub.add_listener(got.append)
    hub.add_listener(lambda s: 1 / 0)            # a broken listener is ignored
    for i in range(5):
        src.emit("fake.v", float(i), t=100.0 + i)
    src.emit("fake.v", None, t=106.0, flag="OL")
    assert hub.latest("fake.v") == Sample("fake.v", 106.0, None, "OL")
    assert hub.history("fake.v") == [(102.0, 2.0), (103.0, 3.0), (104.0, 4.0)]
    assert len(got) == 6 and hub.sample_count == 6
    assert hub.channel("fake.v").unit == "V"
    hub.remove_source("fake")
    assert hub.channels() == [] and src._sink is None


def test_measurement_live_rows_look_like_signals():
    hub = MeasurementHub()
    src = FakeMeter()
    hub.add_source(src)
    live = measure.MeasurementLiveData()
    assert not live.sample(hub)                  # no readings yet: no rows
    src.emit("fake.v", 4.9, t=1.0)
    assert live.sample(hub)
    key = live.order[0]
    row = live.rows[key]
    assert (row.value, row.sig.name, row.sig.unit, row.sig.decimals) == (4.9, "Probe V", "V", 2)
    assert row.sig.out_of_range(4.9) and not row.sig.out_of_range(2.0)
    assert live.source_label("fake") == "Fake meter" and live.matches(key, "probe")
    src.emit("fake.v", None, t=2.0, flag="OL")
    live.sample(hub)
    assert row.value is None and live.flags[key] == "OL" and row.hi == 4.9


def test_clock_mapper_uses_the_fastest_reading():
    m = measure.ClockMapper(window=10)
    assert m.to_host(5.0, host_t=1000.020) == 1000.020         # first: offset 995.020
    assert m.to_host(6.0, host_t=1001.005) == 1001.005         # faster arrival: offset 995.005
    assert abs(m.to_host(7.0, host_t=1002.050) - 1002.005) < 1e-9   # slow packet doesn't shift time


def test_value_at():
    pts = [(1.0, 10.0), (2.0, 20.0), (3.0, 30.0)]
    assert measure.value_at(pts, 0.5) is None
    assert measure.value_at(pts, 2.5) == 20.0 and measure.value_at(pts, 3.0) == 30.0
    assert measure.value_at(pts, 9.0, max_age=2.0) is None


# --- sources ------------------------------------------------------------------
def test_simulated_meter_shape_and_dropout():
    m = meters.SimulatedMeter(seed=1, sweep_s=6.0)
    supply, gnd, a, b = m.values(3.0)                        # top of the sweep
    assert abs(supply - 5) < 0.05 and abs(gnd) < 0.05 and abs(a - 4.5) < 0.05 and abs(b - 2.25) < 0.05
    assert m.values(12 + 0.44 * 6)[3] < 0.1                  # third sweep: B drops out mid-travel
    assert m.values(0 + 0.44 * 6)[3] > 1.0                   # first sweep: it doesn't
    hub = MeasurementHub()
    hub.add_source(meters.SimulatedMeter(rate=50))
    assert wait_for(lambda: hub.latest("sim.p4") is not None)
    hub.stop()


def test_line_protocol_hello_json_and_key_value():
    got = []
    src = meters.LineSource("pico", "Pico", opener=None)
    src._sink = got.append                # lines are fed by hand: no reader thread
    src.handle('{"hello": "canopener-meter", "name": "Pico DMM", "channels": '
               '[{"id": "p1", "name": "Probe 1", "unit": "V", "decimals": 3, "low": 0.5}]}', now=50.0)
    src.handle('{"ch": "p1", "v": 4.512}', now=50.1)
    src.handle('{"ch": "p1", "v": null, "flag": "OL"}', now=50.2)
    src.handle("p1=1.25 p2=oops r1=220", now=50.3)
    src.handle("garbage {", now=50.4)
    src.handle('{"status": "error", "text": "probe 3 over voltage"}')
    assert src.name == "Pico DMM"
    ch = {c.id: c for c in src.channels()}
    assert ch["pico.p1"].name == "Probe 1" and ch["pico.p1"].low == 0.5 and ch["pico.p1"].source == "pico"
    assert [(s.channel, s.value, s.flag) for s in got] == [
        ("pico.p1", 4.512, ""), ("pico.p1", None, "OL"),
        ("pico.p1", 1.25, ""), ("pico.p2", None, "oops"), ("pico.r1", 220.0, "")]
    assert src.status.level == measure.BAD and "over voltage" in src.status.detail


def test_line_source_reads_a_stream_and_retries_when_it_goes_away():
    r, w = os.pipe()
    opened = []

    def opener():
        if opened:
            raise OSError(2, "No such file or directory")
        opened.append(1)
        return meters.FdReader(r)

    hub = MeasurementHub()
    src = meters.LineSource("ser", "Serial meter", opener, where="/dev/ttyACM0")
    hub.add_source(src)
    os.write(w, b'{"ch": "v", "v": 1.5, "t_us": 1000}\nv=1.75\n')
    assert wait_for(lambda: src.lines == 2)
    assert hub.latest("ser.v").value == 1.75 and src.status.level == measure.OK
    os.close(w)                                         # the meter is unplugged
    assert wait_for(lambda: src.status.level == measure.BAD)
    hub.stop()


def test_iio_source_reads_sysfs_voltages(tmp_path):
    dev = tmp_path / "iio:device0"
    dev.mkdir()
    (dev / "name").write_text("ads1015\n")
    (dev / "in_voltage0_raw").write_text("1000\n")
    (dev / "in_voltage0_scale").write_text("2.0\n")          # mV per count
    (dev / "in_voltage1_raw").write_text("250\n")
    (dev / "in_voltage1_scale").write_text("2.0\n")
    (dev / "in_voltage0-voltage1_raw").write_text("5\n")     # differential: skipped
    assert meters.iio_devices(str(tmp_path)) == [(str(dev), "ads1015")]
    src = meters.IioSource("ads1015", ratio=6.0, sys_iio=str(tmp_path))
    got = []
    src._sink = got.append
    assert src._find() and [c.id for c in src.channels()] == ["iio.a0", "iio.a1"]
    assert src.read_once(now=5.0)
    assert [(s.channel, round(s.value, 6)) for s in got] == [("iio.a0", 12.0), ("iio.a1", 3.0)]


def test_sources_from_settings(tmp_path):
    s = Settings(str(tmp_path / "s.json"))
    assert meters.sources_from_settings(s) == []
    s.update({"meters.sim": True, "meters.serial": "/dev/ttyACM0", "meters.iio": "ads1015"})
    assert [type(x).__name__ for x in meters.sources_from_settings(s)] == [
        "SimulatedMeter", "SerialLineSource", "IioSource"]


# --- captures -----------------------------------------------------------------
def test_capture_records_meters_beside_the_bus(tmp_path):
    bus = VirtualBus()
    sim = MachineSimulator(VirtualBackend(bus), seed=1)
    sim.start()
    session = Session(VirtualBackend(bus))
    session.start()
    hub = MeasurementHub()
    src = FakeMeter()
    hub.add_source(src)
    try:
        c = Capture("pedal", "", ["sweep"])
        c.start(session, str(tmp_path), {"source": "sim"}, measurements=hub)
        t0 = c.started
        for i in range(30):                      # 3 s at 10 readings/s; out of range for a moment
            src.emit("fake.v", 4.8 if 10 <= i < 13 else 2.0 + i * 0.01, t=t0 + i * 0.1)
        src.emit("fake.v", None, t=t0 + 3.05, flag="OL")
        c.tag_next(t0 + 1.05)
        assert c.recorded_samples == 31
        time.sleep(1.2)
        c.stop()
    finally:
        session.stop()
        sim.stop()
    assert c.samples == 31 and c.meta["channels"][0]["id"] == "fake.v"
    src.emit("fake.v", 3.0)                     # after stopping: not recorded
    folder = c.save()
    assert "measurements.csv" in os.listdir(folder)
    rows = list(csv.DictReader(open(os.path.join(folder, "measurements.csv"))))
    assert len(rows) == 31 and rows[0]["name"] == "Probe V" and rows[-1]["flag"] == "OL"
    live = list(csv.DictReader(open(os.path.join(folder, "live_data.csv"))))
    assert "Probe V [V]" in live[0] and "Engine speed [rpm]" in live[0]
    assert any(r["Probe V [V]"] == "4.8" for r in live)
    summary = open(os.path.join(folder, "summary.txt")).read()
    assert "31 meter readings" in summary and "-- Measurements --" in summary
    assert "Probe V out of range: 4.80 V, normal 0.50 V to 4.50 V" in summary
    assert "Probe V back to normal" in summary and "Probe V: no reading (OL)" in summary
    assert "1 - sweep" in summary.split("-- Measurements at each timestamp --")[1]
    assert json.load(open(os.path.join(folder, cap.META_JSON)))["samples"] == 31


def test_capture_with_meters_only_and_trim(tmp_path):
    hub = MeasurementHub()
    src = FakeMeter()
    hub.add_source(src)
    c = Capture("bench", "", [])
    c.start(None, str(tmp_path), measurements=hub)
    t0 = c.started
    for i in range(600):                         # 10 minutes at 1 reading/s
        src.emit("fake.v", 1.0 + (i % 10) / 10, t=t0 + i)
    c.stop()
    c.stopped = t0 + 599
    c.tag_next(t0 + 300)
    kept, _ = c.trim(60, 30, keep_full=True)
    assert kept == 0 and c.samples == 91          # no frames, 91 readings kept (240..330 s)
    assert os.path.exists(os.path.join(c.folder, cap.FULL_MEASUREMENTS))
    folder = c.save()
    live = list(csv.DictReader(open(os.path.join(folder, "live_data.csv"))))
    assert len(live) > 150 and "Probe V [V]" in live[0]       # rows from the meter alone
    assert "91 meter readings" in open(os.path.join(folder, "summary.txt")).read()


def test_capture_needs_something_to_record(tmp_path):
    try:
        Capture().start(None, str(tmp_path))
    except ValueError:
        return
    raise AssertionError("expected ValueError")
