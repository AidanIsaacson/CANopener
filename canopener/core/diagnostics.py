"""Diagnostics for the installed options: the few commands worth running when a part isn't found.

Each option (``core.hardware.OPTIONS``) has a short list of ``Check``s: the config.txt lines that
load its overlay, the kernel's messages about its driver, what sysfs and ``/dev`` show, and a
command or two (``ip link``, ``lsusb``, ``i2cdetect``). ``run`` runs them and ``report`` turns the
results into a text file a person can read or send on; ``save`` writes it to the logs folder
(``~/canopener/diagnostics/``).

Everything here only reads. No root (``dmesg`` falls back to ``journalctl -k`` where it's
restricted), every command has a timeout, and missing tools are reported rather than fatal.
Shared by the handheld (System > Installed options > Diagnostics) and
``canopener-cli hardware --diagnose``. Standard library only.
"""

import glob
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass

from . import config

DIAG_DIR = "diagnostics"
TIMEOUT = 5.0
MAX_LINES = 60          # per check: the newest lines win for kernel messages
CONFIG_FILES = ["/boot/firmware/config.txt", "/boot/config.txt"]
CONFIG_LINES = r"^\s*(\[|dtoverlay|dtparam|gpio=|enable_uart)"


@dataclass(frozen=True)
class Check:
    title: str
    kind: str               # cmd / kernel / file / list / names / can_iface / meter
    arg: object = None      # argv, a path or paths, a glob, an SPI device ...
    grep: str = ""          # keep only lines matching this (case-insensitive)
    why: str = ""           # what to look for, in a line


@dataclass
class Result:
    check: Check
    shown: str              # the command as a person would type it
    output: str
    ran: bool = True        # False: the tool or file wasn't there (not a verdict on the hardware)


def _config(grep: str = CONFIG_LINES, why: str = "") -> Check:
    return Check("config.txt", "file", CONFIG_FILES, grep,
                 why or "The overlay lines for this part, under [all] (not under a [cm4] or [pi4] section).")


def _kernel(pattern: str, why: str) -> Check:
    return Check("Kernel messages", "kernel", None, pattern, why)


CHECKS: dict[str, list[Check]] = {
    "usb_can": [
        Check("USB devices", "cmd", ["lsusb"], why="A CANable shows as 1d50:606f (candleLight) or 16d0:117e (slcan)."),
        Check("CAN interfaces", "cmd", ["ip", "-br", "link"], "can", "can0 / can1 once the driver has it."),
        Check("USB serial ports", "list", "/dev/serial/by-id/*", why="An slcan CANable is a serial port first."),
        _kernel(r"gs_usb|slcan|\bcan\d|cdc_acm", "gs_usb binding the adapter, or slcan attaching."),
    ],
    "controls": [
        _config(r"^\s*(\[|dtoverlay=gpio-key|dtoverlay=rotary|gpio=)", "One gpio-key line per button and the "
                "rotary-encoder line (config-controls.txt)."),
        Check("Input devices", "file", ["/proc/bus/input/devices"], r"^N: Name",
              "A device per button (gpio-key) and one for the encoder (rotary)."),
    ],
    "can_a": [
        _config(r"^\s*(\[|dtparam=spi|dtoverlay=mcp251)", "dtparam=spi=on and the mcp251xfd line for spi0-0."),
        Check("SPI devices", "list", "/sys/bus/spi/devices/*", why="spi0.0 once SPI and the overlay are on."),
        Check("Interface on spi0.0", "list", "/sys/bus/spi/devices/spi0.0/net/*",
              why="What Installed options looks for. Empty: the driver didn't start (see the kernel messages)."),
        _kernel(r"mcp251|\bspi\b|spi\d", "Nothing: the overlay didn't load. 'Failed to detect' / oscillator not "
                "ready: no 3.3 V or the crystal. CRC or read errors: SPI wiring."),
        Check("Interface details", "can_iface", "spi0.0", why="Bitrate, state and error counters."),
        Check("CAN interfaces", "cmd", ["ip", "-br", "link"], "can"),
    ],
    "can_b": [
        _config(r"^\s*(\[|dtparam=spi|dtoverlay=mcp251)", "The mcp251xfd line for spi0-1."),
        Check("Interface on spi0.1", "list", "/sys/bus/spi/devices/spi0.1/net/*"),
        _kernel(r"mcp251|\bspi\b|spi\d", "As for stack A."),
        Check("Interface details", "can_iface", "spi0.1"),
    ],
    "logic_pico": [
        Check("USB serial ports", "list", "/dev/serial/by-id/*", why="sigrok-pico shows as a Raspberry Pi Pico."),
        Check("ACM ports", "list", "/dev/ttyACM*"),
        _kernel(r"cdc_acm|usb \d+-[\d.]+: (new|product|manufacturer)", "The Pico arriving on USB."),
    ],
    "meter_pico": [
        Check("USB serial ports", "list", "/dev/serial/by-id/*", why="MicroPython or the CANopener meter firmware."),
        Check("ACM ports", "list", "/dev/ttyACM*"),
        _kernel(r"cdc_acm|usb \d+-[\d.]+: (new|product|manufacturer)", "The Pico arriving on USB."),
        Check("Parts the meter reported", "meter", why="From the meter firmware's hello."),
    ],
    "sync": [
        _config(r"^\s*(\[|dtoverlay=pps)", "dtoverlay=pps-gpio,gpiopin=21"),
        Check("pps devices", "names", "/sys/class/pps/pps*", why="One named pps-gpio."),
        _kernel(r"pps", "pps-gpio registering the source on GPIO21."),
    ],
    "adc_pi": [
        _config(r"^\s*(\[|dtparam=i2c|dtoverlay=ads1)", "dtparam=i2c_arm=on and dtoverlay=ads1115."),
        Check("IIO devices", "names", "/sys/bus/iio/devices/iio:device*", why="One named ads1115 (or ads1015)."),
        Check("I2C bus 1", "cmd", ["i2cdetect", "-y", "1"],
              why="48 (or UU, taken by the driver). Needs i2c-tools; '--' everywhere: wiring or power."),
        _kernel(r"ads1|i2c", "The ADS1115 driver starting, or I2C errors."),
    ],
    "esp32": [
        _config(r"^\s*(\[|dtparam=uart|enable_uart|dtoverlay=(disable|miniuart)-bt)",
                "Pi 5: dtparam=uart0=on. 3B+: enable_uart=1 and dtoverlay=disable-bt."),
        Check("Serial ports", "list", "/dev/ttyAMA*", why="ttyAMA0 is the GPIO14/15 UART. On a Pi 5, ttyAMA10 is "
              "the debug connector, not the ESP32."),
        _kernel(r"ttyAMA|uart|pl011", "The UART being registered."),
    ],
}
for _oid in ("probe_frontend", "ohms", "src", "ads8584s"):
    CHECKS[_oid] = [Check("Parts the meter reported", "meter",
                          why="These parts sit behind the meter Pico, which lists what it found when it starts.")]

