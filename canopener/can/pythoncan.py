"""Any adapter python-can supports: PEAK, Kvaser, Vector, IXXAT, neoVI, gs_usb / candleLight
without the Linux driver, slcan on a serial port, socketcand over the network, and more.

Optional (``pip install -e ".[adapters]"``): Linux interfaces like ``can0`` keep using the
built-in ``SocketCanBackend``, which needs nothing extra. An adapter is named
``interface:channel``, with optional ``;key=value`` settings python-can takes::

    pcan:PCAN_USBBUS1        kvaser:0        vector:0;app_name=CANalyzer
    gs_usb:0                 slcan:/dev/ttyACM0;tty_baudrate=115200
    socketcand:192.168.1.10;port=29536;channel=can0

Linux interface names can't contain ':', so the same Interface box takes both kinds.

Frames are timestamped on arrival with ``time.time()``, like ``SocketCanBackend``: adapters'
own clocks differ (some count from power-up), and captures need every frame on wall-clock time.
"""

import importlib.util
import logging
import time
import warnings

from .backends import CAN_ERR_MASK, BackendError, CanBackend, describe_error_frame
from .frame import CanFrame

SEP = ":"
DETECT = ("pcan", "kvaser", "vector", "ixxat", "neovi", "gs_usb", "cantact", "slcan", "serial")


def is_spec(name: str) -> bool:
    """Is this an ``interface:channel`` name (python-can), not a Linux interface like can0?"""
    return SEP in (name or "")


def parse_spec(spec: str) -> tuple[str, str | None, dict]:
    """"slcan:/dev/ttyACM0;tty_baudrate=115200" -> ("slcan", "/dev/ttyACM0", {"tty_baudrate": 115200})."""
    head, *opts = (spec or "").split(";")
    interface, _, channel = head.partition(SEP)
    options: dict = {}
    for opt in opts:
        key, eq, value = opt.partition("=")
        if not eq or not key.strip():
            raise ValueError(f"bad option {opt!r} in {spec!r} (expected key=value)")
        value = value.strip()
        options[key.strip()] = int(value) if value.lstrip("-").isdigit() else \
            {"true": True, "false": False}.get(value.lower(), value)
    if not interface.strip():
        raise ValueError(f"no interface in {spec!r} (expected e.g. pcan:PCAN_USBBUS1)")
    return interface.strip(), channel.strip() or None, options


def available() -> bool:
    return importlib.util.find_spec("can") is not None


INSTALL_HINT = "python-can isn't installed. Install it with: pip install -e \".[adapters]\" (or pip install python-can)"


def detect(interfaces=DETECT, timeout: float = 2.0) -> list[str]:
    """Adapters python-can can find right now, as ``interface:channel`` names (empty without
    python-can, or when nothing is plugged in). Can take a few seconds."""
    if not available():
        return []
    import can
    out = []
    quiet = logging.getLogger("can")
    level = quiet.level
    quiet.setLevel(logging.CRITICAL)        # each missing vendor driver logs a warning: just skip them
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            configs_by = []
            for iface in interfaces:
                try:
                    configs_by.append((iface, can.detect_available_configs(interfaces=[iface], timeout=timeout)))
                except Exception:   # a vendor driver that's missing or misbehaves: skip it
                    continue
    finally:
        quiet.setLevel(level)
    for iface, configs in configs_by:
        for c in configs:
            name = f"{c.get('interface', iface)}{SEP}{c.get('channel', '')}"
            if name not in out:
                out.append(name)
    return out


class PythonCanBackend(CanBackend):
    kind = "python-can"

    def __init__(self, spec: str, bitrate: int | None = 250000, listen_only: bool = False):
        super().__init__(spec)
        self.bitrate = bitrate
        self.listen_only = listen_only
        self.hardware_listen_only = False     # True when the adapter itself was put in listen-only
        self.bus = None
        self._can = None

    def open(self):
        if not available():
            raise BackendError(INSTALL_HINT)
        import can
        self._can = can
        try:
            interface, channel, options = parse_spec(self.channel)
        except ValueError as e:
            raise BackendError(str(e)) from None
        if not self.bitrate:
            raise BackendError("Pick a bitrate: Auto (the signal finder) works with Linux interfaces like can0 only.")
        kw = {"interface": interface, "bitrate": self.bitrate, "receive_own_messages": False, **options}
        if channel is not None:
            kw["channel"] = channel
        try:
            self.bus = can.Bus(**kw)
        except Exception as e:      # python-can and vendor drivers raise all sorts
            raise BackendError(f"{self.channel}: {e}") from e
        if self.listen_only:
            try:
                self.bus.state = can.BusState.PASSIVE
                self.hardware_listen_only = True
            except Exception:       # not every adapter has a listen-only mode; the session still never sends
                pass

    def close(self):
        if self.bus is not None:
            try:
                self.bus.shutdown()
            finally:
                self.bus = None

    def send(self, frame: CanFrame):
        if self.bus is None:
            raise OSError("adapter not open")
        msg = self._can.Message(arbitration_id=frame.can_id, data=bytes(frame.data), is_extended_id=frame.extended)
        try:
            self.bus.send(msg, timeout=0.2)
        except self._can.CanError as e:
            raise OSError(str(e)) from e

    def recv(self) -> CanFrame | None:
        if self.bus is None:
            raise OSError("adapter not open")
        try:
            msg = self.bus.recv(0.2)
        except self._can.CanError as e:
            raise OSError(str(e)) from e
        if msg is None:
            return None
        t = time.time()
        data = bytes(msg.data)
        if msg.is_error_frame:
            text = describe_error_frame(msg.arbitration_id & CAN_ERR_MASK, data) if msg.arbitration_id else ""
            return CanFrame(msg.arbitration_id & CAN_ERR_MASK, data, True, t, error=text or "error frame")
        if msg.is_remote_frame:
            return None
        return CanFrame(msg.arbitration_id, data, bool(msg.is_extended_id), t)

    def describe(self) -> str:
        rate = f" at {self.bitrate // 1000}k" if self.bitrate else ""
        return f"{self.channel}{rate} (python-can)"
