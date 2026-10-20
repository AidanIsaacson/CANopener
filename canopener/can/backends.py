"""Bus backends. A backend moves raw frames; all J1939 logic lives above it in ``Session``.

* ``SocketCanBackend`` - the Linux kernel CAN stack (can0, vcan0, slcan0 ...), no extra deps.
* ``VirtualBus`` / ``VirtualBackend`` - in-process bus for the simulator and tests.
* ``LogReplayBackend`` (in ``logfile``) - plays a recorded log back in real time.
"""

import queue
import socket
import struct
import threading
import time
from dataclasses import replace

from .frame import CanFrame

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x7FF
CAN_ERR_MASK = 0x1FFFFFFF
FRAME_FMT = "=IB3x8s"
FRAME_SIZE = struct.calcsize(FRAME_FMT)

# linux/can/error.h error classes (in the can_id of an error frame)
_ERR_CLASSES = [
    (0x001, "TX timeout"),
    (0x002, "lost arbitration"),
    (0x004, "controller problem"),
    (0x008, "protocol violation"),
    (0x010, "transceiver problem"),
    (0x020, "no ACK on transmission"),
    (0x040, "bus-off"),
    (0x080, "bus error"),
    (0x100, "controller restarted"),
]
_CTRL_DETAILS = [
    (0x01, "RX overflow"), (0x02, "TX overflow"), (0x04, "RX warning"), (0x08, "TX warning"),
    (0x10, "RX error-passive"), (0x20, "TX error-passive"), (0x40, "back to error-active"),
]


def describe_error_frame(err_class: int, data: bytes) -> str:
    parts = [text for bit, text in _ERR_CLASSES if err_class & bit]
    if err_class & 0x004 and len(data) > 1:
        parts += [text for bit, text in _CTRL_DETAILS if data[1] & bit]
    return ", ".join(parts) or f"error 0x{err_class:X}"


class BackendError(Exception):
    pass


class CanBackend:
    """Interface every backend implements."""

    kind = "base"
    can_transmit = True

    def __init__(self, channel: str = ""):
        self.channel = channel

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def send(self, frame: CanFrame) -> None:
        """Transmit a frame. Raise OSError on failure."""
        raise NotImplementedError

    def recv(self) -> CanFrame | None:
        """Block briefly (<= ~0.2 s) for one frame. None on timeout. OSError on bus failure."""
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.kind}:{self.channel}"


class SocketCanBackend(CanBackend):
    kind = "socketcan"

    def __init__(self, channel: str = "can0"):
        super().__init__(channel)
        self.sock = None

    def open(self):
        if not hasattr(socket, "AF_CAN"):
            raise BackendError("This Python has no SocketCAN support (Linux only).")
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            # Ask the kernel for error frames so we can report bus-off / no-ACK problems.
            s.setsockopt(socket.SOL_CAN_RAW, getattr(socket, "CAN_RAW_ERR_FILTER", 2),
                         struct.pack("=I", CAN_ERR_MASK))
        except OSError:
            pass
        try:
            s.bind((self.channel,))
        except OSError:
            s.close()
            raise
        s.settimeout(0.2)
        self.sock = s

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def send(self, frame: CanFrame):
        if not self.sock:
            raise OSError("interface not open")
        can_id = (frame.can_id & CAN_EFF_MASK) | CAN_EFF_FLAG if frame.extended else frame.can_id & CAN_SFF_MASK
        self.sock.send(struct.pack(FRAME_FMT, can_id, len(frame.data), bytes(frame.data).ljust(8, b"\x00")))

    def recv(self):
        try:
            raw = self.sock.recv(FRAME_SIZE)
        except TimeoutError:
            return None
        t = time.time()
        can_id, dlc, data = struct.unpack(FRAME_FMT, raw)
        data = data[:min(dlc, 8)]
        if can_id & CAN_ERR_FLAG:
            cls = can_id & CAN_ERR_MASK
            return CanFrame(cls, bytes(data), True, t, error=describe_error_frame(cls, data))
        if can_id & CAN_RTR_FLAG:
            return None
        ext = bool(can_id & CAN_EFF_FLAG)
        return CanFrame(can_id & (CAN_EFF_MASK if ext else CAN_SFF_MASK), bytes(data), ext, t)


class VirtualBus:
    """A software CAN bus: every frame sent by one endpoint is delivered to all the others."""

    def __init__(self):
        self._lock = threading.Lock()
        self._endpoints: list[VirtualBackend] = []

    def attach(self, ep):
        with self._lock:
            if ep not in self._endpoints:
                self._endpoints.append(ep)

    def detach(self, ep):
        with self._lock:
            if ep in self._endpoints:
                self._endpoints.remove(ep)

    def deliver(self, sender, frame: CanFrame):
        with self._lock:
            targets = [ep for ep in self._endpoints if ep is not sender]
        for ep in targets:
            ep.queue.put(frame)


class VirtualBackend(CanBackend):
    kind = "virtual"

    def __init__(self, bus: VirtualBus, channel: str = "virtual"):
        super().__init__(channel)
        self.bus = bus
        self.queue: queue.Queue[CanFrame] = queue.Queue()
        self.is_open = False

    def open(self):
        self.bus.attach(self)
        self.is_open = True

    def close(self):
        self.bus.detach(self)
        self.is_open = False

    def send(self, frame: CanFrame):
        if not self.is_open:
            raise OSError("virtual interface not open")
        self.bus.deliver(self, replace(frame, timestamp=time.time(), tx=False))

    def recv(self):
        try:
            return self.queue.get(timeout=0.2)
        except queue.Empty:
            return None
