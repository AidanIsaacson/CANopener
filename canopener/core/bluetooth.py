"""Bluetooth OBD adapters: find, pair, name and forget them, for both front-ends.

Everything goes through BlueZ's own ``bluetoothctl`` (standard on Raspberry Pi OS and desktop
Linux): no root, no extra Python packages. The desktop's Pair / Manage dialog and the handheld's
Bluetooth screen call these functions on a worker thread (a scan takes ~10 s).

The app connects with Classic Bluetooth serial (SPP / RFCOMM, ``can.elm327``). Many adapters,
the vLinker MC+ among them, have Classic *and* Bluetooth Low Energy (BLE, for iPhones) and can
show up twice in a scan. ``Device.kind`` says which entry is which: ``serial`` (Classic, offers
the Serial Port service: the one to pair), ``classic`` (Classic, services not known yet: most
likely fine), ``ble`` (Low Energy only: can't be used) or ``unknown``.

Names you give adapters are CANopener's own (``settings.json``: ``bluetooth.names``), so they're
the same on the desktop and the handheld and don't depend on BlueZ.
"""

import os
import re
import select
import shutil
import subprocess
import time
from dataclasses import dataclass

BLUETOOTHCTL = "bluetoothctl"       # tests point this at a stand-in
SCAN_SECONDS = 10
PAIR_TIMEOUT = 30.0
DEFAULT_PIN = "1234"
SERIAL_PORT_UUID = "00001101-0000-1000-8000-00805f9b34fb"
OBD_NAME = re.compile(r"obd|elm|v-?link|vgate|icar|veepeak|konnwei|carista|bafx|wican|kiwi|lelink|panlong|"
                      r"scan ?tool|obdii|torque", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x01|\x02")
_DEVICE_LINE = re.compile(r"Device ([0-9A-Fa-f:]{17})(?: (.*))?$")

KINDS = {
    "serial": "Classic Bluetooth, serial port: use this one",
    "classic": "Classic Bluetooth",
    "ble": "Bluetooth Low Energy only: can't be used (pick the adapter's Classic entry)",
    "unknown": "",
}


class BluetoothError(Exception):
    pass


@dataclass
class Device:
    address: str
    name: str = ""
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    kind: str = "unknown"           # serial / classic / ble / unknown (see the module docstring)
    rssi: int | None = None

    @property
    def looks_like_obd(self) -> bool:
        return bool(OBD_NAME.search(self.name or ""))

    @property
    def usable(self) -> bool:
        return self.kind != "ble"

    @property
    def kind_text(self) -> str:
        return KINDS.get(self.kind, "")


@dataclass
class ControllerState:
    present: bool
    powered: bool = False
    blocked: bool = False
    name: str = ""

    def problem(self) -> str:
        """'' when Bluetooth is ready, else what's wrong and how to fix it, in plain words."""
        if not available():
            return ("Bluetooth tools aren't installed. On Raspberry Pi OS / Debian / Ubuntu: "
                    "sudo apt install bluez")
        if not self.present:
            return "No Bluetooth on this computer (or the Bluetooth service isn't running)."
        if self.blocked:
            return ("Bluetooth is switched off (blocked). Turn it on from the desktop's Bluetooth menu, "
                    "or run: rfkill unblock bluetooth")
        if not self.powered:
            return "Bluetooth is off. Press Turn on Bluetooth, or turn it on from the desktop's Bluetooth menu."
        return ""


# ----------------------------------------------------------------------
# running bluetoothctl
# ----------------------------------------------------------------------
def available() -> bool:
    return shutil.which(BLUETOOTHCTL) is not None


def _clean(text: str) -> str:
    return _ANSI.sub("", text).replace("\r", "\n")


def run(*args: str, timeout: float = 10.0) -> str:
    """One non-interactive bluetoothctl command; its output with colours stripped."""
    if not available():
        raise BluetoothError("bluetoothctl isn't installed (sudo apt install bluez)")
    try:
        p = subprocess.run([BLUETOOTHCTL, *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return _clean(out)
    except OSError as e:
        raise BluetoothError(f"couldn't run bluetoothctl: {e}") from e
    return _clean((p.stdout or "") + (p.stderr or ""))


def controller() -> ControllerState:
    if not available():
        return ControllerState(False)
    out = run("show", timeout=5.0)
    if "Controller" not in out or "No default controller" in out:
        return ControllerState(False)
    powered = re.search(r"^\s*Powered:\s*yes", out, re.MULTILINE) is not None
    blocked = re.search(r"PowerState:\s*off-blocked", out) is not None
    name = re.search(r"^\s*Name:\s*(.+)$", out, re.MULTILINE)
    return ControllerState(True, powered, blocked, name.group(1).strip() if name else "")


def power_on() -> bool:
    out = run("power", "on", timeout=8.0)
    return "succeeded" in out.lower() or controller().powered


def _device_lines(out: str) -> list[Device]:
    found: dict[str, Device] = {}
    for line in out.splitlines():
        m = _DEVICE_LINE.search(line.strip())
        if m and "[DEL]" not in line:
            addr = m.group(1).upper()
            name = (m.group(2) or "").strip()
            if addr not in found or (name and not found[addr].name):
                found[addr] = Device(addr, name if name.replace("-", ":").upper() != addr else "")
    return list(found.values())


def info(address: str) -> Device:
    """What BlueZ knows about one device: name, paired / trusted, and which kind of Bluetooth."""
    out = run("info", address, timeout=5.0)
    dev = Device(address.upper())

    def field(name):
        m = re.search(rf"^\s*{name}:\s*(.+)$", out, re.MULTILINE)
        return m.group(1).strip() if m else ""

    dev.name = field("Alias") or field("Name")
    dev.paired = field("Paired") == "yes"
    dev.trusted = field("Trusted") == "yes"
    dev.connected = field("Connected") == "yes"
    rssi = re.search(r"RSSI:\s*(?:0x[0-9a-f]+ )?\(?(-?\d+)\)?", out)
    dev.rssi = int(rssi.group(1)) if rssi else None
    low = out.lower()
    if SERIAL_PORT_UUID in low or "serial port" in low:
        dev.kind = "serial"
    elif field("Class") or field("Icon") in ("computer", "phone"):
        dev.kind = "classic"            # a Class of Device is Classic-only
    elif field("AddressType") == "random" or "generic attribute" in low:
        dev.kind = "ble"
    elif "Device" in out:
        dev.kind = "unknown"
    return dev


def paired_devices() -> list[Device]:
    out = run("devices", "Paired", timeout=5.0)
    if "Invalid" in out or "Usage" in out or not out.strip():
        out = run("paired-devices", timeout=5.0)    # BlueZ before 5.65
    devices = _device_lines(out)
    for d in devices:
        d.paired = True
    return devices


def scan(seconds: int = SCAN_SECONDS) -> list[Device]:
    """Look for nearby devices for ``seconds``, then describe each one BlueZ knows of. OBD-looking
    names come first, then Classic before BLE. Already paired ones are included (marked)."""
    out = run("--timeout", str(seconds), "scan", "on", timeout=seconds + 8.0)
    seen = {d.address: d for d in _device_lines(out)}
    for d in _device_lines(run("devices", timeout=5.0)):
        seen.setdefault(d.address, d)
    result = []
    for addr, d in seen.items():
        try:
            full = info(addr)
        except BluetoothError:
            full = d
        full.name = full.name or d.name
        result.append(full)
    order = {"serial": 0, "classic": 1, "unknown": 2, "ble": 3}
    result.sort(key=lambda d: (not d.looks_like_obd, order.get(d.kind, 2), d.name.lower() or "~", d.address))
    return result


def pair(address: str, pin: str = DEFAULT_PIN, timeout: float = PAIR_TIMEOUT) -> str:
    """Pair with (and trust) an adapter, answering a PIN request with ``pin`` and confirming a
    passkey if asked. Returns a short message; raises BluetoothError when pairing fails."""
    if not available():
        raise BluetoothError("bluetoothctl isn't installed (sudo apt install bluez)")
    try:
        p = subprocess.Popen([BLUETOOTHCTL], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, bufsize=0)
    except OSError as e:
        raise BluetoothError(f"couldn't run bluetoothctl: {e}") from e
    out = ""

    def say(line: str):
        try:
            p.stdin.write((line + "\n").encode())
            p.stdin.flush()
        except OSError:
            pass

    try:
        say("agent KeyboardDisplay")
        say("default-agent")
        say(f"pair {address}")
        deadline = time.time() + timeout
        answered_pin = answered_yes = False
        fd = p.stdout.fileno()
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.25)
            if r:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                out += _clean(chunk.decode(errors="replace"))
            low = out.lower()
            if not answered_pin and ("enter pin code" in low or "enter passkey" in low):
                say(pin)
                answered_pin = True
            if not answered_yes and ("(yes/no)" in low or "confirm passkey" in low):
                say("yes")
                answered_yes = True
                out = out.replace("(yes/no)", "(answered)")
            if "pairing successful" in low or "already paired" in low or "alreadyexists" in low:
                say(f"trust {address}")
                time.sleep(0.5)
                return "Paired" + (" (already paired)" if "already" in low else "")
            if "failed to pair" in low or "authenticationfailed" in low or "not available" in low:
                break
        raise BluetoothError(_pair_failure(out, address))
    finally:
        say("quit")
        try:
            p.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            p.kill()


def _pair_failure(out: str, address: str) -> str:
    low = out.lower()
    if "authenticationfailed" in low or "authentication failed" in low:
        return "Pairing failed: wrong PIN? Most adapters use 1234, some 0000 or 6789."
    if "not available" in low:
        return (f"{address} isn't in range. Plug the adapter into the vehicle (some only wake with the "
                "ignition on), press Scan, then pair.")
    if "authenticationcanceled" in low or "canceled" in low:
        return "The adapter cancelled pairing. Try again; some adapters need a button press first."
    tail = [line.strip() for line in out.splitlines() if line.strip()][-2:]
    return "Pairing didn't finish" + (": " + " / ".join(tail) if tail else ". Is the adapter powered and in range?")


def trust(address: str) -> bool:
    return "succeeded" in run("trust", address).lower()


def remove(address: str) -> bool:
    """Forget (unpair) a device."""
    out = run("remove", address).lower()
    return "removed" in out or "not available" in out


# ----------------------------------------------------------------------
# adapter names and the picker (settings shared by both front-ends)
# ----------------------------------------------------------------------
def names(settings) -> dict[str, str]:
    return dict(settings.get("bluetooth.names") or {})


def set_name(settings, address: str, name: str):
    n = names(settings)
    if name.strip():
        n[address.upper()] = name.strip()
    else:
        n.pop(address.upper(), None)
    settings.set("bluetooth.names", n)


def forget_name(settings, address: str):
    n = names(settings)
    if n.pop(address.upper(), None) is not None:
        settings.set("bluetooth.names", n)
    if (settings.get("elm.adapter") or "").upper() == address.upper():
        settings.set("elm.adapter", "")


def label(settings, address: str, bluez_name: str = "") -> str:
    """'vLinker (truck)' if you named it, else BlueZ's name, else the address."""
    return names(settings).get(address.upper()) or bluez_name or address


def adapter_choices(settings, paired: list[Device] | None = None) -> list[tuple[str, str]]:
    """(address, label) for the Adapter picker: the adapters you named, then paired devices that
    look like OBD adapters, the selected one kept even if it isn't paired any more."""
    named = names(settings)
    out: dict[str, str] = {addr: name for addr, name in named.items()}
    for d in paired or []:
        if d.address not in out and (d.looks_like_obd or d.kind == "serial"):
            out[d.address] = d.name or d.address
    sel = (settings.get("elm.adapter") or "").upper()
    if sel and sel not in out:
        out[sel] = sel
    return sorted(out.items(), key=lambda kv: (kv[0] not in named, kv[1].lower()))
