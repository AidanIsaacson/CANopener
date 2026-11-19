"""The handheld's controls, turned into a few actions the screens understand.

Hardware (see docs/DEVICE.md for wiring and config.txt lines):

* **Buttons** (the soft keys along the top edge, the d-pad, the encoder's push) are either GPIO
  pins with the kernel's ``gpio-key`` overlay, where each one is a key press (F1-F5, arrows,
  Enter) arriving as an ordinary keyboard event (a USB keyboard does the same on the bench), or
  resistor stacks on the button ADC (below).
* **The rotary encoder** uses the ``rotary-encoder`` overlay, which reports relative motion
  (``REL_X``), not keys. ``EvdevReader`` reads it straight from ``/dev/input/eventN``.
* **Resistor-ladder button stacks** (several buttons on one analog input) are on a second
  ADS1115 (the button ADC, I2C 0x49, through the kernel IIO driver): ``LadderButtons`` decodes
  one input into presses, ``ButtonAdc`` reads every stack and the battery on its own thread.

Everything here is standard library; the app (``device/app.py``) feeds in key names from
Pygame.
"""

import glob
import os
import struct
import threading
import time
from collections import deque

# Actions
UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
SELECT, BACK, HOME = "select", "back", "home"
SOFT = ("soft1", "soft2", "soft3", "soft4", "soft5")
BUTTON5 = "button5"         # the 5th top button: what it does is a setting (device.button5)
ROTATE = "rotate"           # encoder: comes with a +1 / -1 step count
LOG, TAG = "log", "tag"     # start/stop the data log, tag a timestamp: work from any screen
HOLD = "hold"               # freeze the values on the screen (the bus and the log keep going)
QUIT = "quit"
ROTATE_CW, ROTATE_CCW = "rotate+", "rotate-"

ACTION_NAMES = {
    UP: "Up", DOWN: "Down", LEFT: "Left", RIGHT: "Right", SELECT: "Select", BACK: "Back",
    HOME: "Home", LOG: "Start / stop log", TAG: "Timestamp", HOLD: "Hold (freeze the screen)", BUTTON5: "Button 5",
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
    "f9": LOG, "f10": TAG, "pause": HOLD,
}
BUTTON5_CHOICES = {"back": BACK, "soft": SOFT[4], "log": LOG, "tag": TAG, "hold": HOLD}


class KeyMap:
    def __init__(self, overrides: dict | None = None, button5: str = "back"):
        self.keys = {**DEFAULT_KEYS, **{str(k).lower(): v for k, v in (overrides or {}).items()}}
        self.button5 = BUTTON5_CHOICES.get(button5, BACK)

    def action(self, key_name: str) -> str | None:
        a = self.keys.get((key_name or "").lower())
        if a in ("", "none"):                   # a key set to do nothing (Control mapping)
            return None
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
MIN_GAP = 0.05              # volts: levels closer than this can't be told apart


class LadderButtons:
    """Several buttons on one analog input: each pulls the input to its own voltage, and nothing
    pressed reads above the top level. ``levels`` are the expected volts per button, in order;
    a reading is the button whose level is nearest, within 40% of the gap to its neighbours (a
    reading halfway between two is ambiguous, so it counts as nothing pressed).
    A new button must read the same twice in a row (debounce). One button at a time: a ladder
    can't tell two pressed together (the stacks are split so that buttons used together, like
    the d-pad and a soft key, are on different inputs)."""

    def __init__(self, levels: list[float], actions: list[str], read_volts=None):
        if len(levels) != len(actions):
            raise ValueError("one action per ladder level")
        self.levels = list(levels)
        self.actions = list(actions)
        self.read_volts = read_volts
        order = sorted(self.levels)
        gaps = [b - a for a, b in zip(order, order[1:])] or [1.0]
        self.tolerance = max(MIN_GAP, min(gaps)) * 0.4
        self.rest = max(self.levels) + self.tolerance       # above this: nothing pressed
        self.pressed: int | None = None
        self.volts: float | None = None                     # the last reading
        self._candidate: int | None = None

    def decode(self, volts: float | None) -> int | None:
        if volts is None:
            return None
        best = min(range(len(self.levels)), key=lambda i: abs(self.levels[i] - volts))
        return best if abs(self.levels[best] - volts) <= self.tolerance else None

    def update(self, volts: float | None) -> list[tuple[str, str]]:
        """Feed one reading; returns [("down" / "up", action)] transitions."""
        self.volts = volts
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


