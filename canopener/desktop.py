"""Install CANopener as a clickable desktop application (Linux Mint, Ubuntu, Pi OS ...).

    canopener --install-desktop                     # application menu entry
    canopener --install-desktop --desktop-shortcut  # ... plus an icon on the desktop
    canopener --uninstall-desktop

The launcher runs ``<this python> -m canopener``, so it keeps working from a venv without
activating it. Re-run after moving the venv.
"""

import os
import shutil
import subprocess
import sys

APP_ID = "canopener"
ICON_SRC = os.path.join(os.path.dirname(__file__), "assets", "canopener.svg")


def _data_home() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def _desktop_dir() -> str:
    try:
        r = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.expanduser("~/Desktop")


def paths() -> dict[str, str]:
    data = _data_home()
    return {
        "menu": os.path.join(data, "applications", f"{APP_ID}.desktop"),
        "icon": os.path.join(data, "icons", "hicolor", "scalable", "apps", f"{APP_ID}.svg"),
        "shortcut": os.path.join(_desktop_dir(), f"{APP_ID}.desktop"),
    }


def entry_text(icon_path: str, python: str | None = None) -> str:
    python = python or sys.executable
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=CANopener",
        "GenericName=CAN / J1939 diagnostics",
        "Comment=Read fault codes, live data and record CAN logs",
        f'Exec="{python}" -m canopener',
        f"Icon={icon_path}",
        "Terminal=false",
        "Categories=Utility;Development;",
        "Keywords=CAN;J1939;diagnostics;fault;codes;",
        f"StartupWMClass={APP_ID}",
        "",
    ])


def install(desktop_shortcut: bool = False) -> list[str]:
    p = paths()
    os.makedirs(os.path.dirname(p["icon"]), exist_ok=True)
    shutil.copyfile(ICON_SRC, p["icon"])
    text = entry_text(p["icon"])
    os.makedirs(os.path.dirname(p["menu"]), exist_ok=True)
    with open(p["menu"], "w") as f:
        f.write(text)
    os.chmod(p["menu"], 0o755)
    done = [p["menu"], p["icon"]]
    if desktop_shortcut:
        os.makedirs(os.path.dirname(p["shortcut"]), exist_ok=True)
        with open(p["shortcut"], "w") as f:
            f.write(text)
        os.chmod(p["shortcut"], 0o755)
        # Cinnamon/GNOME only launch desktop files marked trusted; otherwise it asks first.
        if shutil.which("gio"):
            subprocess.run(["gio", "set", p["shortcut"], "metadata::trusted", "true"], capture_output=True)
        done.append(p["shortcut"])
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", os.path.dirname(p["menu"])], capture_output=True)
    return done


def uninstall() -> list[str]:
    removed = []
    for path in paths().values():
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return removed