SYSTEM = [
    Check("Board", "file", ["/proc/device-tree/model"]),
    Check("Kernel", "cmd", ["uname", "-a"]),
    Check("Uptime", "cmd", ["uptime"]),
]


def checks_for(option_id: str) -> list[Check]:
    return list(CHECKS.get(option_id, []))


def _filter(text: str, grep: str, newest: bool = False) -> str:
    lines = text.splitlines()
    if grep:
        rx = re.compile(grep, re.I)
        lines = [ln for ln in lines if rx.search(ln)]
    if len(lines) > MAX_LINES:
        cut = len(lines) - MAX_LINES
        lines = (lines[-MAX_LINES:] if newest else lines[:MAX_LINES])
        lines = ([f"... {cut} earlier lines left out"] + lines) if newest else (lines + [f"... {cut} more lines"])
    return "\n".join(lines)


class Runner:
    """Runs checks. ``root`` prefixes every path (a temporary folder in tests); ``run_cmd(argv)``
    returns (exit code, output) and can be swapped in tests."""

    def __init__(self, root: str = "/", run_cmd=None, hardware=None, timeout: float = TIMEOUT):
        self.root = root
        self.hardware = hardware
        self.timeout = timeout
        self._run_cmd = run_cmd or self._subprocess

    def _p(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip("/"))

    def _subprocess(self, argv: list[str]) -> tuple[int | None, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout,
                               env={**os.environ, "LC_ALL": "C"}, errors="replace")
        except FileNotFoundError:
            return None, f"{argv[0]} isn't installed"
        except subprocess.TimeoutExpired:
            return 1, f"(no answer in {self.timeout:g} s)"
        except OSError as e:
            return 1, str(e)
        return p.returncode, (p.stdout + p.stderr).strip()

    def run(self, check: Check) -> Result:
        k = check.kind
        if k == "cmd":
            code, out = self._run_cmd(list(check.arg))
            shown = " ".join(check.arg) + (f"  (lines matching {check.grep!r})" if check.grep else "")
            return Result(check, shown, _filter(out, check.grep) if code is not None else out, code is not None)
        if k == "kernel":
            code, out = self._run_cmd(["dmesg"])
            shown = "dmesg"
            if code != 0 or not out:
                code, out = self._run_cmd(["journalctl", "-k", "-b", "--no-pager", "-q"])
                shown = "journalctl -k -b"
            text = _filter(out, check.grep, newest=True) if code == 0 else out
            return Result(check, f"{shown} | grep -iE '{check.grep}'", text or "(no matching lines)", code == 0)
        if k == "file":
            for path in check.arg:
                try:
                    with open(self._p(path), errors="replace") as f:
                        text = f.read().replace("\x00", "").strip()
                except OSError:
                    continue
                shown = f"cat {path}" + (f"  (lines matching {check.grep!r})" if check.grep else "")
                return Result(check, shown, _filter(text, check.grep) or "(no matching lines)")
            return Result(check, f"cat {' or '.join(check.arg)}", "not found", False)
        if k == "list":
            hits = sorted(glob.glob(self._p(check.arg)))
            return Result(check, f"ls {check.arg}", "\n".join(self._unroot(h) for h in hits) or "(nothing)")
        if k == "names":
            lines = []
            for d in sorted(glob.glob(self._p(check.arg))):
                lines.append(f"{os.path.basename(d)}: {_read(os.path.join(d, 'name')) or '?'}")
            return Result(check, f"cat {check.arg}/name", "\n".join(lines) or "(nothing)")
        if k == "can_iface":
            ifaces = sorted(glob.glob(self._p(f"/sys/bus/spi/devices/{check.arg}/net/*")))
            if not ifaces:
                return Result(check, "ip -details -statistics link show", f"(no CAN interface on {check.arg})")
            name = os.path.basename(ifaces[0])
            argv = ["ip", "-details", "-statistics", "link", "show", name]
            code, out = self._run_cmd(argv)
            return Result(check, " ".join(argv), out, code is not None)
        if k == "meter":
            rep = getattr(self.hardware, "_reported", None) if self.hardware is not None else None
            text = ("(the meter Pico hasn't reported yet: is it connected and running the meter firmware?)"
                    if rep is None else ", ".join(rep) or "(none)")
            return Result(check, "meter Pico hello: options", text)
        return Result(check, check.kind, "unknown check", False)

    def _unroot(self, path: str) -> str:
        root = os.path.join(self.root, "")
        return "/" + path[len(root):] if self.root != "/" and path.startswith(root) else path


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def run(option_ids: list[str], hardware=None, runner: Runner | None = None) -> dict[str, list[Result]]:
    """Every check for these options, plus a few lines about the system ("system")."""
    runner = runner or Runner(hardware=hardware)
    out = {"system": [runner.run(c) for c in SYSTEM]}
    for oid in option_ids:
        out[oid] = [runner.run(c) for c in checks_for(oid)]
    return out