# ----------------------------------------------------------------------------
# The button ADC: every button on resistor stacks, and the battery
# ----------------------------------------------------------------------------
# A second ADS1115 at I2C 0x49 (System > Installed options > Buttons and battery ADC). Each stack
# is buttons from one input to ground, each through its own resistor, with a 10 kohm pull-up to
# 3.3 V: 0, 2.2 k, 5.6 k, 12 k and 27 kohm, the same on every stack, so one list of levels
# (device.button_levels). Buttons on different stacks can be held together; two on one can't.
BUTTON_STACKS = {1: (SOFT[0], SOFT[1], SOFT[2], SOFT[3], BUTTON5),    # A1: the top row, left to right
                 2: (UP, DOWN, LEFT, RIGHT, SELECT),                  # A2: the d-pad and the encoder's push
                 3: (BACK, HOLD, LOG, TAG, "")}                       # A3: the side buttons, top down; 5th spare
STACK_SIZE = 5              # positions per stack (a spare one is still decoded, so it can't pass for another)
BATTERY_CHANNEL = 0                                                   # A0: the pack, through its divider
STACK_NAMES = {1: "top row", 2: "d-pad and push", 3: "side buttons"}
# What a button can be given (Control mapping). "" = nothing: the button is ignored.
ASSIGNABLE = (SOFT[0], SOFT[1], SOFT[2], SOFT[3], BUTTON5, UP, DOWN, LEFT, RIGHT, SELECT, BACK, HOME, HOLD,
              LOG, TAG)
# The kernel's codes for each action (the gpio-key overlays' codes for the same buttons), for Doom:
# while it has the screen, the stacks reach it as a keyboard (``doom_controls.ButtonKeys``).
KEYCODES = {SOFT[0]: 59, SOFT[1]: 60, SOFT[2]: 61, SOFT[3]: 62, BUTTON5: 63, UP: 103, DOWN: 108, LEFT: 105,
            RIGHT: 106, SELECT: 28, BACK: 1, HOLD: 119, LOG: 67, TAG: 68}
SYS_IIO = "/sys/bus/iio/devices"
LOST_S = 2.0                # every read failing this long: the ADC has gone (``ButtonAdc.lost``)
# Flood guard: a stack that isn't wired (no pull-up, the input floating) or has a bad joint reads
# a wandering voltage that decodes as presses one after another. A stack doing that is ignored
# until it settles (``ButtonAdc.flooding``), so it can't drive the screens.
FLOOD_WINDOW = 2.0          # seconds looked at
FLOOD_CHANGES = 16          # this many changes in the window is more than a hand does on one stack
FLOOD_SOME = 6              # ... or this many with at least FLOOD_UNSURE of the readings between buttons
FLOOD_UNSURE = 0.2           # (a real press reads between buttons only for a sample or so at its edges)
FLOOD_QUIET = 3.0           # settled: no changes for this long, readings back on a level or at rest
LOG_KEPT = 60


def pos_key(ch: int, pos: int) -> str:
    """Settings key of a stack position: "2.3" is A2, button 3 (1-based)."""
    return f"{ch}.{pos}"


def stack_actions(button_map: dict | None = None) -> dict:
    """{channel: [action per position]}: ``BUTTON_STACKS`` with the Control mapping's changes
    (``device.button_map``, {"2.3": "left"}; "" ignores a button)."""
    m = button_map or {}
    out = {}
    for ch, acts in BUTTON_STACKS.items():
        acts = list(acts) + [""] * (STACK_SIZE - len(acts))
        out[ch] = [m.get(pos_key(ch, i + 1), a) for i, a in enumerate(acts)]
    return out


def stack_levels(levels, ch: int, tuning: dict | None = None) -> list[float]:
    """One stack's volts per position: the shared levels, with any taken for this stack's
    buttons on the Control mapping page (``device.button_tuning``, {"2.3": 1.21})."""
    levels = list(levels)[:STACK_SIZE]
    t = tuning or {}
    return [float(t.get(pos_key(ch, i + 1), v)) for i, v in enumerate(levels)]


