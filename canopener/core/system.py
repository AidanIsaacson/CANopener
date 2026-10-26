"""The handheld as a computer: screen brightness, temperature, uptime, network, disk, power.

Read from sysfs / procfs with the standard library, so the System Settings page of any front-end
can show them. Every function takes its paths as arguments for tests and returns None (or an
empty list) where the information doesn't exist, as on a laptop with no backlight control.

Brightness needs write access to ``/sys/class/backlight/*/brightness``; the Pi installer's
``--device`` option adds a udev rule giving the ``video`` group that access.
"""

import glob
import os
import shutil
import socket
import time

SYS_BACKLIGHT = "/sys/class/backlight"
SYS_THERMAL = "/sys/class/thermal"


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def backlights(sys_backlight: str = SYS_BACKLIGHT) -> list[str]:
    return sorted(glob.glob(os.path.join(sys_backlight, "*")))


def brightness(sys_backlight: str = SYS_BACKLIGHT) -> float | None:
    """Current brightness of the first backlight, 0.0-1.0."""
    for d in backlights(sys_backlight):
        cur, top = _read(os.path.join(d, "brightness")), _read(os.path.join(d, "max_brightness"))
        if cur and top and cur.isdigit() and top.isdigit() and int(top) > 0:
            return int(cur) / int(top)
    return None


def set_brightness(level: float, sys_backlight: str = SYS_BACKLIGHT, floor: float = 0.05) -> tuple[bool, str]:
    """Set every backlight to ``level`` (0.0-1.0; never below ``floor``, so the screen can't be
    turned all the way off by accident)."""
    level = max(floor, min(1.0, level))
    found = backlights(sys_backlight)
    if not found:
        return False, "No backlight control on this screen."
    for d in found:
        top = _read(os.path.join(d, "max_brightness"))
        if not top or not top.isdigit():
            continue
        try:
            with open(os.path.join(d, "brightness"), "w") as f:
                f.write(str(max(1, round(level * int(top)))))
        except OSError as e:
            return False, (f"Can't change brightness ({e.strerror}). Run the Pi installer with --device "
                           "to allow it.")
    return True, f"Brightness {round(level * 100)}%"


def cpu_temp(sys_thermal: str = SYS_THERMAL) -> float | None:
    """CPU temperature in °C."""
    for zone in sorted(glob.glob(os.path.join(sys_thermal, "thermal_zone*"))):
        v = _read(os.path.join(zone, "temp"))
        if v and v.lstrip("-").isdigit():
            return int(v) / 1000
    return None


def uptime(proc_uptime: str = "/proc/uptime") -> float | None:
    v = _read(proc_uptime)
    try:
        return float(v.split()[0]) if v else None
    except ValueError:
        return None


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    m, _ = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    return f"{d}d {h}h {m:02d}m" if d else f"{h}h {m:02d}m"


def ip_addresses(sys_net: str = "/sys/class/net") -> list[tuple[str, str]]:
    """(interface, IPv4 address) for interfaces that are up, found without extra tools: the
    address a UDP socket would leave from, labelled with the interface that's up."""
    up = []
    for d in sorted(glob.glob(os.path.join(sys_net, "*"))):
        name = os.path.basename(d)
        if name == "lo" or _read(os.path.join(d, "operstate")) != "up":
            continue
        up.append(name)
    if not up:
        return []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))         # TEST-NET: nothing is sent, it only picks a route
            addr = s.getsockname()[0]
    except OSError:
        return []
    return [(", ".join(up), addr)]


def disk_free(path: str) -> tuple[int, int] | None:
    """(free bytes, total bytes) of the disk holding ``path``."""
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return u.free, u.total


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def clock_text(t: float | None = None) -> str:
    return time.strftime("%H:%M", time.localtime(t))


# Power. The logged-in user may do these through logind (no root, no sudoers entry); front-ends
# confirm first.
POWER_OFF = ["systemctl", "poweroff"]
REBOOT = ["systemctl", "reboot"]
