"""The handheld's controls, turned into a few actions the screens understand.

Hardware (see docs/DEVICE.md for wiring and config.txt lines):

* **Buttons** (the soft keys along the top edge, the d-pad, the encoder's push) are GPIO pins
  with the kernel's ``gpio-key`` overlay: each one is a key press (F1-F5, arrows, Enter), so
  they arrive as ordinary keyboard events. No polling, kernel debouncing, and a USB keyboard
  does the same thing on the bench.
* **The rotary encoder** uses the ``rotary-encoder`` overlay, which reports relative motion
  (``REL_X``), not keys. ``EvdevReader`` reads it straight from ``/dev/input/eventN``.
* **A resistor-ladder button stack** (several buttons on one analog input) needs an ADC: the
  Pi has no analog inputs. ``LadderButtons`` decodes one ADC channel (an ADS1115 through
  the kernel IIO driver) into button presses, if the hardware goes that way.

Everything here is standard library; the app (``device/app.py``) feeds in key names from
Pygame.
"""

import glob
import os
import struct
import time

# Actions
UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
SELECT, BACK, HOME = "select", "back", "home"
SOFT = ("soft1", "soft2", "soft3", "soft4", "soft5")
BUTTON5 = "button5"         # the 5th top button: what it does is a setting (device.button5)
ROTATE = "rotate"           # encoder: comes with a +1 / -1 step count
LOG, TAG = "log", "tag"     # start/stop the data log, tag a timestamp: work from any screen
QUIT = "quit"
ROTATE_CW, ROTATE_CCW = "rotate+", "rotate-"

ACTION_NAMES = {
    UP: "Up", DOWN: "Down", LEFT: "Left", RIGHT: "Right", SELECT: "Select", BACK: "Back",
    HOME: "Home", LOG: "Start / stop log", TAG: "Timestamp", BUTTON5: "Button 5",
    ROTATE_CW: "Encoder clockwise", ROTATE_CCW: "Encoder anticlockwise",
    **{s: f"Soft key {i + 1}" for i, s in enumerate(SOFT)},
}

# Pygame key names (pygame.key.name) -> action. The gpio-key overlays in docs/DEVICE.md send
# these, so the same bindings work for the hardware and for a keyboard on the bench.
DEFAULT_KEYS = {
    "f1": SOFT[0], "f2": SOFT[1], "f3": SOFT[2], "f4": SOFT[3], "f5": BUTTON5,
    "up": UP, "down": DOWN, "left": LEFT, "right": RIGHT,
    "return": SELECT, "enter": SELECT,
    "escape": BACK, "backspace": BACK, "home": HOME,
    "page up": ROTATE_CCW, "page down": ROTATE_CW,
    "f9": LOG, "f10": TAG,
}
BUTTON5_CHOICES = {"back": BACK, "soft": SOFT[4], "log": LOG, "tag": TAG}


class KeyMap:
    def __init__(self, overrides: dict | None = None, button5: str = "back"):
        self.keys = {**DEFAULT_KEYS, **{str(k).lower(): v for k, v in (overrides or {}).items()}}
        self.button5 = BUTTON5_CHOICES.get(button5, BACK)

    def action(self, key_name: str) -> str | None:
        a = self.keys.get((key_name or "").lower())
        return self.button5 if a == BUTTON5 else a


