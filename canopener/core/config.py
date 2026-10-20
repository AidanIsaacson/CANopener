"""User file locations (XDG-style, so they also work on a Raspberry Pi)."""

import os


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "canopener")


def data_dir() -> str:
    """Default home for saved reports and logs."""
    return os.path.expanduser("~/canopener")


def spn_csv_path() -> str:
    return os.path.join(config_dir(), "spn_names.csv")


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path