def report(results: dict[str, list[Result]], hardware=None, version: str = "", now: float | None = None) -> str:
    """The results as plain text: what each option's status is, then each check's command and output."""
    from . import hardware as hw_mod
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    lines = [f"CANopener diagnostics, {stamp}", f"CANopener {version}".rstrip(), f"Host: {platform.node()}", ""]

    def section(title: str, res: list[Result]):
        for r in res:
            lines.append(f"--- {title}: {r.check.title}")
            lines.append(f"$ {r.shown}")
            if r.check.why:
                lines.append(f"# {r.check.why}")
            lines.extend((r.output or "(empty)").splitlines())
            lines.append("")

    section("System", results.get("system", []))
    for oid, res in results.items():
        if oid == "system":
            continue
        opt = hw_mod.BY_ID.get(oid)
        name = opt.name if opt else oid
        lines.append("=" * 72)
        lines.append(f"{name} ({oid}, stage {opt.stage if opt else '?'})")
        if hardware is not None and opt is not None:
            st = hardware.status(oid)
            lines.append(f"Set to: {st.state}. Status: {st.text}" + (f". {st.detail}" if st.detail else ""))
        lines.append("")
        section(name, res)
    return "\n".join(lines).rstrip() + "\n"


def save(text: str, folder: str | None = None, now: float | None = None) -> str:
    """Write a report to ``~/canopener/diagnostics/diagnostics-<date>-<time>.txt``; returns the path."""
    folder = folder or os.path.join(config.data_dir(), DIAG_DIR)
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, time.strftime("diagnostics-%Y%m%d-%H%M%S", time.localtime(now)))
    path, n = base + ".txt", 1
    while os.path.exists(path):         # two in the same second: never overwrite
        n += 1
        path = f"{base}_{n}.txt"
    with open(path, "w") as f:
        f.write(text)
    return path


def saved_reports(folder: str | None = None) -> list[str]:
    folder = folder or os.path.join(config.data_dir(), DIAG_DIR)
    return sorted(glob.glob(os.path.join(folder, "diagnostics-*.txt")))