class PressTracker:
    """Turns key downs / ups into actions. Holding Select for ``long_press`` seconds is Back (one
    hand, thumb on the encoder); everything else fires on press."""

    def __init__(self, long_press: float = 0.7):
        self.long_press = long_press
        self._select_down: float | None = None
        self._long_fired = False

    def down(self, action: str, now: float | None = None) -> str | None:
        if action == SELECT:
            self._select_down = time.monotonic() if now is None else now
            self._long_fired = False
            return None                     # decided on release (or when the hold gets long)
        return action

    def up(self, action: str, now: float | None = None) -> str | None:
        if action != SELECT or self._select_down is None:
            return None
        self._select_down = None
        return None if self._long_fired else SELECT

    def poll(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        if self._select_down is not None and not self._long_fired and now - self._select_down >= self.long_press:
            self._long_fired = True
            return BACK
        return None

    @property
    def holding(self) -> float:
        """Seconds Select has been held (for drawing a progress ring), 0 when not."""
        if self._select_down is None or self._long_fired:
            return 0.0
        return time.monotonic() - self._select_down


# ----------------------------------------------------------------------------
# evdev (the rotary encoder)
# ----------------------------------------------------------------------------
EV_KEY, EV_REL = 0x01, 0x02
REL_X, REL_DIAL, REL_WHEEL = 0x00, 0x07, 0x08
EVENT = struct.Struct("llHHi")      # struct input_event: timeval, type, code, value
SYS_INPUT = "/sys/class/input"


def input_devices(sys_input: str = SYS_INPUT) -> list[tuple[str, str, bool]]:
    """(``/dev/input/eventN``, name, has relative axes) for each input device."""
    out = []
    for d in sorted(glob.glob(os.path.join(sys_input, "event*"))):
        try:
            with open(os.path.join(d, "device", "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        try:
            with open(os.path.join(d, "device", "capabilities", "rel")) as f:
                rel = int(f.read().split()[-1] or "0", 16) != 0
        except (OSError, ValueError, IndexError):
            rel = False
        out.append((os.path.join("/dev/input", os.path.basename(d)), name, rel))
    return out


def find_encoder(sys_input: str = SYS_INPUT) -> str | None:
    """The rotary-encoder overlay's device ("rotary@..."), if there is one."""
    for path, name, rel in input_devices(sys_input):
        if rel and ("rotary" in name.lower() or "encoder" in name.lower()):
            return path
    return None


class EvdevReader:
    """Reads relative-motion events from one input device without blocking. Re-opens the device
    if it goes away (and finds it again when ``path`` is "auto")."""

    def __init__(self, path: str = "auto", sys_input: str = SYS_INPUT, axes=(REL_X, REL_DIAL, REL_WHEEL)):
        self.spec = path
        self.sys_input = sys_input
        self.axes = set(axes)
        self.path: str | None = None
        self.error: str | None = None
        self._fd: int | None = None
        self._next_try = 0.0

    def _open(self):
        if time.monotonic() < self._next_try:
            return
        self._next_try = time.monotonic() + 2.0
        self.path = find_encoder(self.sys_input) if self.spec == "auto" else self.spec
        if not self.path:
            self.error = "no rotary encoder found"
            return
        try:
            self._fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            self.error = None
        except OSError as e:
            self.error = f"{self.path}: {e.strerror} (is the user in the 'input' group?)"

    def read(self) -> int:
        """Encoder steps since the last call (clockwise positive)."""
        if self._fd is None:
            self._open()
            if self._fd is None:
                return 0
        total = 0
        try:
            while True:
                data = os.read(self._fd, EVENT.size * 32)
                if not data:
                    break
                for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, etype, code, value = EVENT.unpack_from(data, off)
                    if etype == EV_REL and code in self.axes:
                        total += value
        except BlockingIOError:
            pass
        except OSError as e:
            self.error = str(e)
            self.close()
        return total

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


# ----------------------------------------------------------------------------
# Resistor ladder
# ----------------------------------------------------------------------------
class LadderButtons:
    """Several buttons on one analog input: each pulls the input to its own voltage, and nothing
    pressed reads above the top level. ``levels`` are the expected volts per button, in order;
    a reading is the button whose level is nearest, within 40% of the gap to its neighbours (a
    reading halfway between two is ambiguous, so it counts as nothing pressed).
    A new button must read the same twice in a row (debounce). One button at a time: a ladder
    can't tell two pressed together, which is a reason to prefer one GPIO per button."""

    def __init__(self, levels: list[float], actions: list[str], read_volts=None):
        if len(levels) != len(actions):
            raise ValueError("one action per ladder level")
        self.levels = list(levels)
        self.actions = list(actions)
        self.read_volts = read_volts
        gaps = [b - a for a, b in zip(self.levels, self.levels[1:])] or [1.0]
        self.tolerance = min(gaps) * 0.4
        self.pressed: int | None = None
        self._candidate: int | None = None

    def decode(self, volts: float | None) -> int | None:
        if volts is None:
            return None
        best = min(range(len(self.levels)), key=lambda i: abs(self.levels[i] - volts))
        return best if abs(self.levels[best] - volts) <= self.tolerance else None

    def update(self, volts: float | None) -> list[tuple[str, str]]:
        """Feed one reading; returns [("down" / "up", action)] transitions."""
        b = self.decode(volts)
        if b != self._candidate:
            self._candidate = b
            return []
        if b == self.pressed:
            return []
        out = []
        if self.pressed is not None:
            out.append(("up", self.actions[self.pressed]))
        if b is not None:
            out.append(("down", self.actions[b]))
        self.pressed = b
        return out

    def poll(self) -> list[tuple[str, str]]:
        return self.update(self.read_volts() if self.read_volts else None)


def iio_volts_reader(raw_path: str):
    """A ``read_volts`` for ``LadderButtons`` from an IIO channel (``.../in_voltage0_raw``)."""
    scale_path = raw_path.replace("_raw", "_scale")
    if not os.path.exists(scale_path):
        scale_path = os.path.join(os.path.dirname(raw_path), "in_voltage_scale")

    def read() -> float | None:
        try:
            with open(raw_path) as f:
                raw = float(f.read())
            try:
                with open(scale_path) as f:
                    scale = float(f.read())
            except (OSError, ValueError):
                scale = 1.0
            return raw * scale / 1000.0
        except (OSError, ValueError):
            return None
    return read
