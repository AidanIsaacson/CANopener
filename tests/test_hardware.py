"""Installed options (core.hardware), the SYNC pulse clock (core.sync), the logic Pico helpers
(core.logic) and the meter line-protocol additions they rely on."""

import io
import json
import os
import zipfile

from canopener.core import hardware, logic, meters, sync
from canopener.core.settings import Settings


def _settings(tmp_path):
    return Settings(str(tmp_path / "settings.json"))


def _can_iface(root, name, *, spi=None, usb=None, virtual=False):
    """A CAN interface in a fake sysfs: on an SPI chip select, on a USB device, or with no device
    (slcand's, or vcan)."""
    net = root / "sys/class/net" / name
    os.makedirs(net)
    (net / "type").write_text("280\n")
    if spi:
        dev = root / "sys/devices/platform/soc/spi" / spi
        os.makedirs(dev / "net" / name)
        os.makedirs(root / "sys/bus/spi/devices", exist_ok=True)
        os.symlink(dev, root / "sys/bus/spi/devices" / spi)
        os.symlink(dev, net / "device")
    elif usb:
        dev = root / "sys/devices/platform/usb1/1-1" / usb
        drv = root / "sys/bus/usb/drivers" / "gs_usb"
        os.makedirs(dev)
        os.makedirs(drv, exist_ok=True)
        os.symlink(drv, dev / "driver")
        os.symlink(dev, net / "device")


def _sysfs(root, *, can=(), pps=False, iio=None, serial=()):
    for iface in can:
        _can_iface(root, iface, spi="spi0.0")
    if pps:
        d = root / "sys/class/pps/pps0"
        os.makedirs(d)
        (d / "name").write_text("pps@15.-1\n" if pps == "other" else "pps-gpio.-1\n")
    if iio:
        d = root / "sys/bus/iio/devices/iio:device0"
        os.makedirs(d)
        (d / "name").write_text(iio + "\n")
    if serial:
        os.makedirs(root / "dev/serial/by-id")
        for name in serial:
            (root / "dev/serial/by-id" / name).write_text("")


def _hw(tmp_path, **sysfs):
    root = tmp_path / "root"
    root.mkdir()
    _sysfs(root, **sysfs)
    return hardware.Hardware(_settings(tmp_path), hardware.Probe(str(root)), cache_s=0)


def test_nothing_is_installed_by_default(tmp_path):
    hw = _hw(tmp_path, can=["can0"])
    assert not any(hw.available(o.id) for o in hardware.OPTIONS)
    assert all(st.text == "Not installed" for st in hw.report())


def test_stage_preset_and_requirements(tmp_path):
    hw = _hw(tmp_path)
    hw.set_stage("0")
    assert [o.id for o in hardware.OPTIONS if hw.state(o.id) == hardware.YES] == ["usb_can"]
    hw.set_stage("2b")
    assert [o.id for o in hardware.OPTIONS if hw.state(o.id) == hardware.YES] == \
        ["usb_can", "controls", "can_a", "logic_pico", "meter_pico", "sync"]
    hw.set_state("ohms", hardware.YES)
    assert hw.available("ohms")
    hw.set_state("meter_pico", hardware.NO)
    assert not hw.available("ohms") and not hw.available("sync")   # needs the meter Pico
    assert hw.status("ohms").text.startswith("Needs Meter Pico")


def test_installed_but_missing_is_a_fault_with_a_hint(tmp_path):
    hw = _hw(tmp_path)
    hw.set_state("can_a", hardware.YES)
    st = hw.status("can_a")
    assert st.level == hardware.BAD and "mcp251xfd" in st.detail
    assert hw.available("can_a")          # still offered: the UI shows the fault and retries


def test_auto_uses_what_is_found(tmp_path):
    hw = _hw(tmp_path, can=["can0"], pps=True, iio="ads1115",
             serial=["usb-MicroPython_Board_in_FS_mode_e6614c3-if00"])
    for oid in ("can_a", "can_b", "adc_pi", "meter_pico", "sync", "logic_pico"):
        hw.set_state(oid, hardware.AUTO)
    assert hw.available("can_a") and not hw.available("can_b")
    assert hw.detect("adc_pi").detail == "ads1115"
    assert hw.port("meter_pico").endswith("MicroPython_Board_in_FS_mode_e6614c3-if00")
    assert hw.available("sync")
    assert not hw.available("logic_pico")     # the MicroPython Pico is the meter, not sigrok


