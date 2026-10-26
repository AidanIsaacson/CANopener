"""Switching between the two graphical front-ends: the desktop app (Qt) and the handheld UI
(Pygame).

The choice is the shared setting ``ui`` (``core.settings``): ``"device"`` makes the ``canopener``
command (and its menu entry, and the Pi's launcher) start the handheld UI instead of the
desktop window. Both apps can flip it: the desktop app under Settings > Handheld (Pi) screen,
the handheld under System > Desktop app instead. Switching replaces the running process
(``os.execv``) once the current app has closed cleanly, so under the Pi's systemd service the
new UI keeps the same process and the service doesn't see an exit.
"""

import importlib.util
import os
import sys

DESKTOP, DEVICE = "desktop", "device"
MODULES = {DESKTOP: "canopener.gui.app", DEVICE: "canopener.device.app"}


def available(ui: str) -> tuple[bool, str]:
    """Can this front-end start here? (False, what to install) if its toolkit is missing."""
    if ui == DEVICE:
        if importlib.util.find_spec("pygame") is None:
            return False, 'The handheld UI needs Pygame: pip install -e ".[device]" (or pip install pygame-ce).'
        return True, ""
    if importlib.util.find_spec("PySide6") is None and importlib.util.find_spec("PyQt6") is None:
        return False, 'The desktop app needs PySide6 (pip install -e ".[gui]") or PyQt6 (apt install python3-pyqt6).'
    return True, ""


def argv_for(ui: str, fullscreen: bool = False, sim: bool = False) -> list[str]:
    argv = [sys.executable, "-m", MODULES[ui]]
    if ui == DESKTOP:
        argv.append("--desktop")            # don't bounce straight back if the setting says otherwise
    if fullscreen:
        argv.append("--fullscreen")
    if sim:
        argv.append("--sim")
    return argv


def exec_ui(ui: str, fullscreen: bool = False, sim: bool = False):
    """Replace this process with the other front-end. Doesn't return."""
    argv = argv_for(ui, fullscreen, sim)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(argv[0], argv)
