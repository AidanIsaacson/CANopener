"""Installed options: which on-deck hardware this unit actually has.

The handheld is built in stages (docs/ELECTRONICS.md, "Stages"): a bench Pi with one CAN stack,
then the Picos, an ADC, the second CAN stack, the ESP32, the probe front end ... A feature that
needs a part (the ohms mode needs the meter Pico and its ohms channel) asks here first, so the UI
never offers, polls or waits on hardware that isn't fitted.

Each ``Option`` is set to one of:

* ``no`` (the default): not fitted. Its features are hidden and nothing ever touches it.
* ``yes``: fitted. Its features are offered; if it can't be found that's a fault, shown in red
  with a hint and retried (the "Pico not detected, Retry?" pattern from DESIGN.md).
* ``auto``: use it if it's found. Handy on a bench Pi whose parts come and go.

Two rules keep a missing part from freezing anything:

1. **Detection only looks.** ``detect()`` checks sysfs and ``/dev`` entries (does ``can0``
   exist, is there a pps device, an IIO ADC with that name, a serial port matching the Pico's
   USB name). It never opens a device, so it can't block.
2. **Talking to hardware stays on its own thread** with timeouts, as every
   ``core.measure.MeasurementSource`` already does. The options decide what gets started; the
   threads make sure a part that dies mid-session is a status lamp, not a hang.

Options behind the meter Pico (the ADS8584S, the ohms channel, the test source) can't be seen
from the Pi: the meter firmware probes its own I2C / SPI parts at boot and lists what answered in
its hello line (``"options": ["dac", "ina226", ...]``). ``Hardware.meter_report()`` takes that list.

Stored in ``core.settings`` (``hardware``: option id -> state), shared by every front-end.
Standard library only.
"""

import glob
import os
import time
from dataclasses import dataclass, field

YES, NO, AUTO = "yes", "no", "auto"
STATES = [("Not installed", NO), ("Installed", YES), ("Use if found", AUTO)]

# Build stages in order (docs/ELECTRONICS.md section 3). "Built to stage N" fits everything up to N.
STAGES = ["1", "2a", "2b", "3", "4", "5", "6", "7", "7b", "8"]

# Source-status levels, the same lamp colours as core.measure / core.link
OK, WAIT, BAD, OFF = "ok", "wait", "bad", "off"

# USB serial names (/dev/serial/by-id) of each Pico's firmware. Set the port in settings
# (hardware.<id>.port) when both Picos look alike; a serial number makes it exact.
METER_PICO_NAMES = ["*CANopener*Meter*", "*MicroPython*"]
LOGIC_PICO_NAMES = ["*sigrok*", "*Raspberry_Pi_Pico*"]


@dataclass(frozen=True)
class Detection:
    found: bool | None          # None = can't be checked from the Pi (yet)
    where: str = ""             # what was found: "can0", "/dev/ttyACM0", "iio:device0 (ads1115)"
    detail: str = ""


@dataclass(frozen=True)
class Option:
    id: str
    name: str
    stage: str
    what: str                   # one line: what it is and what it's for
    hint: str                   # what to check when it's fitted but not found
    requires: tuple = ()        # option ids this needs (the ohms channel needs the meter Pico)
    detect: str = ""            # detection kind, see Probe.detect
    arg: object = None          # its argument (interface name, IIO names, USB name patterns ...)


