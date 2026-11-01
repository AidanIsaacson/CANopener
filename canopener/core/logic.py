"""The logic Pico: an original Pico running stock sigrok-pico, driven through ``sigrok-cli``.

Nothing custom runs on that Pico (docs/ELECTRONICS.md, D2); this module builds the
``sigrok-cli`` command, names the channels after what's wired to them, and reads the capture back
far enough to find the SYNC edges on D2, which put the whole capture on the CAN timeline
(``core.sync.fit_edges``). Protocol decoding (CAN, LIN, UART ...) stays sigrok's job.

Status: a sketch for stage 2a. Two things to confirm on the Pi first:

* sigrok-cli 0.7.2 (Debian / Pi OS) predates the ``raspberrypi-pico`` driver: build libsigrok
  and sigrok-cli from source (firmware/logic-pico/README.md). ``driver_available`` checks.
* sigrok's channel names (D2 ...) are assumed to be the Pico's GPIO numbers. Check before wiring.

A capture is a subprocess with a timeout, never a call that can hang the UI. Standard library
only.
"""

import configparser
import os
import re
import shutil
import subprocess
import zipfile

DRIVER = "raspberrypi-pico"

# What's wired to each channel (docs/ELECTRONICS.md 5.6). Front-ends show these names.
CHANNELS: dict[str, str] = {
    "D2": "SYNC",
    "D3": "Probe 1", "D4": "Probe 2", "D5": "Probe 3", "D6": "Probe 4",
    **{f"D{7 + i}": f"Header {i}" for i in range(8)},          # low-voltage logic header, 3.3/5 V
}
SYNC = "D2"


def sigrok_cli() -> str | None:
    return shutil.which("sigrok-cli")


def driver_available(timeout: float = 5.0) -> tuple[bool, str]:
    """Is sigrok-cli installed and new enough to know the Pico? (ok, why not). Runs a
    subprocess, so call it from a worker thread."""
    exe = sigrok_cli()
    if not exe:
        return False, "sigrok-cli isn't installed (build it from source: firmware/logic-pico/README.md)"
    try:
        out = subprocess.run([exe, "-L"], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"sigrok-cli -L failed: {e}"
    if DRIVER not in out:
        return False, f"this sigrok-cli has no {DRIVER} driver (0.7.2 predates it): build a newer one from source"
    return True, ""


def capture_argv(port: str, out_file: str, samplerate: int = 1_000_000, samples: int | None = None,
                 seconds: float | None = None, channels: list[str] | None = None,
                 trigger: str = "") -> list[str]:
    """The sigrok-cli command for one capture to a .sr file. ``channels`` are sigrok names
    (default: SYNC and the four probes); each gets its wired name (``D3=Probe_1``).
    ``trigger``: sigrok syntax, e.g. "D3=r" (rising edge on probe 1)."""
    chans = channels or [SYNC, "D3", "D4", "D5", "D6"]
    if SYNC not in chans:
        chans = [SYNC] + chans              # without SYNC the capture can't be placed on the timeline
    names = ",".join(f"{c}={CHANNELS.get(c, c).replace(' ', '_')}" for c in chans)
    argv = [sigrok_cli() or "sigrok-cli", "-d", f"{DRIVER}:conn={port}",
            "--config", f"samplerate={samplerate}", "--channels", names, "-o", out_file]
    if seconds is not None:
        argv += ["--time", f"{int(seconds * 1000)}ms"]
    else:
        argv += ["--samples", str(samples or samplerate)]
    if trigger:
        argv += ["--triggers", trigger]
    return argv


_RATE = re.compile(r"([\d.]+)\s*([kMG]?)Hz")


def parse_samplerate(text: str) -> float:
    """sigrok's "1 MHz" / "500 kHz" / "1000000" -> samples a second."""
    m = _RATE.search(text)
    if m:
        return float(m.group(1)) * {"": 1, "k": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]
    return float(text.strip())


def read_sr(path: str) -> dict:
    """A sigrok session file (.sr, a zip): samplerate, unit size, channel name -> bit, raw bytes."""
    with zipfile.ZipFile(path) as z:
        meta = configparser.ConfigParser(interpolation=None)
        meta.read_string(z.read("metadata").decode())
        dev = next(s for s in meta.sections() if s.startswith("device"))
        d = meta[dev]
        unitsize = int(d.get("unitsize", "1"))
        rate = parse_samplerate(d.get("samplerate", "0"))
        bits = {v: int(k[len("probe"):]) - 1 for k, v in d.items() if k.startswith("probe")}
        prefix = d.get("capturefile", "logic-1")
        chunks = sorted((n for n in z.namelist() if n == prefix or n.startswith(prefix + "-")),
                        key=lambda n: int(n.rsplit("-", 1)[1]) if n != prefix else 0)
        data = b"".join(z.read(n) for n in chunks)
    return {"samplerate": rate, "unitsize": unitsize, "bits": bits, "data": data}


def rising_edges(data: bytes, unitsize: int, bit: int) -> list[int]:
    """Sample numbers where ``bit`` goes 0 -> 1. Plain Python: fine for SYNC at 10 Hz in a capture
    of a few million samples; a long capture would want it chunked on a worker thread."""
    byte, mask = divmod(bit, 8)
    mask = 1 << mask
    out, prev = [], None
    for i in range(byte, len(data), unitsize):
        v = data[i] & mask
        if v and prev == 0:
            out.append(i // unitsize)
        prev = v
    return out


def sync_edges(path: str, name: str = CHANNELS[SYNC]) -> tuple[list[int], float]:
    """(SYNC rising-edge sample numbers, samplerate) of a capture, for ``core.sync.fit_edges``."""
    sr = read_sr(path)
    bit = sr["bits"].get(name.replace(" ", "_"), sr["bits"].get(SYNC))
    if bit is None:
        return [], sr["samplerate"]
    return rising_edges(sr["data"], sr["unitsize"], bit), sr["samplerate"]


def capture_file(folder: str) -> str:
    """Where a capture folder keeps its logic capture (listed in capture.json when that's wired up)."""
    return os.path.join(folder, "logic.sr")
