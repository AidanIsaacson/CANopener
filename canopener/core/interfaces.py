"""Discover and configure Linux CAN interfaces (can0, vcan0, slcan0 ...)."""

import json
import os
import re
import shlex
import shutil
import subprocess
import time
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


def _ip() -> str:
    return shutil.which("ip") or "/usr/sbin/ip"


def setup_steps(name: str, bitrate: int, listen_only: bool = False,
                sys_net: str = SYS_NET) -> list[list[str]]:
    """argv lists (each run as root) that (re)configure an interface and bring it up."""
    if not valid_name(name):
        raise ValueError(f"bad interface name: {name!r}")
    ip = _ip()
    if name.startswith("vcan"):
        steps = [["modprobe", "vcan"]]
        if not os.path.exists(os.path.join(sys_net, name)):
            steps.append([ip, "link", "add", "dev", name, "type", "vcan"])
        return steps + [[ip, "link", "set", name, "up"]]
    return [
        [ip, "link", "set", name, "down"],
        [ip, "link", "set", name, "type", "can", "bitrate", str(int(bitrate)),
         "listen-only", "on" if listen_only else "off"],
        [ip, "link", "set", name, "up"],
    ]


def restart_ms_step(name: str) -> list[str]:
    """Best-effort auto-recovery from bus-off. Many USB adapters (gs_usb / candleLight) don't
    support it and reject the command with 'Device does not support restart from Bus Off', so it
    is run as an optional step: the interface still comes up when it fails."""
    return [_ip(), "link", "set", name, "type", "can", "restart-ms", "100"]


def bring_up(name: str, bitrate: int, listen_only: bool = False, timeout: float = 90) -> tuple[bool, str]:
    """Configure and bring up an interface, setting restart-ms best-effort (see above)."""
    steps = setup_steps(name, bitrate, listen_only)
    optional: set[int] = set()
    if not name.startswith("vcan"):
        steps = steps[:-1] + [restart_ms_step(name)] + steps[-1:]   # restart-ms before 'up'
        optional = {len(steps) - 2}
    return run_privileged(steps, timeout=timeout, optional=optional)


def setup_command(name: str, bitrate: int, listen_only: bool = False) -> str:
    """The same steps as one shell command, for showing to the user or running by hand."""
    return " && ".join(shlex.join(step) for step in setup_steps(name, bitrate, listen_only))


def passwordless_sudo(argv: list[str]) -> bool:
    """True if sudoers lets us run ``argv`` without a password (see deploy/raspberry-pi)."""
    if not shutil.which("sudo"):
        return False
    try:
        return subprocess.run(["sudo", "-n", "-l", *argv], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_privileged(steps: list[list[str]], timeout: float = 90, optional=()) -> tuple[bool, str]:
    """Run each step as root. Returns (ok, message).

    Order of preference: already root; passwordless sudo for exactly these commands (the
    kiosk setup on a Pi, where there's nobody to type a password); pkexec, which pops up a
    graphical password prompt once for all steps.

    ``optional`` is a set of step indices that may fail without failing the whole operation
    (e.g. restart-ms on an adapter that doesn't support it). They keep their place in the order.
    """
    optional = set(optional)
    # Shell form: optional steps wrapped so their failure doesn't break the && chain.
    manual = " && ".join(f"{{ {shlex.join(s)} || true; }}" if i in optional else shlex.join(s)
                         for i, s in enumerate(steps))
    if os.geteuid() == 0:
        runs = list(steps)
    elif all(passwordless_sudo(step) for step in steps):
        runs = [["sudo", "-n", *step] for step in steps]
    elif shutil.which("pkexec"):
        return _run_one(["pkexec", "sh", "-c", manual], manual, timeout)
    else:
        return False, f"No way to get root (no sudo rule, no pkexec). Run manually:\n  sudo sh -c '{manual}'"
    for i, argv in enumerate(runs):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"Could not run: {e}\n\nRun manually:\n  sudo sh -c '{manual}'"
        if r.returncode != 0 and i not in optional:
            return False, (r.stderr or r.stdout).strip() or f"exit status {r.returncode}"
    return True, "ok"


def _run_one(argv: list[str], manual: str, timeout: float) -> tuple[bool, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Could not run: {e}\n\nRun manually:\n  sudo sh -c '{manual}'"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or f"exit status {r.returncode}"
    return True, "ok"


def detect_bitrate(name: str, candidates=BITRATES, listen_secs: float = 1.5, min_frames: int = 3,
                   runner=run_privileged, open_backend=None, progress=None) -> tuple[int | None, list[str]]:
    """Find the bus bitrate by listening at each candidate rate. Returns (bitrate or None, log).

    Every attempt uses **listen-only** mode: a controller at the wrong bitrate in normal mode
    sends error frames onto the machine's bus, which can upset the ECUs on it. If the adapter
    can't do listen-only we stop rather than risk that.
    """
    from ..can.backends import SocketCanBackend
    open_backend = open_backend or SocketCanBackend
    log = []
    for br in candidates:
        if progress:
            progress(br)
        ok, msg = runner(setup_steps(name, br, listen_only=True))
        if not ok:
            log.append(f"{br // 1000}k: could not set listen-only mode ({msg})")
            return None, log
        backend = open_backend(name)
        try:
            backend.open()
        except OSError as e:
            log.append(f"{br // 1000}k: {e}")
            return None, log
        good = errors = 0
        end = time.monotonic() + listen_secs
        try:
            while time.monotonic() < end and good < min_frames:
                fr = backend.recv()
                if fr is None:
                    continue
                if fr.error:
                    errors += 1
                else:
                    good += 1
        finally:
            backend.close()
        log.append(f"{br // 1000}k: {good} frame(s), {errors} error frame(s)")
        if good >= min_frames:
            return br, log
    return None, log
