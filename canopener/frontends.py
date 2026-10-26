"""Switching between the two graphical front-ends: the desktop app (Qt) and the handheld UI
(Pygame).

The choice is the shared setting ``ui`` (``core.settings``): ``"device"`` makes the ``canopener``
command (and its menu entry, and the Pi's launcher) start the handheld UI instead of the
desktop window. Both apps can flip it: the desktop app under Settings > Handheld (Pi) screen,
the handheld under System > Desktop app instead.

A switch happens after the current app has closed cleanly, by starting the other one as a
**fresh process** (``switch``), the same way "Restart now" after an update does: nothing the
old toolkit left behind in the process (SDL's or Qt's open handles) can trip up the new one.
Under the Pi's systemd service the app exits with the restart code instead, and the service's
launcher starts whichever UI the setting now names. The new process's output goes to
``log_path()`` so a UI that fails to open leaves a reason behind.
"""

import datetime
import importlib.util
import os
import subprocess
import sys

from .core import config

DESKTOP, DEVICE = "desktop", "device"


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
    if ui == DEVICE:
        argv = [sys.executable, "-m", "canopener.device.app"]
    else:   # --desktop: don't bounce straight back to the handheld if the setting says otherwise
        argv = [sys.executable, "-m", "canopener", "--desktop"]
    if fullscreen:
        argv.append("--fullscreen")
    if sim:
        argv.append("--sim")
    return argv


def log_path() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "canopener", "canopener.log")


def switch(ui: str, fullscreen: bool = False, popen=subprocess.Popen) -> int:
    """Start ``ui`` once this app has closed. Returns the exit code this app should end with:
    the service's restart code under systemd, else 0 (the new UI is already starting)."""
    from .core import updates
    if updates.under_systemd():
        return updates.RESTART_EXIT_CODE
    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    argv = argv_for(ui, fullscreen)
    with open(path, "a") as log:
        log.write(f"=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} switching to the {ui} UI: {' '.join(argv)}\n")
        log.flush()
        popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
              close_fds=True, cwd=config.install_dir())    # not the home folder: see install_dir()
    return 0


def exec_ui(ui: str, fullscreen: bool = False, sim: bool = False):
    """Replace this process with ``ui`` at startup, before any window exists (``canopener``
    following the setting). Doesn't return."""
    argv = argv_for(ui, fullscreen, sim)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        if not os.isatty(2):                # started from a menu: keep a crash's reason in the log
            path = log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.write(fd, f"=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} starting the {ui} UI\n".encode())
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.close(fd)
    except OSError:
        pass
    os.chdir(config.install_dir())          # not the home folder: see core.config.install_dir()
    os.execv(argv[0], argv)
