"""User file locations (XDG-style, so they also work on a Raspberry Pi)."""

import os

_data_dir_override: str | None = None


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "canopener")


def data_dir() -> str:
    """Home for saved captures, logs and reports (``~/canopener`` unless the user picked another)."""
    return _data_dir_override or os.path.expanduser("~/canopener")


def set_data_dir(path: str | None):
    """Front-ends call this with the user's chosen save folder (None = back to the default)."""
    global _data_dir_override
    _data_dir_override = os.path.expanduser(path) if path else None


def logs_dir() -> str:
    return os.path.join(data_dir(), "logs")


def reports_dir() -> str:
    return os.path.join(data_dir(), "reports")


def spn_csv_path() -> str:
    return os.path.join(config_dir(), "spn_names.csv")


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path