def test_usb_adapter_and_spi_stack_are_told_apart(tmp_path):
    root = tmp_path / "root"
    _can_iface(root, "can0", usb="1-1:1.0")                 # the CANable, plugged in first
    _can_iface(root, "can1", spi="spi0.0")                  # the MCP2518FD came up as can1
    _can_iface(root, "vcan0", virtual=True)
    probe = hardware.Probe(str(root))
    assert probe.usb_can() == hardware.Detection(True, "can0 (gs_usb)")
    assert probe.spi_net("spi0.0").where == "can1"
    assert probe.spi_net("spi0.1").found is False


def test_slcan_canable_counts_once_slcand_attached_it(tmp_path):
    root = tmp_path / "root"
    usb = root / "sys/bus/usb/devices/1-1"
    os.makedirs(usb)
    (usb / "idVendor").write_text("16d0\n")
    (usb / "idProduct").write_text("117e\n")
    (usb / "product").write_text("CANable2 b158aa7 github.com/normaldotcom/canable2.git\n")
    probe = hardware.Probe(str(root))
    det = probe.usb_can()
    assert det.found is False and "slcan" in det.detail          # plugged in, not attached yet
    _can_iface(root, "can0")                                     # slcand made can0 (no device link)
    assert probe.usb_can() == hardware.Detection(True, "can0 (slcan)")


def test_controls_from_input_device_names(tmp_path):
    root = tmp_path / "root"
    probe = hardware.Probe(str(root))
    assert probe.input().found is False
    for i, name in enumerate(["button@5", "button@6", "rotary@18", "vc4-hdmi"]):
        d = root / f"sys/class/input/input{i}"
        os.makedirs(d)
        (d / "name").write_text(name + "\n")
    assert probe.input() == hardware.Detection(True, "2 button(s) + encoder")


def test_meter_options_come_from_the_hello(tmp_path):
    hw = _hw(tmp_path)
    hw.set_stage("8")
    assert hw.status("ads8584s").level == hardware.WAIT
    hw.meter_report(["dac", "ohms"])
    assert hw.status("ohms").level == hardware.OK
    assert hw.status("ads8584s").level == hardware.BAD
    hw.meter_report(None)
    assert hw.status("ohms").level == hardware.WAIT


def test_settings_port_wins(tmp_path):
    hw = _hw(tmp_path, serial=["usb-Raspberry_Pi_Pico_E66-if00"])
    hw.settings.set("hardware.logic_pico.port", "/dev/ttyACM7")
    hw.set_state("logic_pico", hardware.YES)
    assert hw.status("logic_pico").level == hardware.BAD      # /dev/ttyACM7 isn't there
    assert hw.port("logic_pico") == "/dev/ttyACM7"


def test_sources_follow_installed_options(tmp_path):
    hw = _hw(tmp_path, iio="ads1115")
    s = hw.settings
    assert meters.sources_from_settings(s, hw) == []
    hw.set_state("adc_pi", hardware.YES)
    hw.set_state("meter_pico", hardware.YES)     # not plugged in: a source that reports it and retries
    srcs = meters.sources_from_settings(s, hw)
    assert sorted(x.id for x in srcs) == ["iio", "pico"]
    assert next(x for x in srcs if x.id == "iio").device == "ads1115"
    s.set("meters.sim", True)
    assert "sim" in [x.id for x in meters.sources_from_settings(s, hw)]


def test_cli_hardware(tmp_path, monkeypatch, capsys):
    from canopener import cli
    monkeypatch.setattr("canopener.core.settings.path", lambda: str(tmp_path / "settings.json"))
    assert cli.main(["hardware", "--stage", "1", "--set", "esp32=auto"]) == 0
    out = capsys.readouterr().out
    assert "can_a" in out and "Not installed" in out
    st = json.load(open(tmp_path / "settings.json"))["hardware"]
    assert st["can_a"] == "yes" and st["esp32"] == "auto" and st["can_b"] == "no"


# ----------------------------------------------------------------------------
# SYNC
# ----------------------------------------------------------------------------
def test_pulse_clock_corrects_drift():
    clock = sync.PulseClock()
    drift = 50e-6                               # the Pico's crystal runs 50 ppm fast
    host0 = 1_700_000_000.0
    for n in range(20):
        edge = host0 + n * 0.1
        dev = 12.0 + n * 0.1 * (1 + drift)
        if n % 2:                               # either side may arrive first
            clock.device_pulse(dev, edge + 0.002)
            clock.host_pulse(edge)
        else:
            clock.host_pulse(edge)
            clock.device_pulse(dev, edge + 0.002)
    assert clock.locked and len(clock.pairs()) == 20
    dev = 12.0 + 1.55 * (1 + drift)             # between pulses 15 and 16
    assert abs(clock.to_host(dev) - (host0 + 1.55)) < 1e-6
    assert abs(clock.to_host(12.0 + 2.5 * (1 + drift)) - (host0 + 2.5)) < 1e-6   # extrapolated


