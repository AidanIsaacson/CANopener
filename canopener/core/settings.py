"""Settings every front-end can share, in ``~/.config/canopener/settings.json``.

The desktop GUI still keeps its own values in Qt's ``QSettings`` (window geometry and the like
belong there); this store is for things that aren't tied to one toolkit: the CAN connection,
units, the active profile, sound cues, the handheld's controls and screen, and which measurement
sources (meters) are switched on. The on-device Pygame UI reads and writes only this file.

Keys are flat, dotted strings (``"can.bitrate"``). ``get`` falls back to ``DEFAULTS``, so a key
added later needs no migration. Every ``set`` writes the file straight away (atomically), so a
handheld that loses power keeps its settings.
"""

import json
import os
import threading

from . import config

FILE_NAME = "settings.json"

DEFAULTS: dict = {
    # shared by every front-end
    "ui": "",                           # "device": `canopener` starts the handheld UI (canopener.frontends)
    "units": "metric",
    "units.custom": {},
    "profile": "generic",
    "data_dir": "",
    "can.source": "socketcan",          # core.connection kinds: socketcan / sim / obd-sim / profile-sim
    "can.iface": "can0",
    "can.bitrate": 250000,              # 0 = auto (the signal finder picks it)
    "can.listen_only": False,
    "can.connect_on_start": False,
    "show_simulators": False,           # list the simulators as sources (off: a shorter, adapter-only list)
    "sound.speaker": True,
    "sound.buzzer": "auto",
    "sound.link_cues": True,            # chime when traffic drops out / comes back
    "sound.volume": 80,                 # speaker volume, 0-100 (core.sound.amplitude)
    "sound.touch": False,               # chirp on every tap / button press (the handheld)
    "sound.tones": {},                  # event -> tone name or "off" (core.sound.EVENTS / TONES)
    "log.plan": [],
    # measurement sources (core.meters)
    "meters.sim": False,                # a simulated meter, for trying the logger without hardware
    "meters.serial": "",                # serial port of a JSON-lines meter (Pico / ESP32 bridge)
    "meters.iio": "",                   # IIO ADC device name (e.g. ads1115) read as probe voltages
    "meters.iio_ratio": 1.0,            # input divider: volts at the probe per volt at the ADC
    "meters.cal": {},                   # channel id -> {offset, gain, ref, when} (core.calibration)
    "meters.cal_ref": 12.0,             # the supply voltage last typed in for a Quick calibration
    # installed options (core.hardware): which on-deck parts this unit has fitted
    "hardware": {},                     # option id -> "yes" / "no" / "auto"; missing = "no"
    "hardware.meter_pico.port": "",     # serial port, if not found by its USB name
    "hardware.logic_pico.port": "",
    "hardware.esp32.port": "",          # "" = /dev/ttyAMA0 (the GPIO14/15 UART)
    # the handheld (canopener.device)
    "device.window": "800x480",         # window size when not full screen (trying it on a laptop)
    "device.diagonal_in": 7.0,          # screen size: sizes touch targets in millimetres
    "device.rotate": 0,                 # 0 / 90 / 180 / 270: rotate the picture (and touch) in software
    "device.softkeys_edge": "top",      # where the hardware buttons are: their labels sit on that edge
    "device.buttons": 5,                # hardware buttons along that edge (4 or 5)
    "device.button5": "back",           # what a 5th button does: back / soft / log / tag
    "device.long_press_s": 0.7,         # holding Select this long = Back
    "device.fps": 30,
    "device.theme": "element-zero",     # the handheld's look: core/themes.py (built-in or a themes/*.json)
    "device.accent": "",                # "#rrggbb" in place of the theme's accent colour ("" = the theme's)
    "device.keys": {},                  # key name -> action, on top of device.input.DEFAULT_KEYS
    "device.encoder": "auto",           # evdev device for the rotary encoder: auto / none / /dev/input/eventN
    "device.ladder": "",                # IIO channel of a resistor-ladder button stack ("" = none)
    "device.ladder_levels": [0.0, 0.6, 1.2, 1.8, 2.4],     # volts per ladder button (soft keys 1-5)
    "device.games": False,              # Games on the home screen (System > About, seven times)
    "games.harness_best": 0,
    "games.cargo_best": 0,
    "games.controls": {},               # profile id -> signal bindings (device/game_controls.py)
    "plugins.sources": [],              # GitHub repos to install plugins from (core.plugin_repos)
    "github.client_id": "",             # GitHub App for sign-in ("" = core.github.DEFAULT_CLIENT_ID)
}


def path() -> str:
    return os.path.join(config.config_dir(), FILE_NAME)


class Settings:
    """A small JSON key/value store. Thread-safe; ``get`` never raises for a missing key."""

    def __init__(self, file: str | None = None):
        self.file = file or path()
        self._lock = threading.RLock()
        self._values: dict = {}
        self._listeners: list = []
        self.load()

    def load(self):
        try:
            with open(self.file) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        with self._lock:
            self._values = data if isinstance(data, dict) else {}

    def get(self, key: str, default=None):
        with self._lock:
            if key in self._values:
                return self._values[key]
        return DEFAULTS.get(key) if default is None else default

    def __getitem__(self, key: str):
        return self.get(key)

    def set(self, key: str, value):
        with self._lock:
            if self._values.get(key, object()) == value:
                return
            self._values[key] = value
            self.save()
        for fn in list(self._listeners):
            fn(key, value)

    def update(self, values: dict):
        with self._lock:
            self._values.update(values)
            self.save()
        for key, value in values.items():
            for fn in list(self._listeners):
                fn(key, value)

    def reset(self, key: str):
        with self._lock:
            if self._values.pop(key, None) is not None:
                self.save()

    def save(self):
        with self._lock:
            config.ensure_dir(os.path.dirname(self.file))
            tmp = self.file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._values, f, indent=2, sort_keys=True)
            os.replace(tmp, self.file)

    def on_change(self, fn):
        """Call ``fn(key, value)`` after each change (on the caller's thread)."""
        self._listeners.append(fn)

    def as_dict(self) -> dict:
        with self._lock:
            return {**DEFAULTS, **self._values}
