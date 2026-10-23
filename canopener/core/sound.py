"""Short audible cues, standard library only, for a laptop speaker and/or a piezo buzzer.

A cue is a few (frequency, seconds) notes. Each one has its own rhythm as well as its own pitch,
so it still reads on an *active* buzzer that can only beep at one pitch.

Outputs:

* **speaker**: the notes as a WAV file, played with whichever command-line player exists
  (PipeWire ``pw-play``, PulseAudio ``paplay`` or ALSA ``aplay``).
* **buzzer**: a piezo driven by the kernel, two ways:
    - a beeper input device (``pwm-beeper`` or ``gpio-beeper`` driver from a device-tree overlay):
      ``/dev/input/eventN``, sent EV_SND tones. Found automatically by name.
    - a sysfs PWM channel: ``pwm:CHIP:CHANNEL`` = ``/sys/class/pwm/pwmchipCHIP/pwmCHANNEL``
      (on a Pi, ``dtoverlay=pwm,pin=18,func=2`` gives ``pwm:0:0``). Best for a passive piezo.
  The user needs write access (``input`` group for the event device, ``gpio`` group for PWM
  on Raspberry Pi OS).

``play`` never raises and never blocks: buzzer notes are timed on a short-lived thread.
"""

import glob
import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave

RATE = 22050
GAP = 0.04                  # silence between notes, so an active buzzer's notes stay separate
CUES = {
    "found": [(880, 0.10), (1175, 0.10), (1760, 0.35)],     # three rising, last one long: locked on
    "lost": [(880, 0.30), (587, 0.45)],                     # two long falling: traffic stopped
    "back": [(1175, 0.08), (1760, 0.08)],                   # two quick rising: traffic came back
    "test": [(1320, 0.15)],
}
PLAYERS = [["pw-play"], ["paplay"], ["aplay", "-q"]]
SPEAKER, BUZZER = "speaker", "buzzer"
AUTO = "auto"               # buzzer spec: find a beeper input device by itself

EV_SND, SND_TONE = 0x12, 0x02
SYS_INPUT = "/sys/class/input"
SYS_PWM = "/sys/class/pwm"