class ButtonAdc:
    """The button ADC on its own thread (a conversion takes milliseconds, which the screen's loop
    shouldn't wait for): every stack read ``hz`` times a second, the battery every ``battery_s``.
    ``poll()`` (from the UI) returns the [("down" / "up", action)] since the last call;
    ``battery`` is the last battery input reading in ADC volts (None until there is one).

    A stack in ``muted`` (switched off on the Control mapping page) or ``flooding`` (see
    ``FLOOD_WINDOW``) is still read and decoded, so the page can show it, but sends nothing.
    ``log`` keeps the last presses as (time, channel, position 1-5, "down" / "up")."""

    def __init__(self, stacks: dict, battery=None, hz: float = 40.0, battery_s: float = 2.0,
                 clock=time.monotonic, start: bool = True, muted=()):
        self.stacks, self.read_battery = dict(stacks), battery
        self.period, self.battery_s, self.clock = 1.0 / hz, battery_s, clock
        self.muted = {int(c) for c in muted}
        self.flooding: set[int] = set()
        self.battery: float | None = None
        self.log: deque = deque(maxlen=LOG_KEPT)
        self._battery_at = -1e9
        self._ok_at = clock()
        self._sent: dict[int, str | None] = {ch: None for ch in self.stacks}    # action held down, per stack
        self._changes: dict[int, deque] = {ch: deque() for ch in self.stacks}
        self._samples: dict[int, deque] = {ch: deque() for ch in self.stacks}   # (time, between buttons)
        self._events: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="button-adc", daemon=True)
            self._thread.start()

    def step(self):
        """One pass over every stack (and the battery when it's due)."""
        now = self.clock()
        got, any_ok = [], False
        for ch, lad in self.stacks.items():
            v = lad.read_volts() if lad.read_volts else None
            any_ok |= v is not None
            before = lad.pressed
            lad.update(v)
            if lad.pressed != before:
                self._changes[ch].append(now)
                if before is not None:
                    self.log.append((now, ch, before + 1, "up"))
                if lad.pressed is not None:
                    self.log.append((now, ch, lad.pressed + 1, "down"))
            self._samples[ch].append((now, v is not None and v < lad.rest and lad.decode(v) is None))
            self._guard(ch, now)
            want = None
            if ch not in self.muted and ch not in self.flooding and lad.pressed is not None:
                want = lad.actions[lad.pressed] or None
            if want != self._sent[ch]:
                if self._sent[ch]:
                    got.append(("up", self._sent[ch]))
                if want:
                    got.append(("down", want))
                self._sent[ch] = want
        if self.read_battery is not None and now - self._battery_at >= self.battery_s:
            self._battery_at = now
            v = self.read_battery()
            any_ok |= v is not None
            if v is not None:
                self.battery = v
        if any_ok:
            self._ok_at = now
        if got:
            with self._lock:
                self._events += got

    def _guard(self, ch: int, now: float):
        changes, samples = self._changes[ch], self._samples[ch]
        while changes and now - changes[0] > max(FLOOD_WINDOW, FLOOD_QUIET):
            changes.popleft()
        while samples and now - samples[0][0] > FLOOD_WINDOW:
            samples.popleft()
        recent = sum(1 for t in changes if now - t <= FLOOD_WINDOW)
        unsure = sum(1 for _, u in samples if u) / max(1, len(samples))
        if ch not in self.flooding:
            if recent >= FLOOD_CHANGES or (recent >= FLOOD_SOME and unsure >= FLOOD_UNSURE):
                self.flooding.add(ch)
        elif (not changes or now - changes[-1] >= FLOOD_QUIET) and unsure < 0.1:
            self.flooding.discard(ch)

    def presses(self, seconds: float = 5.0) -> int:
        """Presses ("down"s) on every stack in the last ``seconds``: a flood shows as a big number."""
        now = self.clock()
        return sum(1 for t, _c, _p, kind in list(self.log) if kind == "down" and now - t <= seconds)

    @property
    def lost(self) -> bool:
        """Nothing has read for ``LOST_S``: the ADC or its wiring has gone."""
        return self.clock() - self._ok_at >= LOST_S

    def _run(self):
        while not self._stop.wait(self.period):
            self.step()

    def poll(self) -> list[tuple[str, str]]:
        with self._lock:
            out, self._events = self._events, []
        return out

    def stop(self):
        self._stop.set()


def button_adc(levels, sys_iio: str = SYS_IIO, stacks: dict | None = None, tuning: dict | None = None,
               muted=(), **kw) -> "ButtonAdc | None":
    """The stacks and the battery on the buttons' ADS1115 (0x49), reading on their own thread, or
    None when that ADC isn't there (yet: the app tries again). ``stacks``: {channel: [action per
    position]} (``stack_actions``); ``tuning``: levels taken per position (``stack_levels``)."""
    from ..core import hardware
    folder = hardware.find_iio(hardware.ADC_NAMES, hardware.BUTTON_ADC, sys_iio)
    if folder is None:
        return None
    ladders = {}
    for ch, acts in (stack_actions() if stacks is None else stacks).items():
        raw = os.path.join(folder, f"in_voltage{ch}_raw")
        lv = stack_levels(levels, ch, tuning)
        n = min(len(acts), len(lv))
        if os.path.exists(raw) and n:
            ladders[ch] = LadderButtons(lv[:n], list(acts)[:n], iio_volts_reader(raw))
    bat = os.path.join(folder, f"in_voltage{BATTERY_CHANNEL}_raw")
    return ButtonAdc(ladders, iio_volts_reader(bat) if os.path.exists(bat) else None, muted=muted, **kw)


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
