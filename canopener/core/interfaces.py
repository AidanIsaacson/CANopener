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
    slcan: bool = False     # attached by slcand: can't do listen-only (see is_slcan)

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


def list_interfaces(sys_net: str = SYS_NET, details: bool = True) -> list[CanInterface]:
    """CAN interfaces that exist now. ``details=False`` skips the bitrate / listen-only lookup
    (one ``ip`` call per interface), for the once-a-second adapter check."""
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
        iface = CanInterface(name, state, slcan=is_slcan(name, sys_net))
        if details:
            _fill_details(iface)
        out.append(iface)
    return out


SYS_USB = "/sys/bus/usb/devices"
SYS_TTY = "/sys/class/tty"

# USB IDs of the adapters the install guides cover. gs_usb devices become canN by themselves;
# slcan ones are a serial port that deploy/linux/70-canopener.rules attaches with slcand.
USB_GS_USB = {("1d50", "606f"), ("1209", "2323")}     # candleLight / CANable gs_usb firmware
USB_SLCAN = {("16d0", "117e")}                         # CANable / CANable 2.0 slcan firmware
USB_DFU = {("0483", "df11")}                           # STM32 bootloader (firmware update mode)


@dataclass
class UsbAdapter:
    kind: str           # "gs_usb" / "slcan" / "dfu"
    product: str
    tty: str = ""       # /dev/ttyACMn for slcan

    def hint(self) -> str:
        if self.kind == "slcan":
            return (f"{self.product} with slcan firmware is plugged in at {self.tty or 'a serial port'} but "
                    "isn't attached as a CAN interface. Run the installer (deploy/linux/install.sh), which "
                    "attaches it automatically on plug-in, then unplug and replug it. (Needs Linux 6.0+.)")
        if self.kind == "dfu":
            return (f"{self.product} is in firmware-update (DFU) mode. Remove the BOOT jumper / release the "
                    "boot button and replug it.")
        return (f"{self.product} is plugged in but no CAN interface appeared. The gs_usb driver may be "
                "missing: run the installer (deploy/linux/install.sh), or 'sudo modprobe gs_usb'.")


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def usb_adapters(sys_usb: str = SYS_USB, sys_tty: str = SYS_TTY) -> list[UsbAdapter]:
    """USB CAN adapters that are plugged in, recognised by USB ID (CANable-style ones; the
    other gs_usb adapters still work, they just aren't listed here). Used to explain why no CAN
    interface showed up."""
    ttys = {}
    try:
        for name in os.listdir(sys_tty):
            dev = os.path.realpath(os.path.join(sys_tty, name, "device"))
            ttys[os.path.dirname(dev)] = name          # tty's interface -> its USB device dir
    except OSError:
        pass
    out = []
    try:
        names = sorted(os.listdir(sys_usb))
    except OSError:
        return out
    for name in names:
        d = os.path.join(sys_usb, name)
        ids = (_read(os.path.join(d, "idVendor")).lower(), _read(os.path.join(d, "idProduct")).lower())
        product = _read(os.path.join(d, "product")) or "USB adapter"
        if ids in USB_GS_USB:
            out.append(UsbAdapter("gs_usb", product))
        elif ids in USB_DFU:
            out.append(UsbAdapter("dfu", product))
        else:
            tty = ttys.get(os.path.realpath(d), "")
            # Clones of the slcan firmware sometimes use other IDs but keep "CANable" in the name.
            if ids in USB_SLCAN or (tty and "canable" in product.lower()):
                out.append(UsbAdapter("slcan", product, f"/dev/{tty}" if tty else ""))
    return out


def no_adapter_hint(sys_usb: str = SYS_USB, sys_tty: str = SYS_TTY) -> str:
    """Why there is no CAN interface to connect to, as specifically as we can tell."""
    found = usb_adapters(sys_usb, sys_tty)
    if found:
        return "\n".join(a.hint() for a in found)
    return ("No CAN adapter found. Is it plugged in? Press ⟳ to re-scan after plugging it in, or pick "
            "'Simulator' to try the app without hardware.")


def is_slcan(name: str, sys_net: str = SYS_NET) -> bool:
    """True for an interface attached by slcand (a CANable with slcan firmware, or another
    serial-line adapter). Those are the only real CAN interfaces with no parent device in sysfs:
    gs_usb, PEAK, Kvaser, MCP2515 ... all have /sys/class/net/canN/device."""
    if name.startswith("vcan"):
        return False
    d = os.path.join(sys_net, name)
    return os.path.isdir(d) and not os.path.exists(os.path.join(d, "device"))


def slcan_listen_only_hint(name: str) -> str:
    """Why listen-only is refused on an slcan interface. In listen-only Linux opens the adapter with
    the slcan 'L' command, which the CANable's slcan firmware doesn't have (it uses a nonstandard
    'M1'). Linux never reads the reply, so the interface shows up, listen-only and error-active,
    while the adapter stays closed and hears nothing at any bitrate."""
    return (f"{name} is a CANable-style adapter with slcan firmware, which can't do listen-only: in "
            "listen-only it never opens and hears nothing. Untick Listen-only and pick the bitrate "
            "(250k for most J1939 machines), or flash the adapter with candleLight firmware, which "
            "does listen-only properly.")


