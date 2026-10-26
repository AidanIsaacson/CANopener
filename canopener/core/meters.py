"""Measurement sources: where meter readings come from (see ``core/measure.py`` for the model).

* ``SimulatedMeter``: four probe voltages from a pretend pedal-position sensor (the DESIGN.md
  use case: channel B drops out mid-travel), for trying the meter and the logger with no hardware.
* ``SerialLineSource``: a meter that prints one reading per line on a USB serial port: the Pico
  coprocessor, the ESP32 bridge for wireless satellites, or anything else that follows the line
  protocol below.
* ``IioSource``: an ADC the Linux kernel already drives (an ADS1115 via ``dtoverlay=ads1015``,
  an MCP3008, ...), read from sysfs: probe voltages on a bench Pi before the Pico board exists.

Every source keeps a ``status`` the front-ends show ("not detected, retrying" and so on), the
subsystem-fault pattern from DESIGN.md: a meter that drops out is reported and retried, and
the rest of the instrument carries on.

Line protocol (UTF-8, one message per line; JSON objects, or ``key=value`` pairs for tiny
firmware)::

    {"hello": "canopener-meter", "name": "Pico DMM",
     "channels": [{"id": "p1", "name": "Probe 1", "unit": "V", "decimals": 3, "kind": "voltage"}]}
    {"ch": "p1", "v": 4.512}                      reading now
    {"ch": "p1", "v": 4.512, "t_us": 81234567}    with the device's own clock (mapped to wall time)
    {"ch": "p1", "v": null, "flag": "OL"}          no valid reading, and why
    {"status": "error", "text": "probe 3 over voltage"}
    p1=4.512 p2=0.118                             the same, minimal (units from the hello, if any)

Channel ids get the source id in front ("pico.p1"), so two meters can both have a "p1".
"""

import glob
import json
import math
import os
import random
import select
import threading
import time

from .measure import BAD, OK, SIM, WAIT, Channel, ClockMapper, MeasurementSource, SourceStatus

RETRY_SECS = 2.0


