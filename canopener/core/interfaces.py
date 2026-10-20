"""Discover and configure Linux CAN interfaces (can0, vcan0, slcan0 ...)."""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

ARPHRD_CAN = 280
SYS_NET = "/sys/class/net"
BITRATES = [250000, 500000, 125000, 1000000]   # 250k is the J1939 default; 500k for J1939-14


@dataclass
class CanInterface:
    name: str
    state: str          # "up" / "down" / "unknown"
    bitrate: int | None = None
    listen_only: bool | None = None

    @property
    def virtual(self) -> bool:
        return self.name.startswith("vcan")

    def describe(self) -> str:
        bits = [self.state]
        if self.bitrate:
            bits.append(f"{self.bitrate // 1000}k")
        if self.listen_only:
            bits.append("listen-only")
        return f"{self.name} ({', '.join(bits)})"


def list_interfaces(sys_net: str = SYS_NET) -> list[CanInterface]:
    out = []
    try:
        names = sorted(os.listdir(sys_net))
    except OSError:
        return out
    for name in names:
        try:
            with open(os.path.join(sys_net, name, "type")) as f:
                if int(f.read().strip()) != ARPHRD_CAN:
                    continue
        except (OSError, ValueError):
            continue
        state = "unknown"
        try:
            with open(os.path.join(sys_net, name, "operstate")) as f:
                state = f.read().strip()
            with open(os.path.join(sys_net, name, "flags")) as f:
                if int(f.read().strip(), 16) & 0x1:      # IFF_UP
                    state = "up" if state in ("up", "unknown") else state
        except (OSError, ValueError):
            pass
        iface = CanInterface(name, state)
        _fill_details(iface)
        out.append(iface)
    return out


def _fill_details(iface: CanInterface):
    ip = shutil.which("ip")
    if not ip or iface.virtual:
        return
    try:
        r = subprocess.run([ip, "-details", "-json", "link", "show", iface.name],
                           capture_output=True, text=True, timeout=2)
        info = json.loads(r.stdout)[0].get("linkinfo", {}).get("info_data", {})
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return
    iface.bitrate = info.get("bittiming", {}).get("bitrate")
    iface.listen_only = "LISTEN-ONLY" in info.get("ctrlmode", [])


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name))


def setup_command(name: str, bitrate: int, listen_only: bool = False) -> str:
    """Shell command (run as root) that (re)configures an interface and brings it up."""
    if not valid_name(name):
        raise ValueError(f"bad interface name: {name!r}")
    if name.startswith("vcan"):
        return (f"modprobe vcan 2>/dev/null; ip link show {name} >/dev/null 2>&1 || "
                f"ip link add dev {name} type vcan; ip link set {name} up")
    lo = "on" if listen_only else "off"
    return (f"ip link set {name} down; ip link set {name} type can bitrate {int(bitrate)} "
            f"listen-only {lo} restart-ms 100 && ip link set {name} up")


def run_privileged(cmd: str, timeout: float = 90) -> tuple[bool, str]:
    """Run via pkexec (graphical password prompt). Returns (ok, message)."""
    if os.geteuid() == 0:
        argv = ["sh", "-c", cmd]
    elif shutil.which("pkexec"):
        argv = ["pkexec", "sh", "-c", cmd]
    else:
        return False, f"pkexec not found. Run manually:\n  sudo sh -c '{cmd}'"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Could not run: {e}\n\nRun manually:\n  sudo sh -c '{cmd}'"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or f"exit status {r.returncode}"
    return True, "ok"