OPTIONS: list[Option] = [
    Option("can_a", "CAN stack A", "1", "MCP2518FD + TCAN1051V on SPI0 CE0: can0, the DB9.",
           "dtoverlay=mcp251xfd,spi0-0,oscillator=20000000,interrupt=25 in config.txt; then "
           "`dmesg | grep mcp251xfd`. No can0 usually means wiring or the crystal.",
           detect="net", arg="can0"),
    Option("logic_pico", "Logic Pico", "2a", "Original Pico running stock sigrok-pico: the logic analyzer.",
           "Plugged in by USB? Flashed with sigrok-pico? Set hardware.logic_pico.port if it isn't recognised.",
           detect="serial", arg=LOGIC_PICO_NAMES),
    Option("meter_pico", "Meter Pico", "2b", "Pico 2 running the CANopener meter firmware: probes, "
           "thresholds, CAN monitors, sync pulse.",
           "Plugged in by USB? Flashed with firmware/meter-pico? Set hardware.meter_pico.port if it isn't "
           "recognised.", detect="serial", arg=METER_PICO_NAMES),
    Option("sync", "Sync pulse input", "2b", "The meter Pico's SYNC pulse into GPIO21, time-stamped by the "
           "kernel (pps-gpio): lines Pico readings up with CAN frames.",
           "dtoverlay=pps-gpio,gpiopin=21 in config.txt; look for /dev/pps0.",
           requires=("meter_pico",), detect="pps"),
    Option("adc_pi", "ADC on the Pi", "3", "ADS1115 on I2C1: four slow probe voltages before the meter Pico "
           "exists. Only through the divider + clamp front end, never bare probes.",
           "dtoverlay=ads1115 in config.txt (check `dtoverlay -h ads1115`); `i2cdetect -y 1` should show 48.",
           detect="iio", arg=("ads1115", "ads1015")),
    Option("can_b", "CAN stack B", "5", "Second MCP2518FD on SPI0 CE1: can1, a second bus at the same time.",
           "dtoverlay=mcp251xfd,spi0-1,oscillator=20000000,interrupt=20 in config.txt.",
           detect="net", arg="can1"),
    Option("esp32", "ESP32 wireless", "6", "ESP-NOW receiver for satellite meters and Bluetooth gateway, on "
           "the Pi UART (GPIO14/15).",
           "Pi 5: dtparam=uart0=on in config.txt. 3B+: enable_uart=1 and dtoverlay=disable-bt. No serial "
           "console on it (raspi-config). Port: hardware.esp32.port (default /dev/ttyAMA0).",
           detect="port", arg="/dev/ttyAMA0"),
    Option("probe_frontend", "Probe front end", "7", "Attenuators, comparators and the MCP4728 threshold DAC: "
           "probe voltages on the meter Pico and probes 1-4 as logic channels.",
           "The meter Pico didn't find the MCP4728 (0x60) on its I2C0 (GP4/GP5).",
           requires=("meter_pico",), detect="meter", arg="dac"),
    Option("ohms", "Resistance channel", "7", "Excitation switch, 2.5 V reference and the ADS1115 (0x48) on the "
           "meter Pico: ohms and continuity on one probe.",
           "The meter Pico didn't find the ADS1115 (0x48) on its I2C0.",
           requires=("meter_pico",), detect="meter", arg="ohms"),
    Option("src", "Component test source", "7b", "Current-limited 5 V on probe 4 with the INA226 (0x40) and "
           "pull-ups on probes 1-2, for powering a sensor off the machine.",
           "The meter Pico didn't find the INA226 (0x40) on its I2C0.",
           requires=("meter_pico",), detect="meter", arg="src"),
    Option("ads8584s", "Waveform ADC", "8", "ADS8584S: four probes sampled together, up to 330 kSPS: the scope.",
           "The meter Pico got no answer from the ADS8584S (BUSY never went low after CONVST).",
           requires=("meter_pico", "probe_frontend"), detect="meter", arg="ads8584s"),
]
BY_ID = {o.id: o for o in OPTIONS}


def stage_index(stage: str) -> int:
    return STAGES.index(stage) if stage in STAGES else len(STAGES)


def states_for_stage(stage: str) -> dict:
    """Every option up to and including ``stage`` installed, the rest not: the starting point
    for "I've built up to stage N" (then change single options either way)."""
    n = stage_index(stage)
    return {o.id: (YES if stage_index(o.stage) <= n else NO) for o in OPTIONS}


class Probe:
    """Looks for each option's hardware without opening anything. ``root`` is "/" on the Pi and
    a temporary folder in tests."""

    def __init__(self, root: str = "/"):
        self.root = root

    def _p(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip("/"))

    def net(self, iface: str) -> Detection:
        d = self._p(f"/sys/class/net/{iface}")
        if not os.path.isdir(d):
            return Detection(False, detail=f"no {iface} interface")
        drv = os.path.realpath(os.path.join(d, "device", "driver"))
        driver = os.path.basename(drv) if os.path.exists(drv) else ""
        return Detection(True, iface, f"driver {driver}" if driver else "")

    def serial(self, patterns, port: str = "") -> Detection:
        if port:
            ok = os.path.exists(self._p(port))
            return Detection(ok, port if ok else "", "" if ok else f"{port} not present")
        import fnmatch
        names = sorted(os.path.basename(p) for p in glob.glob(self._p("/dev/serial/by-id/*")))
        for pat in patterns:
            hit = next((n for n in names if fnmatch.fnmatch(n, pat)), None)
            if hit:
                return Detection(True, f"/dev/serial/by-id/{hit}")
        return Detection(False, detail="no matching USB serial device")

    def port(self, path: str) -> Detection:
        ok = os.path.exists(self._p(path))
        return Detection(ok, path if ok else "", "" if ok else f"{path} not present")

    def pps(self, _arg=None) -> Detection:
        for d in sorted(glob.glob(self._p("/sys/class/pps/pps*"))):
            name = _read(os.path.join(d, "name"))
            if name and "gpio" in name:
                return Detection(True, f"/dev/{os.path.basename(d)}", name)
        return Detection(False, detail="no pps-gpio device")

    def iio(self, names) -> Detection:
        for d in sorted(glob.glob(self._p("/sys/bus/iio/devices/iio:device*"))):
            name = _read(os.path.join(d, "name"))
            if name in names:
                return Detection(True, f"{os.path.basename(d)} ({name})", name)
        return Detection(False, detail=f"no IIO device named {' / '.join(names)}")

    def detect(self, opt: Option, settings=None, reported: list | None = None) -> Detection:
        port = (settings.get(f"hardware.{opt.id}.port") if settings is not None else "") or ""
        if opt.detect == "net":
            return self.net(opt.arg)
        if opt.detect == "serial":
            return self.serial(opt.arg, port)
        if opt.detect == "port":
            return self.port(port or opt.arg)
        if opt.detect == "pps":
            return self.pps()
        if opt.detect == "iio":
            return self.iio(opt.arg)
        if opt.detect == "meter":
            if reported is None:
                return Detection(None, detail="waiting for the meter Pico to report its parts")
            return Detection(opt.arg in reported, "meter Pico" if opt.arg in reported else "")
        return Detection(None)


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