def needs_setup(iface: CanInterface, bitrate: int | None, listen_only: bool) -> bool:
    """True if Connect has to (re)configure ``iface``: it's down, or it's up at a different bitrate
    (``None`` = any) or listen-only setting than the one picked. The signal finder, for one,
    leaves it in listen-only at whatever bitrate it was trying when it was stopped."""
    if iface.virtual:
        return False
    if iface.state != "up":
        return True
    if iface.listen_only is not None and iface.listen_only != bool(listen_only):
        return True
    return bool(bitrate) and iface.bitrate is not None and iface.bitrate != bitrate


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


def bring_up(name: str, bitrate: int, listen_only: bool = False, timeout: float = 90,
             sys_net: str = SYS_NET) -> tuple[bool, str]:
    """Configure and bring up an interface, setting restart-ms best-effort (see above). Refuses
    listen-only on slcan, where it would come up deaf (see slcan_listen_only_hint)."""
    if listen_only and is_slcan(name, sys_net):
        return False, slcan_listen_only_hint(name)
    steps = setup_steps(name, bitrate, listen_only)
    optional: set[int] = set()
    if not name.startswith("vcan"):
        steps = steps[:-1] + [restart_ms_step(name)] + steps[-1:]   # restart-ms before 'up'
        optional = {len(steps) - 2}
    return run_privileged(steps, timeout=timeout, optional=optional)


def setup_command(name: str, bitrate: int, listen_only: bool = False) -> str:
    """The same steps as one shell command, for showing to the user or running by hand."""
    return " && ".join(shlex.join(step) for step in setup_steps(name, bitrate, listen_only))


SUDOERS_PATH = "/etc/sudoers.d/canopener"


def sudoers_installed() -> bool:
    """True if the passwordless rule for bringing up CAN interfaces is already in place."""
    return passwordless_sudo([_ip(), "link", "set", "can0", "up"])


def _valid_user(user: str) -> bool:
    return bool(re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user or ""))


def sudoers_content(user: str) -> str:
    """A minimal sudoers rule letting ``user`` run only ``ip link set canN ...`` as root. No
    shell, no password stored anywhere — the standard way to avoid a prompt on every bring-up."""
    if not _valid_user(user):
        raise ValueError(f"bad username: {user!r}")
    return (
        f"# CANopener: let {user} configure CAN interfaces without a password.\n"
        f"# Installed from the app's Settings menu. Only 'ip link set canN ...' is allowed.\n"
        "Cmnd_Alias CANOPENER_IP = /usr/sbin/ip link set can[0-9] *, /usr/bin/ip link set can[0-9] *\n"
        f"{user} ALL=(root) NOPASSWD: CANOPENER_IP\n"
    )


def install_sudoers(user: str | None = None, timeout: float = 120) -> tuple[bool, str]:
    """Install the passwordless-CAN-setup rule (validated with visudo). Asks for a password once
    (via pkexec/sudo) to write the file; afterwards bringing an interface up needs no password.
    Stores no secret of any kind."""
    import getpass
    import tempfile
    user = user or getpass.getuser()
    content = sudoers_content(user)                 # raises ValueError on a bad username
    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        steps = [
            ["visudo", "-cf", tmp],                 # validate before installing (never install a broken file)
            ["install", "-o", "root", "-g", "root", "-m", "0440", tmp, SUDOERS_PATH],
        ]
        ok, msg = run_privileged(steps, timeout=timeout)
        return (True, f"Installed {SUDOERS_PATH}. Bringing up a CAN interface no longer asks for a "
                "password.") if ok else (False, msg)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def can_configure_quietly(name: str) -> bool:
    """True if (re)configuring ``name`` won't ask for a password: we're root, or the sudoers rule
    covers its setup steps. The signal finder changes the bitrate every second or so, which is
    only workable without a password prompt each time."""
    if os.geteuid() == 0:
        return True
    try:
        steps = setup_steps(name, BITRATES[0], listen_only=True)
    except ValueError:
        return False
    return all(passwordless_sudo(step) for step in steps)


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


def count_traffic(backend, secs: float, enough: int | None = None, stop=None) -> tuple[int, int]:
    """Listen on an open backend for up to ``secs``. Returns (good frames, error frames). Ends
    early once ``enough`` good frames have arrived, or when ``stop()`` returns True."""
    good = errors = 0
    end = time.monotonic() + secs
    while time.monotonic() < end and (enough is None or good < enough):
        if stop is not None and stop():
            break
        fr = backend.recv()
        if fr is None:
            continue
        if fr.error:
            errors += 1
        else:
            good += 1
    return good, errors


def detect_bitrate(name: str, candidates=BITRATES, listen_secs: float = 1.5, min_frames: int = 3,
                   runner=run_privileged, open_backend=None, progress=None,
                   sys_net: str = SYS_NET) -> tuple[int | None, list[str]]:
    """Find the bus bitrate by listening at each candidate rate. Returns (bitrate or None, log).

    Every attempt uses **listen-only** mode: a controller at the wrong bitrate in normal mode
    sends error frames onto the machine's bus, which can upset the ECUs on it. If the adapter
    can't do listen-only we stop rather than risk that.
    """
    from ..can.backends import SocketCanBackend
    open_backend = open_backend or SocketCanBackend
    if is_slcan(name, sys_net):
        return None, [slcan_listen_only_hint(name)]
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
        try:
            good, errors = count_traffic(backend, listen_secs, enough=min_frames)
        finally:
            backend.close()
        log.append(f"{br // 1000}k: {good} frame(s), {errors} error frame(s)")
        if good >= min_frames:
            return br, log
    return None, log