# ----------------------------------------------------------------------
# speaker
# ----------------------------------------------------------------------
def wav_bytes(notes, rate: int = RATE, volume: float = 0.8) -> bytes:
    """16-bit mono PCM for (frequency Hz, seconds) notes, each with a short fade so it doesn't click."""
    out = bytearray()
    for freq, secs in notes:
        n = int(rate * secs)
        fade = max(1, min(n // 2, int(rate * 0.01)))
        for k in range(n):
            env = min(1.0, k / fade, (n - 1 - k) / fade)
            out += struct.pack("<h", int(32767 * volume * env * math.sin(2 * math.pi * freq * k / rate)))
        out += b"\x00\x00" * int(rate * GAP)
    return bytes(out)


def cue_path(cue: str, folder: str | None = None) -> str:
    """The cue's WAV file, written on first use."""
    folder = folder or tempfile.gettempdir()
    uid = os.getuid() if hasattr(os, "getuid") else 0
    path = os.path.join(folder, f"canopener-{cue}-{uid}.wav")
    if not os.path.exists(path):
        tmp = f"{path}.{os.getpid()}.tmp"
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(wav_bytes(CUES[cue]))
        os.replace(tmp, path)
    return path


def player() -> list[str] | None:
    for argv in PLAYERS:
        exe = shutil.which(argv[0])
        if exe:
            return [exe, *argv[1:]]
    return None


def play_speaker(cue: str) -> bool:
    argv = player()
    if argv is None:
        return False
    try:
        subprocess.Popen([*argv, cue_path(cue)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


# ----------------------------------------------------------------------
# buzzer
# ----------------------------------------------------------------------
def find_beeper(sys_input: str = SYS_INPUT, dev_input: str = "/dev/input") -> str | None:
    """/dev/input/eventN of a writable kernel beeper (pwm-beeper / gpio-beeper), if there is one.
    Those drivers name their device after themselves. A PC's "PC Speaker" is left out (it's the
    old motherboard beeper, not a buzzer someone fitted); give its path explicitly to use it."""
    for ev in sorted(glob.glob(os.path.join(sys_input, "event*"))):
        try:
            with open(os.path.join(ev, "device", "name")) as f:
                name = f.read().strip().lower()
        except OSError:
            continue
        path = os.path.join(dev_input, os.path.basename(ev))
        if ("beep" in name or "buzzer" in name) and os.access(path, os.W_OK):
            return path
    return None


class InputBeeper:
    """A kernel beeper input device: EV_SND / SND_TONE with the frequency, 0 to stop."""

    def __init__(self, path: str):
        self.path = path

    def _send(self, f, hz: int):
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1e6)
        f.write(struct.pack("llHHi", sec, usec, EV_SND, SND_TONE, int(hz)))
        f.flush()

    def play(self, notes, sleep=time.sleep):
        with open(self.path, "wb", buffering=0) as f:
            try:
                for hz, secs in notes:
                    self._send(f, hz)
                    sleep(secs)
                    self._send(f, 0)
                    sleep(GAP)
            finally:
                self._send(f, 0)


class PwmBuzzer:
    """A sysfs PWM channel driving a piezo: period = 1/frequency, 50 % duty."""

    def __init__(self, chip: int, channel: int, sys_pwm: str = SYS_PWM):
        self.chip_dir = os.path.join(sys_pwm, f"pwmchip{chip}")
        self.channel = channel
        self.dir = os.path.join(self.chip_dir, f"pwm{channel}")

    @staticmethod
    def _write(path: str, value):
        with open(path, "w") as f:
            f.write(str(value))

    def _export(self):
        if not os.path.isdir(self.dir):
            self._write(os.path.join(self.chip_dir, "export"), self.channel)
            for _ in range(20):             # udev fixes up permissions just after export
                if os.access(os.path.join(self.dir, "enable"), os.W_OK):
                    break
                time.sleep(0.05)

    def _tone(self, hz: int):
        period = int(1e9 / hz)
        # Lower the duty cycle first: it must never exceed the period, old or new.
        self._write(os.path.join(self.dir, "duty_cycle"), 0)
        self._write(os.path.join(self.dir, "period"), period)
        self._write(os.path.join(self.dir, "duty_cycle"), period // 2)

    def play(self, notes, sleep=time.sleep):
        self._export()
        enable = os.path.join(self.dir, "enable")
        try:
            for hz, secs in notes:
                self._tone(hz)
                self._write(enable, 1)
                sleep(secs)
                self._write(enable, 0)
                sleep(GAP)
        finally:
            try:
                self._write(enable, 0)
            except OSError:
                pass


def buzzer_for(spec: str):
    """``auto`` (find a beeper device), ``/dev/input/eventN``, or ``pwm:CHIP:CHANNEL``. None if
    there's nothing to drive (``auto`` and no beeper, or an empty / ``off`` spec)."""
    spec = (spec or "").strip()
    if not spec or spec == "off":
        return None
    if spec == AUTO:
        path = find_beeper()
        return InputBeeper(path) if path else None
    if spec.startswith("pwm:"):
        try:
            chip, channel = (int(x) for x in spec[4:].split(":"))
        except ValueError:
            return None
        return PwmBuzzer(chip, channel)
    return InputBeeper(spec)


_busy = threading.Lock()


def play_buzzer(cue: str, spec: str = AUTO, wait: bool = False) -> bool:
    """Start the cue on the buzzer. False if there is no buzzer, or one is already sounding.
    With ``wait`` it plays on this thread and returns whether it worked."""
    dev = buzzer_for(spec)
    if dev is None or not _busy.acquire(blocking=False):
        return False

    def run() -> bool:
        try:
            dev.play(CUES[cue])
            return True
        except OSError:
            return False
        finally:
            _busy.release()

    if wait:
        return run()
    threading.Thread(target=run, name="buzzer", daemon=True).start()
    return True


# ----------------------------------------------------------------------
def play(cue: str, speaker: bool = True, buzzer: str = AUTO) -> bool:
    """Play a cue on the speaker and/or buzzer, whichever are enabled and present. Returns True
    if at least one started (the front-end can fall back to its own beep otherwise)."""
    if cue not in CUES:
        return False
    started = play_speaker(cue) if speaker else False
    return play_buzzer(cue, buzzer) or started
