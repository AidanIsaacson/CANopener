"""
Settings persistence and session management.
Saves/loads JSON config from ~/.can_opener/settings.json
"""

import json
import os
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict

CONFIG_DIR  = Path.home() / ".can_opener"
CONFIG_FILE = CONFIG_DIR / "settings.json"
LOG_BASE    = Path.home() / "can_logs"

DEFAULTS = {
    "interface":  "can0",
    "bitrate":    "250000",
    "protocol":   "J1939",
    "saved_configs": [],
    "log_path":   str(LOG_BASE),
    "font_size":  11,
    "last_connected": False,
}


def load_settings() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            # Merge missing keys from defaults
            for k, v in DEFAULTS.items():
                if k not in data:
                    data[k] = v
            return data
        except Exception:
            pass
    return dict(DEFAULTS)


def save_settings(settings: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(settings, f, indent=2)


def get_session_dir(log_base: Optional[str] = None) -> Path:
    """
    Return the session log directory, creating it if needed.
    Session is identified by boot time — one folder per power cycle.
    """
    base = Path(log_base) if log_base else LOG_BASE
    boot_ts = _get_boot_time()
    folder  = base / f"session_{boot_ts}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _get_boot_time() -> str:
    """Return boot timestamp string YYYY-MM-DD_HH-MM."""
    try:
        with open("/proc/uptime") as f:
            uptime_secs = float(f.read().split()[0])
        boot_epoch = time.time() - uptime_secs
        dt = datetime.fromtimestamp(boot_epoch)
        return dt.strftime("%Y-%m-%d_%H-%M")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d_%H-%M")


def new_log_filename() -> str:
    """Generate a timestamped log filename: MM-DD-YY_HH:MM:SS.csv"""
    return datetime.now().strftime("%m-%d-%y_%H-%M-%S") + ".csv"


def list_usb_drives() -> List[Dict]:
    """
    Return list of mounted USB drives as [{"label": str, "path": str}].
    Looks at /proc/mounts for /dev/sd* or /dev/sda* mounted paths.
    """
    drives = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                dev, mount = parts[0], parts[1]
                if dev.startswith("/dev/sd") and mount not in ("/", "/boot", "/home"):
                    label = Path(mount).name or dev
                    drives.append({"label": label, "path": mount})
    except Exception:
        pass
    return drives
