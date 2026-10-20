"""Getting logs and reports off the device: USB drives and CSV export."""

import csv
import datetime
import os
import shutil
from dataclasses import dataclass

from ..can.frame import CanFrame
from ..j1939.addresses import sa_name
from ..j1939.constants import pgn_label
from ..j1939.ids import parse_id
from . import config

# Where desktop automounters put removable media (udisks2 on Pi OS / Ubuntu / Mint).
REMOVABLE_ROOTS = ("/media/", "/run/media/", "/mnt/")


@dataclass
class Drive:
    label: str
    path: str
    free_bytes: int | None = None

    def describe(self) -> str:
        free = f", {self.free_bytes / 1e9:.1f} GB free" if self.free_bytes is not None else ""
        return f"{self.label} ({self.path}{free})"


def removable_drives(mounts_file: str = "/proc/mounts") -> list[Drive]:
    """Mounted USB sticks / SD cards: block devices mounted under a removable-media root."""
    out = []
    try:
        with open(mounts_file) as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/"):
            continue
        mount = parts[1].replace("\\040", " ")
        if not mount.startswith(REMOVABLE_ROOTS):
            continue
        try:
            free = shutil.disk_usage(mount).free
        except OSError:
            free = None
        out.append(Drive(os.path.basename(mount.rstrip("/")) or parts[0], mount, free))
    return out


def export_to(dest_root: str, sources: list[str] | None = None) -> tuple[str, int]:
    """Copy the logs and reports folders into a new dated folder on ``dest_root``.

    Returns (folder created, number of files copied).
    """
    if sources is None:
        sources = [os.path.join(config.data_dir(), d) for d in ("logs", "reports")]
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(dest_root, f"canopener_export_{stamp}")
    n = 0
    for src in sources:
        if not os.path.isdir(src):
            continue
        target = os.path.join(dest, os.path.basename(src.rstrip("/")))
        for dirpath, _, files in os.walk(src):
            rel = os.path.relpath(dirpath, src)
            os.makedirs(os.path.join(target, rel), exist_ok=True)
            for name in files:
                shutil.copy2(os.path.join(dirpath, name), os.path.join(target, rel, name))
                n += 1
    if n and hasattr(os, "sync"):
        os.sync()                   # flush to the stick so it's safe to pull
    return dest, n


CSV_HEADER = ["timestamp", "time", "dir", "can_id", "priority", "pgn", "pgn_name", "sa", "sa_name",
              "da", "dlc", "data"]


def write_csv(path: str, frames: list[CanFrame]) -> int:
    """Frames as a spreadsheet-friendly CSV with the J1939 fields broken out."""
    n = 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for fr in frames:
            if fr.error:
                continue
            wall = datetime.datetime.fromtimestamp(fr.timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] \
                if fr.timestamp > 1e9 else ""
            row = [f"{fr.timestamp:.6f}", wall, "TX" if fr.tx else "RX", fr.id_str()]
            if fr.extended:
                j = parse_id(fr.can_id)
                row += [j.priority, f"{j.pgn:05X}", pgn_label(j.pgn), f"{j.sa:02X}", sa_name(j.sa, with_hex=False),
                        "" if j.da == 0xFF else f"{j.da:02X}"]
            else:
                row += ["", "", "", "", "", ""]
            row += [fr.dlc, fr.data.hex(" ").upper()]
            w.writerow(row)
            n += 1
    return n
