"""Doom on the handheld (Games): finding it, installing it, and starting it.

Nothing of Doom is in this repository and nothing is installed with CANopener. The game is the
real thing: the Chocolate Doom engine (GPL-2.0-or-later) with the Freedoom levels (BSD), both from
the system's package archive. Turning the games menu on asks once whether to download them; the
Doom row under Games asks again later. Your own ``doom.wad`` / ``doom2.wad`` dropped in
``<config>/doom/`` is used in preference to Freedoom.

The handheld's controls are keys (``device/input.py``), so Doom is given a key setup of its own
(``write_config``): d-pad to move and turn, encoder push to fire, the soft keys for use / weapon /
map / strafe, the fifth button for the menu. Installing goes through
``core.interfaces.run_privileged`` as fixed argv lists (``install_steps``; the same lines are in
``deploy/raspberry-pi/canopener.sudoers``).

Standard library only, no Pygame: the screen hand-over is ``DeviceApp.run_external``.
"""

import os
import shutil

from ..core import config, interfaces

ENGINES = ("chocolate-doom", "crispy-doom")
ENGINE_DIRS = ("/usr/games", "/usr/local/games")           # not always on a service's PATH
# Game data, best first: your own copy, then Freedoom, then the shareware episode.
WADS = ("doom2.wad", "doom.wad", "freedoom1.wad", "freedoom2.wad", "doom1.wad")
WAD_NAMES = {"doom2.wad": "Doom II", "doom.wad": "Doom", "freedoom1.wad": "Freedoom", "freedoom2.wad": "Freedoom 2",
             "doom1.wad": "Doom (shareware)"}
SYSTEM_WAD_DIRS = ("/usr/share/games/doom", "/usr/local/share/games/doom", "/usr/share/doom")
PACKAGES = ("chocolate-doom", "freedoom")
INSTALL_TIMEOUT_S = 900

# The key setup, in the scancodes Doom's config files use. Arrows: the d-pad. 28: Enter (the
# encoder's push). 59-63: F1-F5 (the four soft keys and the fifth button).
MAIN_CONFIG = {
    "key_up": 72, "key_down": 80, "key_left": 75, "key_right": 77,
    "key_fire": 28, "key_use": 59, "key_strafe": 62,
    "joyb_speed": 29,               # always run
    "use_mouse": 1, "mouse_sensitivity": 5,     # the encoder, where it shows up as a mouse axis: turning
}
EXTRA_CONFIG = {
    "fullscreen": 1, "show_endoom": 0, "novert": 1, "grabmouse": 1,
    "key_nextweapon": 60, "key_map_toggle": 61,
    "key_menu_activate": 63, "key_menu_abort": 63, "key_menu_confirm": 28,
    # Doom's own F1-F5 shortcuts would fight the soft keys: off (they're all in the menu)
    "key_menu_help": 0, "key_menu_save": 0, "key_menu_load": 0, "key_menu_volume": 0, "key_menu_detail": 0,
}
CONTROLS = [
    "D-pad: walk and turn. Encoder push: fire.",
    "Soft keys: 1 use / open, 2 next weapon, 3 map, 4 hold to strafe.",
    "Fifth button: Doom's menu. Quit Game there (push to confirm) comes back to CANopener.",
]


def data_dir() -> str:
    """Where Doom's settings and saved games live, and where your own WAD goes."""
    return os.path.join(config.config_dir(), "doom")


def engine() -> str | None:
    """The engine's path, if one is installed."""
    for name in ENGINES:
        found = shutil.which(name) or next(
            (p for p in (os.path.join(d, name) for d in ENGINE_DIRS) if os.access(p, os.X_OK)), None)
        if found:
            return found
    return None


def wad() -> str | None:
    """The game data to play: the best one found (yours first, then the system's)."""
    for name in WADS:
        for d in (data_dir(), *SYSTEM_WAD_DIRS):
            for candidate in (name, name.upper()):
                path = os.path.join(d, candidate)
                if os.path.isfile(path):
                    return path
    return None


def installed() -> bool:
    return engine() is not None and wad() is not None


def wad_name(path: str | None = None) -> str:
    path = path or wad()
    return WAD_NAMES.get(os.path.basename(path).lower(), os.path.basename(path)) if path else ""


def can_install() -> bool:
    """The packages can be fetched here (a Debian-family system, like Raspberry Pi OS)."""
    return shutil.which("apt-get") is not None


def install_steps() -> list[list[str]]:
    """What installing runs as root: fixed commands, nothing from the user in them."""
    return [["apt-get", "update"], ["apt-get", "install", "-y", "--no-install-recommends", *PACKAGES]]


def install(runner=None) -> tuple[bool, str]:
    """Download and install the engine and Freedoom. Returns (ok, message). Slow: run it off the
    UI thread."""
    if not can_install():
        return False, "This system has no apt-get: install chocolate-doom and a WAD by hand."
    runner = runner or interfaces.run_privileged
    ok, msg = runner(install_steps(), timeout=INSTALL_TIMEOUT_S, optional={0})   # stale lists may still do
    if ok and not installed():
        return False, "The packages installed, but Doom still wasn't found."
    return ok, msg


def write_config(directory: str | None = None) -> tuple[str, str]:
    """Make sure Doom's two config files exist with the handheld's keys; returns their paths.
    Existing files are left alone, so changes made in Doom's own menus (or by hand) stay."""
    directory = directory or data_dir()
    os.makedirs(directory, exist_ok=True)
    paths = []
    for name, values in (("default.cfg", MAIN_CONFIG), ("chocolate-doom.cfg", EXTRA_CONFIG)):
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.writelines(f"{key:<30}{value}\n" for key, value in values.items())
        paths.append(path)
    return paths[0], paths[1]


def command() -> list[str] | None:
    """The argv that starts Doom with the handheld's setup (None: not installed)."""
    exe, data = engine(), wad()
    if exe is None or data is None:
        return None
    main, extra = write_config()
    return [exe, "-iwad", data, "-config", main, "-extraconfig", extra, "-savedir", data_dir() + os.sep]
