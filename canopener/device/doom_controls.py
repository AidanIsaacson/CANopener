"""Controls from CAN for Doom: the machine's signals become key presses and mouse movement.

Doom (``device/doom.py``) reads the keyboard and mouse itself and knows nothing of the games'
commands, so while it runs this makes two input devices of its own through the kernel's
``/dev/uinput`` (a keyboard and a mouse; Doom sees them like any others) and drives them from
the profile's bindings (``game_controls``):

* **Turn the piece** (a delta binding, e.g. the steering angle) turns you: mouse movement,
  ``games.doom_turn`` counts per binding step (negative reverses it).
* **Walk forward / Walk back / Fire / Use** hold their key down for as long as the signal is past
  its threshold. The keys are the handheld's own (d-pad up / down, encoder push, soft key 1), so
  Doom's key setup needs nothing extra.

A signal that stops arriving (``STALE_S``) lets its key go, and every key is released when Doom
ends. Reading only: nothing is sent on the bus. Needs write access to ``/dev/uinput``
(``deploy/raspberry-pi/72-canopener-uinput.rules``, installed by ``install.sh --device``).
Standard library only, no Pygame; Linux only.
"""

import os
import struct
import threading
import time

from . import game_controls as gc
from .input import EVENT

DEVICE = "/dev/uinput"
STALE_S = 1.0               # a signal not heard for this long counts as let go
POLL_S = 1 / 60
TURN_SETTING = "games.doom_turn"
TURN_COUNTS = 40            # mouse counts per step of the turn binding, unless the setting says otherwise

# <linux/input-event-codes.h>
EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
SYN_REPORT, REL_X, REL_Y = 0, 0, 1
KEY_ENTER, KEY_F1, KEY_UP, KEY_DOWN = 28, 59, 103, 108
BTN_LEFT, BTN_RIGHT = 0x110, 0x111
# <linux/uinput.h>
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 0x40045564, 0x40045565, 0x40045566
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
USER_DEV = struct.Struct("80sHHHHI" + "i" * 256)        # struct uinput_user_dev
BUS_VIRTUAL = 0x06

# What each hold action presses: the same keys the handheld's buttons send (doom.MAIN_CONFIG).
KEYS = {gc.FORWARD: KEY_UP, gc.BACK: KEY_DOWN, gc.FIRE: KEY_ENTER, gc.USE: KEY_F1}


def available() -> bool:
    """This user may make input devices (only looks: nothing is opened)."""
    return os.access(DEVICE, os.W_OK)


def wanted(bindings) -> bool:
    """Any of the profile's bindings is one Doom can use."""
    return any(b.do in KEYS or (b.do == gc.TURN and b.mode != "position") for b in bindings)


class VirtualDevice:
    """One uinput device. ``keys`` / ``rels``: the codes it can send."""

    def __init__(self, name: str, keys=(), rels=(), path: str = DEVICE, ioctl=None):
        if ioctl is None:
            import fcntl
            ioctl = fcntl.ioctl
        self._fd = os.open(path, os.O_WRONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            ioctl(self._fd, UI_SET_EVBIT, EV_SYN)
            if keys:
                ioctl(self._fd, UI_SET_EVBIT, EV_KEY)
                for code in keys:
                    ioctl(self._fd, UI_SET_KEYBIT, code)
            if rels:
                ioctl(self._fd, UI_SET_EVBIT, EV_REL)
                for code in rels:
                    ioctl(self._fd, UI_SET_RELBIT, code)
            os.write(self._fd, USER_DEV.pack(name.encode()[:79], BUS_VIRTUAL, 0x1209, 0xCA40, 1, 0, *([0] * 256)))
            ioctl(self._fd, UI_DEV_CREATE)
        except OSError:
            os.close(self._fd)
            raise
        self._ioctl = ioctl

    def emit(self, kind: int, code: int, value: int):
        """One event, then the report that ends it."""
        os.write(self._fd, EVENT.pack(0, 0, kind, code, value) + EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0))

    def close(self):
        if self._fd is None:
            return
        try:
            self._ioctl(self._fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None


class UInputOutput:
    """The keyboard and the mouse Doom sees. Two devices, each looking like the real thing (a
    keyboard with all the ordinary keys, a mouse with two axes and buttons), because that is how
    input stacks decide what a device is."""

    def __init__(self):
        self.keyboard = VirtualDevice("CANopener game controls (keys)", keys=range(1, 128))
        try:
            self.mouse = VirtualDevice("CANopener game controls (mouse)", keys=(BTN_LEFT, BTN_RIGHT),
                                       rels=(REL_X, REL_Y))
        except OSError:
            self.keyboard.close()
            raise

    def key(self, code: int, down: bool):
        self.keyboard.emit(EV_KEY, code, 1 if down else 0)

    def turn(self, counts: int):
        self.mouse.emit(EV_REL, REL_X, counts)

    def close(self):
        self.keyboard.close()
        self.mouse.close()


class DoomControls:
    """Reads the bound signals and drives ``out`` (``key(code, down)``, ``turn(counts)``,
    ``close()``). ``tick()`` is one look; ``start()`` / ``stop()`` run it on a thread of its own
    while Doom has the screen (the UI thread is waiting for Doom to end)."""

    def __init__(self, controls: gc.SignalControls, session, out, turn_counts: float = TURN_COUNTS,
                 clock=time.time):
        self.controls, self.session, self.out = controls, session, out
        self.turn_counts = turn_counts or TURN_COUNTS
        self.clock = clock
        self.down: set[int] = set()
        self._last: dict[int, float] = {}       # turn bindings: the value last time
        self._acc = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def for_profile(cls, settings, profile_id: str, session, out=None) -> "DoomControls | None":
        """The bridge for this profile's bindings, or None when there's nothing for it to do
        (no session, no binding Doom can use). Raises OSError if the input devices can't be made."""
        controls = gc.SignalControls.for_profile(settings, profile_id, 1)
        if session is None or not wanted(controls.bindings):
            return None
        try:
            counts = float(settings.get(TURN_SETTING) or TURN_COUNTS)
        except (TypeError, ValueError):
            counts = TURN_COUNTS
        return cls(controls, session, out if out is not None else UInputOutput(), counts)

    def tick(self):
        now = self.clock()
        want: set[int] = set()
        for i, b in enumerate(self.controls.bindings):
            got = self.controls.sample(self.session, b)
            fresh = got is not None and now - got[1] <= STALE_S
            if b.do in KEYS:
                if fresh and b.held(got[0]):
                    want.add(KEYS[b.do])
            elif b.do == gc.TURN and b.mode != "position":
                if not fresh:
                    self._last.pop(i, None)     # start again from wherever it is when it's back
                    continue
                v, last = got[0], self._last.get(i)
                self._last[i] = v
                if last is None:
                    continue
                if b.mode == "delta":
                    self._acc += (v - last) / b.every * self.turn_counts
                elif not b.held(last) and b.held(v):        # a switch: one step a press
                    self._acc += self.turn_counts
        for code in sorted(self.down - want):
            self.out.key(code, False)
        for code in sorted(want - self.down):
            self.out.key(code, True)
        self.down = want
        counts = int(self._acc)
        if counts:
            self._acc -= counts
            self.out.turn(counts)

    def release(self):
        for code in sorted(self.down):
            self.out.key(code, False)
        self.down = set()

    # --- on its own thread, while Doom runs ---
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="doom-controls", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(POLL_S):
            try:
                self.tick()
            except OSError:                     # the device went away: nothing more to do
                return

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.release()
        except OSError:
            pass
        self.out.close()