def test_pulse_clock_ignores_lines_far_from_any_edge():
    clock = sync.PulseClock()
    clock.host_pulse(100.0)
    clock.device_pulse(5.0, 100.08)             # 80 ms after the edge: not that pulse
    clock.host_pulse(100.1)
    clock.device_pulse(5.1, 100.101)
    assert clock.pairs() == [(5.1, 100.1)] and clock.to_host(5.1) is None


def test_parse_assert_and_pps_reader(tmp_path):
    assert sync.parse_assert("1730471234.123456789#42\n") == (1730471234.123456789, 42)
    assert sync.parse_assert("garbage") is None
    d = tmp_path / "pps0"
    d.mkdir()
    clock = sync.PulseClock()
    r = sync.PpsReader(clock, folder=str(d))
    (d / "assert").write_text("100.000000000#1\n")
    r.poll_once(str(d))
    r.poll_once(str(d))                         # same edge read twice counts once
    (d / "assert").write_text("100.100000000#2\n")
    r.poll_once(str(d))
    assert r.edges == 2


def test_meter_sync_lines_feed_the_clock_and_time_readings():
    clock = sync.PulseClock()
    got = []
    src = meters.LineSource("pico", "Meter Pico", opener=None, pulse_clock=clock, on_options=got.append)
    samples = []
    src._sink = samples.append
    src.handle('{"hello": "canopener-meter", "options": ["dac", "src"], "channels": [{"id": "p1"}]}')
    assert got == [["dac", "src"]] and src.options == ["dac", "src"]
    for n in range(3):
        clock.host_pulse(500.0 + n * 0.1)
        src.handle(json.dumps({"sync": n, "t_us": 1_000_000 + n * 100_000}), now=500.0 + n * 0.1 + 0.001)
    src.handle('{"ch": "p1", "v": 1.5, "t_us": 1150000}', now=999.0)
    assert abs(samples[-1].t - 500.15) < 1e-9


def test_send_writes_one_json_line():
    src = meters.LineSource("pico", "Meter Pico", opener=None)
    assert not src.send({"cmd": "hello"})       # not connected

    class Stream:
        buf = io.BytesIO()

        def write(self, b):
            self.buf.write(b)
            return True
    src._stream = Stream()
    assert src.send({"cmd": "rate", "hz": 20})
    assert Stream.buf.getvalue() == b'{"cmd": "rate", "hz": 20}\n'


# ----------------------------------------------------------------------------
# Logic Pico
# ----------------------------------------------------------------------------
def test_capture_argv_always_records_sync():
    argv = logic.capture_argv("/dev/ttyACM1", "/tmp/x.sr", samplerate=2_000_000, channels=["D3"], seconds=1.5)
    assert argv[1:] == ["-d", "raspberrypi-pico:conn=/dev/ttyACM1", "--config", "samplerate=2000000",
                        "--channels", "D2=SYNC,D3=Probe_1", "-o", "/tmp/x.sr", "--time", "1500ms"]


def test_read_sr_and_fit_sync_edges(tmp_path):
    rate = 1000                                 # 1 kHz, SYNC on bit 0 every 100 samples
    data = bytes((1 if (i % 100) < 5 and i >= 100 else 0) | 2 for i in range(1000))
    path = tmp_path / "logic.sr"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("version", "2")
        z.writestr("metadata", "[global]\nsigrok version=0.6.0\n\n[device 1]\ncapturefile=logic-1\n"
                               "total probes=2\nsamplerate=1 kHz\nprobe1=SYNC\nprobe2=Probe_1\nunitsize=1\n")
        z.writestr("logic-1-1", data[:500])
        z.writestr("logic-1-2", data[500:])
    edges, sr = logic.sync_edges(str(path))
    assert sr == 1000 and edges == [100, 200, 300, 400, 500, 600, 700, 800, 900]
    host = [10.0 + 0.1 * k + 0.0004 for k in range(1, 10)]
    pairs = sync.fit_edges(edges, rate, host, approx_start=10.003)
    assert len(pairs) == 9
    assert abs(sync.interpolate(pairs, 150) - 10.1504) < 1e-9


def test_firmware_sync_period_matches_the_pi():
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(root, "firmware", "meter-pico", "config.py")).read())
    consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert consts["SYNC_PERIOD_MS"] == round(sync.PERIOD_S * 1000)
    assert consts["SYNC"] == 15                       # docs/ELECTRONICS.md 5.6


