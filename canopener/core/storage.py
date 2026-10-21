"""Getting logs and reports off the device: USB drives and CSV export."""

import csv
import datetime
import os
import shutil
import subprocess
from dataclasses import dataclass

from ..can.frame import CanFrame
from ..j1939.addresses import sa_name
from ..j1939.constants import pgn_label
from ..j1939.ids import parse_id

# Where desktop automounters put removable media (udisks2 on Pi OS / Ubuntu / Mint). Anything
# under /mnt is only offered if the kernel says it's a USB or removable device.
AUTOMOUNT_ROOTS = ("/media/", "/run/media/")
MANUAL_ROOTS = ("/mnt/",)


def is_removable(device: str, sys_block: str = "/sys/class/block") -> bool:
    """True for USB sticks / card readers: a removable disk or anything on the USB bus."""
    name = os.path.basename(device)
    try:
        real = os.path.realpath(os.path.join(sys_block, name))
    except OSError:
        return False
    if "/usb" in real:
        return True
    disk = os.path.dirname(real) if os.path.exists(os.path.join(real, "partition")) else real
    try:
        with open(os.path.join(disk, "removable")) as f:
            return f.read().strip() == "1"
    except OSError:
        return False


@dataclass
class Drive:
    label: str
    path: str
    free_bytes: int | None = None
    device: str = ""            # e.g. /dev/sdb1

    def describe(self) -> str:
        free = f", {self.free_bytes / 1e9:.1f} GB free" if self.free_bytes is not None else ""
        return f"{self.label} ({self.path}{free})"


def removable_drives(mounts_file: str = "/proc/mounts", removable=is_removable) -> list[Drive]:
    """Mounted USB sticks / SD cards: block devices mounted under a removable-media root."""
    out, seen = [], set()
    try:
        with open(mounts_file) as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/") or parts[0] in seen:
            continue
        mount = parts[1].replace("\\040", " ")
        if not (mount.startswith(AUTOMOUNT_ROOTS) or (mount.startswith(MANUAL_ROOTS) and removable(parts[0]))):
            continue
        seen.add(parts[0])
        try:
            free = shutil.disk_usage(mount).free
        except OSError:
            free = None
        out.append(Drive(os.path.basename(mount.rstrip("/")) or parts[0], mount, free, parts[0]))
    return out


def _walk_files(path: str) -> list[tuple[str, str]]:
    """(source file, path relative to the item's parent) for a file or a whole folder."""
    base = os.path.dirname(os.path.abspath(path))
    if os.path.isfile(path):
        return [(path, os.path.relpath(path, base))]
    out = []
    for dirpath, _, files in os.walk(path):
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            out.append((full, os.path.relpath(full, base)))
    return out


def export_size(items: list[str]) -> int:
    return sum(os.path.getsize(f) for item in items for f, _ in _walk_files(item))


def export_items(items: list[str], dest_root: str, progress=None) -> tuple[str, int]:
    """Copy captures (folders) and reports (files) into a new dated folder on ``dest_root``.

    ``progress(done_bytes, total_bytes)`` is called as it goes. Returns (folder, files copied).
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(dest_root, f"CANopener_export_{stamp}")
    files = [pair for item in items for pair in _walk_files(item)]
    total = sum(os.path.getsize(f) for f, _ in files) or 1
    done = 0
    for src, rel in files:
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(src, target)
        done += os.path.getsize(src)
        if progress:
            progress(done, total)
    if files and hasattr(os, "sync"):
        os.sync()                   # flush to the stick before anyone pulls it
    return dest, len(files)


def _parent_disk(device: str) -> str:
    """/dev/sdb1 -> /dev/sdb (the whole stick, which is what gets powered off)."""
    name = os.path.basename(device)
    try:
        real = os.path.realpath(f"/sys/class/block/{name}")
        if os.path.exists(os.path.join(real, "partition")):
            return "/dev/" + os.path.basename(os.path.dirname(real))
    except OSError:
        pass
    return device


def eject(drive: Drive) -> tuple[bool, str]:
    """Unmount the drive and power it off, as the file manager's eject does (no root needed)."""
    udisksctl = shutil.which("udisksctl")
    if udisksctl and drive.device:
        r = subprocess.run([udisksctl, "unmount", "-b", drive.device], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip() or "unmount failed"
        subprocess.run([udisksctl, "power-off", "-b", _parent_disk(drive.device)],
                       capture_output=True, text=True, timeout=30)
        return True, "ejected"
    gio = shutil.which("gio")
    if gio:
        r = subprocess.run([gio, "mount", "-e", drive.path], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, "ejected"
        return False, (r.stderr or r.stdout).strip() or "eject failed"
    return False, "no udisksctl or gio available to eject; use the file manager"


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