class SimulatedMeter(MeasurementSource):
    """Four probes back-probing a two-track pedal sensor: 5 V supply, ground, channel A
    (0.5-4.5 V) and channel B (half of A). The pedal sweeps every few seconds and channel B
    drops out for a moment mid-travel now and then: the intermittent fault the logger is for."""

    id = "sim"
    name = "Simulated meter"

    def __init__(self, rate: float = 20.0, sweep_s: float = 6.0, seed=None):
        super().__init__()
        self.rate = rate
        self.sweep_s = sweep_s
        self._rng = random.Random(seed)
        self._stop = threading.Event()
        self._thread = None
        self._chs = [
            Channel("sim.p1", "Probe 1 (supply)", "V", self.id, 3, "voltage", 4.75, 5.25),
            Channel("sim.p2", "Probe 2 (ground)", "V", self.id, 3, "voltage", -0.1, 0.1),
            Channel("sim.p3", "Probe 3 (signal A)", "V", self.id, 3, "voltage", 0.25, 4.75),
            Channel("sim.p4", "Probe 4 (signal B)", "V", self.id, 3, "voltage", 0.1, 2.4),
        ]

    def channels(self) -> list[Channel]:
        return list(self._chs)

    def values(self, t: float) -> list[float]:
        """The four readings at time ``t`` (seconds), noise included."""
        phase = (t % self.sweep_s) / self.sweep_s
        pedal = 1 - abs(2 * phase - 1)                   # 0 -> 1 -> 0 triangle
        a = 0.5 + 4.0 * pedal
        b = a / 2
        cycle = int(t // self.sweep_s)
        if cycle % 3 == 2 and 0.42 < phase < 0.46:       # every third sweep, B drops out mid-travel
            b = 0.02
        n = self._rng.gauss
        return [5.0 + n(0, 0.004), n(0, 0.002), a + n(0, 0.003), b + n(0, 0.003)]

    def start(self, sink):
        super().start(sink)
        self._stop.clear()
        self.status = SourceStatus(SIM, "Simulated", "Simulated readings, no hardware.")
        self._thread = threading.Thread(target=self._run, name="sim-meter", daemon=True)
        self._thread.start()

    def _run(self):
        t0 = time.time()
        period = 1.0 / self.rate
        while not self._stop.wait(period):
            now = time.time()
            for ch, v in zip(self._chs, self.values(now - t0)):
                self.emit(ch.id, v, now)

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
        super().stop()


# ----------------------------------------------------------------------------
# Line protocol
# ----------------------------------------------------------------------------
def parse_line(line: str):
    """One line of the meter protocol -> ("hello", dict) / ("reading", dict) / ("status", dict),
    a list of readings for key=value lines, or None for blank / unreadable lines."""
    line = line.strip()
    if not line:
        return None
    if line.startswith("{"):
        try:
            d = json.loads(line)
        except ValueError:
            return None
        if not isinstance(d, dict):
            return None
        if "hello" in d:
            return "hello", d
        if "ch" in d:
            return "reading", d
        if "status" in d:
            return "status", d
        return None
    out = []
    for part in line.replace(",", " ").split():
        key, sep, val = part.partition("=")
        if not sep or not key:
            continue
        try:
            out.append(("reading", {"ch": key, "v": float(val)}))
        except ValueError:
            out.append(("reading", {"ch": key, "v": None, "flag": val[:20]}))
    return out or None


class FdReader:
    """Read chunks from a file descriptor with a timeout (a tty, or a pipe in tests)."""

    def __init__(self, fd: int):
        self.fd = fd

    def read(self, timeout: float) -> bytes | None:
        """Bytes available within ``timeout`` (b"" if none), or None once the other end is gone."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        data = os.read(self.fd, 4096)
        return data if data else None

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def open_serial(path: str) -> FdReader:
    """Open a USB serial port raw (no echo, no line editing). Baud rate doesn't matter for USB
    CDC devices like the Pico; it's set to 115200 for real UARTs."""
    import termios  # POSIX only; imported here so the module loads anywhere
    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0                                    # iflag
        attrs[1] = 0                                    # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0                                    # lflag: raw
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass                                            # not a tty (a FIFO): read it anyway
    return FdReader(fd)


class LineSource(MeasurementSource):
    """A meter speaking the line protocol over any byte stream. ``opener()`` returns an object
    with ``read(timeout)`` and ``close()`` (``FdReader``); it is called again after a dropout."""

    def __init__(self, source_id: str, name: str, opener, where: str = ""):
        super().__init__()
        self.id, self.name = source_id, name
        self.opener = opener
        self.where = where or name
        self._chs: dict[str, Channel] = {}
        self._clock = ClockMapper()
        self._stop = threading.Event()
        self._thread = None
        self.lines = 0

    def channels(self) -> list[Channel]:
        return list(self._chs.values())

    def start(self, sink):
        super().start(sink)
        self._stop.clear()
        self.status = SourceStatus(WAIT, "Connecting", f"Opening {self.where}")
        self._thread = threading.Thread(target=self._run, name=f"meter-{self.id}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        super().stop()

    def _run(self):
        while not self._stop.is_set():
            try:
                stream = self.opener()
            except OSError as e:
                self.status = SourceStatus(BAD, "Not detected", f"{self.where}: {e.strerror or e}. Retrying.")
                self._stop.wait(RETRY_SECS)
                continue
            self._clock.reset()
            self.status = SourceStatus(WAIT, "No readings yet", f"{self.where} is open; waiting for data.")
            buf = b""
            try:
                while not self._stop.is_set():
                    chunk = stream.read(0.5)
                    if chunk is None:
                        raise OSError("disconnected")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self.handle(line.decode(errors="replace"))
                    if len(buf) > 65536:        # garbage with no newlines: don't grow forever
                        buf = b""
            except OSError as e:
                self.status = SourceStatus(BAD, "Disconnected", f"{self.where}: {e}. Retrying.")
            finally:
                stream.close()
            self._stop.wait(RETRY_SECS)

    def _channel(self, key: str, unit: str = "") -> Channel:
        ch = self._chs.get(key)
        if ch is None:
            ch = self._chs[key] = Channel(f"{self.id}.{key}", key, unit, self.id)
        return ch

    def handle(self, line: str, now: float | None = None):
        """Process one line (public for tests and for sources fed some other way)."""
        msg = parse_line(line)
        if msg is None:
            return
        for kind, d in (msg if isinstance(msg, list) else [msg]):
            if kind == "hello":
                if d.get("name"):
                    self.name = str(d["name"])
                for c in d.get("channels") or []:
                    if isinstance(c, dict) and c.get("id"):
                        key = str(c["id"])
                        self._chs[key] = Channel.from_dict({**c, "id": f"{self.id}.{key}", "source": self.id,
                                                            "name": c.get("name", key)})
            elif kind == "status":
                level = OK if d.get("status") == "ok" else BAD
                self.status = SourceStatus(level, str(d.get("status", "")).capitalize(), str(d.get("text", "")))
            else:
                self.lines += 1
                ch = self._channel(str(d["ch"]), str(d.get("unit") or d.get("u") or ""))
                host = time.time() if now is None else now
                t = host
                if isinstance(d.get("t_us"), (int, float)):
                    t = self._clock.to_host(d["t_us"] / 1e6, host)
                v = d.get("v")
                v = float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None
                if self.status.level != OK:
                    self.status = SourceStatus(OK, "Reading", f"{self.where}: readings arriving.")
                self.emit(ch.id, v, t, str(d.get("flag") or ("" if v is not None else "no reading")))


class SerialLineSource(LineSource):
    def __init__(self, path: str, source_id: str = "serial", name: str = "Serial meter"):
        super().__init__(source_id, name, lambda: open_serial(path), where=path)
        self.path = path


# ----------------------------------------------------------------------------
# Linux IIO ADCs
# ----------------------------------------------------------------------------
SYS_IIO = "/sys/bus/iio/devices"


def iio_devices(sys_iio: str = SYS_IIO) -> list[tuple[str, str]]:
    """(device folder, driver name) of every IIO device with voltage inputs."""
    out = []
    for d in sorted(glob.glob(os.path.join(sys_iio, "iio:device*"))):
        if not glob.glob(os.path.join(d, "in_voltage*_raw")):
            continue
        try:
            with open(os.path.join(d, "name")) as f:
                name = f.read().strip()
        except OSError:
            name = os.path.basename(d)
        out.append((d, name))
    return out


def _read_float(path: str) -> float | None:
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


class IioSource(MeasurementSource):
    """Single-ended voltage inputs of a kernel ADC driver, polled from sysfs.

    ``device`` is the driver name ("ads1015") or a folder; ``ratio`` is the input divider
    (probe volts per ADC volt: 6.0 for the DESIGN.md 50k/10k divider)."""

    id = "iio"

    def __init__(self, device: str, ratio: float = 1.0, rate: float = 10.0, sys_iio: str = SYS_IIO):
        super().__init__()
        self.device = device
        self.ratio = ratio or 1.0
        self.rate = rate
        self.sys_iio = sys_iio
        self.name = f"ADC {device}"
        self.folder: str | None = None
        self._inputs: list[tuple[Channel, str, str]] = []     # (channel, raw file, scale file)
        self._stop = threading.Event()
        self._thread = None

    def channels(self) -> list[Channel]:
        return [c for c, _, _ in self._inputs]

    def _find(self) -> bool:
        folder = self.device if os.path.isdir(self.device) else next(
            (d for d, n in iio_devices(self.sys_iio) if n == self.device), None)
        if folder is None:
            return False
        self.folder = folder
        inputs = []
        for raw in sorted(glob.glob(os.path.join(folder, "in_voltage*_raw"))):
            idx = os.path.basename(raw)[len("in_voltage"):-len("_raw")]
            if not idx.isdigit():           # differential pairs (in_voltage0-voltage1_raw)
                continue
            scale = os.path.join(folder, f"in_voltage{idx}_scale")
            if not os.path.exists(scale):
                scale = os.path.join(folder, "in_voltage_scale")
            ch = Channel(f"iio.a{idx}", f"Input {idx}", "V", self.id, 3, "voltage")
            inputs.append((ch, raw, scale))
        self._inputs = inputs
        return bool(inputs)

    def read_once(self, now: float | None = None):
        now = time.time() if now is None else now
        ok = False
        for ch, raw_path, scale_path in self._inputs:
            raw, scale = _read_float(raw_path), _read_float(scale_path)
            if raw is None:
                self.emit(ch.id, None, now, "read error")
                continue
            ok = True
            self.emit(ch.id, raw * (scale if scale is not None else 1.0) / 1000.0 * self.ratio, now)
        return ok

    def start(self, sink):
        super().start(sink)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="meter-iio", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            if not self._inputs and not self._find():
                self.status = SourceStatus(BAD, "Not detected", f"No IIO ADC named {self.device}. Is its "
                                           "overlay in config.txt? Retrying.")
                self._stop.wait(RETRY_SECS)
                continue
            if self.read_once():
                self.status = SourceStatus(OK, "Reading", f"{self.folder}: {len(self._inputs)} input(s)")
            else:
                self.status = SourceStatus(BAD, "Read error", f"{self.folder} stopped answering. Retrying.")
                self._inputs = []
            self._stop.wait(1.0 / self.rate)

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
        super().stop()


def sources_from_settings(settings) -> list[MeasurementSource]:
    """The sources switched on in ``core.settings`` (meters.*)."""
    out: list[MeasurementSource] = []
    if settings.get("meters.sim"):
        out.append(SimulatedMeter())
    if settings.get("meters.serial"):
        out.append(SerialLineSource(settings.get("meters.serial")))
    if settings.get("meters.iio"):
        out.append(IioSource(settings.get("meters.iio"), float(settings.get("meters.iio_ratio") or 1.0)))
    return out