@dataclass
class OptionStatus:
    option: Option
    state: str                  # YES / NO / AUTO
    detection: Detection
    available: bool             # its features can be offered
    level: str                  # lamp: OK / WAIT / BAD / OFF
    text: str                   # short: "Found: can0", "Missing", "Not installed"
    detail: str = ""


@dataclass
class Hardware:
    """What's fitted (from settings) and what's found (from ``Probe``), with a short cache so a
    screen can ask every frame."""

    settings: object = None
    probe: Probe = field(default_factory=Probe)
    cache_s: float = 2.0
    _cache: dict = field(default_factory=dict)
    _reported: list | None = None

    def state(self, opt_id: str) -> str:
        states = (self.settings.get("hardware") if self.settings is not None else None) or {}
        v = states.get(opt_id, NO)
        return v if v in (YES, NO, AUTO) else NO

    def set_state(self, opt_id: str, state: str):
        if opt_id not in BY_ID or state not in (YES, NO, AUTO):
            raise ValueError(f"unknown option or state: {opt_id}={state}")
        states = dict(self.settings.get("hardware") or {})
        states[opt_id] = state
        self.settings.set("hardware", states)
        self._cache.clear()

    def set_stage(self, stage: str):
        self.settings.set("hardware", states_for_stage(stage))
        self._cache.clear()

    def meter_report(self, options: list | None):
        """The parts the meter Pico found (from its hello), or None when it's gone."""
        self._reported = None if options is None else [str(o) for o in options]
        self._cache.clear()

    def detect(self, opt_id: str) -> Detection:
        now = time.monotonic()
        hit = self._cache.get(opt_id)
        if hit and now - hit[0] < self.cache_s:
            return hit[1]
        det = self.probe.detect(BY_ID[opt_id], self.settings, self._reported)
        self._cache[opt_id] = (now, det)
        return det

    def installed(self, opt_id: str) -> bool:
        """Fitted as far as features go: "yes", or "auto" and found. Doesn't check ``requires``."""
        st = self.state(opt_id)
        return st == YES or (st == AUTO and self.detect(opt_id).found is True)

    def available(self, opt_id: str) -> bool:
        """Offer this option's features: it and everything it needs are installed."""
        opt = BY_ID[opt_id]
        return self.installed(opt_id) and all(self.available(r) for r in opt.requires)

    def status(self, opt_id: str) -> OptionStatus:
        opt, st = BY_ID[opt_id], self.state(opt_id)
        if st == NO:
            return OptionStatus(opt, st, Detection(None), False, OFF, "Not installed")
        det = self.detect(opt_id)
        avail = self.available(opt_id)
        missing = [BY_ID[r].name for r in opt.requires if not self.available(r)]
        if missing:
            return OptionStatus(opt, st, det, False, BAD if st == YES else OFF, "Needs " + ", ".join(missing),
                                "Set what it needs to Installed too.")
        if det.found:
            return OptionStatus(opt, st, det, avail, OK, f"Found: {det.where}" if det.where else "Found", det.detail)
        if det.found is None:
            return OptionStatus(opt, st, det, avail, WAIT, "Not checked yet", det.detail)
        if st == AUTO:
            return OptionStatus(opt, st, det, False, OFF, "Not found", det.detail)
        return OptionStatus(opt, st, det, avail, BAD, "Missing", opt.hint)

    def report(self) -> list[OptionStatus]:
        return [self.status(o.id) for o in OPTIONS]

    def port(self, opt_id: str) -> str:
        """The serial port of a serial option, as set or as found ("" if neither)."""
        set_port = self.settings.get(f"hardware.{opt_id}.port") if self.settings is not None else ""
        if set_port:
            return set_port
        det = self.detect(opt_id)
        return det.where if det.found and det.where.startswith("/dev/") else ""