def test_waveshare_hat_needs_both_channels_and_takes_the_shared_pins(tmp_path):
    hw = _hw(tmp_path)
    root = str(tmp_path / "root")
    assert hardware.BY_ID["can_hat"].stage not in hardware.STAGES        # never switched on by a build stage
    hw.set_stage("8")
    assert hw.state("can_hat") == hardware.NO
    for dev, name in (("spi0.0", "can0"),):
        os.makedirs(os.path.join(root, "sys/bus/spi/devices", dev, "net", name))
    hw.set_state("can_hat", hardware.YES)
    st = hw.status("can_hat")
    assert st.level == hardware.BAD and not st.detection.found           # one channel isn't the board
    os.makedirs(os.path.join(root, "sys/bus/spi/devices/spi1.0/net/can1"))
    hw._cache.clear()
    assert hw.status("can_hat").text == "Found: can0, can1"
    # the parts on its pins are left out, and say why
    for oid in ("can_a", "can_b", "sync"):
        st = hw.status(oid)
        assert not hw.available(oid) and st.level == hardware.BAD and "Waveshare" in st.text, oid
    assert not hw.clash("controls")                 # the buttons stay: the encoder's A line moves to GPIO7
    assert hw.available("adc_pi") or hw.state("adc_pi") == hardware.YES
    hw.set_state("can_hat", hardware.NO)
    assert hw.available("can_a")


def test_can_ports_are_named_by_connector_not_by_linux_name(tmp_path):
    hw = _hw(tmp_path)
    root = str(tmp_path / "root")
    assert hw.can_ports() == []                                          # nothing on board: USB adapters aren't ports
    hw.set_state("can_hat", hardware.YES)
    # a USB adapter took can0, so the HAT's channels came up as can1 / can2
    os.makedirs(os.path.join(root, "sys/bus/spi/devices/spi0.0/net/can1"))
    os.makedirs(os.path.join(root, "sys/bus/spi/devices/spi1.0/net/can2"))
    hw._cache.clear()
    assert [(p.id, p.label, p.iface) for p in hw.can_ports()] == [("can1", "CAN 1", "can1"), ("can2", "CAN 2", "can2")]
    hw.set_state("can_hat", hardware.NO)
    hw.set_state("can_a", hardware.YES)
    assert [(p.label, p.iface, p.option) for p in hw.can_ports()] == [("CAN 1", "can1", "can_a")]


def test_side_buttons_need_the_adc_and_find_its_a3_input(tmp_path):
    """Back, Hold, Record and Timestamp on a resistor ladder into the ADS1115's A3: no GPIO, an
    Installed option that needs the ADC, and A3 is then no meter input."""
    from canopener.core import meters
    from canopener.device import input as inp
    hw = _hw(tmp_path)
    opt = hardware.BY_ID["side_buttons"]
    assert opt.requires == ("adc_pi",) and opt.stage not in hardware.STAGES
    hw.set_state("side_buttons", hardware.YES)
    assert not hw.available("side_buttons")                 # the ADC isn't installed
    hw.set_state("adc_pi", hardware.YES)
    iio = tmp_path / "iio"
    dev = iio / "iio:device0"
    dev.mkdir(parents=True)
    (dev / "name").write_text("ads1115\n")
    for i in range(4):
        (dev / f"in_voltage{i}_raw").write_text("0\n")
    (dev / "in_voltage3_scale").write_text("0.125\n")   # millivolts per count
    assert inp.iio_channel(sys_iio=str(iio)) == str(dev / "in_voltage3_raw")
    assert inp.iio_channel(names=("other",), sys_iio=str(iio)) is None
    src = meters.IioSource(str(dev), skip=(3,))
    assert src._find() and [c.id for c in src.channels()] == ["iio.a0", "iio.a1", "iio.a2"]
    levels = [0.0, 0.60, 1.18, 1.98]
    lad = inp.LadderButtons(levels, list(inp.SIDE_ACTIONS), inp.iio_volts_reader(str(dev / "in_voltage3_raw")))
    (dev / "in_voltage3_raw").write_text("26400\n")        # 3.3 V: nothing pressed
    assert lad.poll() == [] and lad.poll() == []
    (dev / "in_voltage3_raw").write_text("4800\n")         # 0.60 V: Hold
    assert lad.poll() == [] and lad.poll() == [("down", inp.HOLD)]
    (dev / "in_voltage3_raw").write_text("15840\n")        # 1.98 V: Timestamp
    lad.poll()
    assert lad.poll() == [("up", inp.HOLD), ("down", inp.TAG)]
    side = inp.side_ladder(levels, sys_iio=str(iio))
    try:
        import time
        for _ in range(50):
            got = side.poll()
            if got:
                break
            time.sleep(0.02)
        assert got == [("down", inp.TAG)]
    finally:
        side.stop()
    assert inp.side_ladder(levels, sys_iio=str(tmp_path / "none")) is None
