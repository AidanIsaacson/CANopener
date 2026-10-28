"""A simulated CANopen (CiA 301) machine, so the CANopen side can be shown and tested with no truck:
a battery truck's traction controller, pump controller and battery module.

* Each node sends a boot-up, then heartbeats; the traction controller acts as the NMT master and
  starts the network, as a truck's main controller does. NMT commands from the tool work (stop,
  pre-operational, reset; a reset node boots again and is restarted).
* TPDOs with moving values while operational (motor speed and current, battery voltage, throttle,
  direction switches, temperatures, state of charge). Their layout is made up for the simulator:
  the ``sim-canopen`` profile labels it.
* EMCY: the traction controller reports a device temperature error that stays active; the pump
  controller reports a heartbeat error that clears a few seconds later.
* An SDO server for the standard objects: device type, name and versions (segmented answers),
  identity, error register, heartbeat time, and the error history (0x1003), which can be cleared
  by writing 0 to sub 0. Any other write is refused, and the battery module has no error history
  (it answers "object does not exist").

None of it describes a real controller; every name says (sim).
"""

import math
import threading
import time
from dataclasses import dataclass, field

from ..can.backends import CanBackend
from ..can.frame import CanFrame
from ..canopen import cob, emcy, nmt, objects, sdo

TRACTION, PUMP, BATTERY = 0x01, 0x02, 0x20
VENDOR_SIM = 0x00000000     # no vendor ID: this isn't anybody's product


@dataclass
class _Node:
    node: int
    name: str
    device_type: int
    hw: str
    sw: str
    serial: int
    heartbeat_ms: int
    history: list[int] | None          # None: no 0x1003 object
    state: int = nmt.BOOT_UP
    booted_at: float = 0.0
    next_hb: float = 0.0
    next_pdo: dict[int, float] = field(default_factory=dict)
    upload: list[bytes] = field(default_factory=list)     # segments still to send
    toggle: int = 0
    register: int = 0

    def od(self) -> dict[tuple[int, int], bytes]:
        u32 = lambda v: (v & 0xFFFFFFFF).to_bytes(4, "little")   # noqa: E731
        d = {
            (objects.DEVICE_TYPE, 0): u32(self.device_type),
            (objects.ERROR_REGISTER, 0): bytes([self.register]),
            (objects.DEVICE_NAME, 0): self.name.encode("latin-1"),
            (objects.HARDWARE_VERSION, 0): self.hw.encode("latin-1"),
            (objects.SOFTWARE_VERSION, 0): self.sw.encode("latin-1"),
            (objects.HEARTBEAT_TIME, 0): self.heartbeat_ms.to_bytes(2, "little"),
            (objects.IDENTITY, 0): bytes([4]),
            (objects.IDENTITY, 1): u32(VENDOR_SIM),
            (objects.IDENTITY, 2): u32(0x5100 + self.node),
            (objects.IDENTITY, 3): u32(0x00010002),
            (objects.IDENTITY, 4): u32(self.serial),
        }
        if self.history is not None:
            d[(objects.ERROR_HISTORY, 0)] = bytes([len(self.history)])
            for i, v in enumerate(self.history):
                d[(objects.ERROR_HISTORY, i + 1)] = u32(v)
        return d


class CanopenSimulator:
    def __init__(self, backend: CanBackend, seed: float | None = None):
        self.backend = backend
        self.t0 = seed if seed is not None else time.time()
        self.started = time.time()
        self.nodes = {
            TRACTION: _Node(TRACTION, "SIM TRACTION CONTROLLER", 402, "HW-A (sim)", "SIM 2.1.0", 4242, 500,
                            [0x00003220, 0x00018130]),
            PUMP: _Node(PUMP, "SIM PUMP CONTROLLER", 402, "HW-A (sim)", "SIM 2.0.7", 4243, 500, []),
            BATTERY: _Node(BATTERY, "SIM BATTERY MODULE", 418, "BMS-1 (sim)", "SIM 1.4", 77, 1000, None),
        }
        self.emcy_plan = [(2.0, TRACTION, 0x4200, 0x09, b"\x01\x5A"),       # device temperature: stays
                          (4.0, PUMP, 0x8130, 0x11, b""),                  # heartbeat error ...
                          (7.0, PUMP, emcy.NO_ERROR, 0x00, b"")]           # ... cleared
        self.nmt_log: list[tuple[int, int]] = []
        self._start_at: float | None = None
        self._running = False
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    # --- sending ---------------------------------------------------------------------------------
    def _send(self, can_id: int, data: bytes):
        try:
            self.backend.send(CanFrame(can_id, bytes(data), False, time.time()))
        except OSError:
            pass

    def _boot(self, n: _Node, now: float):
        n.state = nmt.PRE_OPERATIONAL
        n.booted_at = now
        n.next_hb = now + n.heartbeat_ms / 1000
        n.upload, n.toggle = [], 0
        self._send(cob.HEARTBEAT + n.node, bytes([nmt.BOOT_UP]))
        if self._start_at is None:
            self._start_at = now + 0.3          # the traction controller (master) starts the network

    # --- process data ----------------------------------------------------------------------------
    def _pdos(self, n: _Node, t: float) -> dict[int, tuple[float, bytes]]:
        """COB-ID -> (period, payload) for the node's TPDOs at time ``t`` (seconds since start)."""
        if n.node == TRACTION:
            speed = int(2800 * math.sin(t / 6))
            current = int(abs(speed) / 12 + 40 + 20 * math.sin(t))                 # x0.1 A
            volts = int((48.5 - 2.0 * (1 - math.exp(-t / 300)) - abs(speed) / 6000) * 100)   # x0.01 V
            throttle = min(100, int(abs(speed) / 28))
            switches = (1 if speed > 50 else 0) | (2 if speed < -50 else 0) | 4      # fwd, rev, seat
            p1 = speed.to_bytes(2, "little", signed=True) + current.to_bytes(2, "little") \
                + volts.to_bytes(2, "little") + bytes([throttle, switches])
            p2 = bytes([int(45 + 10 * math.sin(t / 30)) & 0xFF, int(60 + 15 * math.sin(t / 40)) & 0xFF,
                        0, 0, 0, 0, 0, 0])
            return {cob.TPDO[0] + n.node: (0.05, p1), cob.TPDO[1] + n.node: (0.5, p2)}
        if n.node == PUMP:
            pump = int(max(0.0, 2400 * math.sin(t / 4)))
            pressure = int(pump / 12)                                                # bar
            return {cob.TPDO[0] + n.node: (0.1, pump.to_bytes(2, "little") + pressure.to_bytes(2, "little")
                                                + bytes([int(40 + 5 * math.sin(t / 20)), 0, 0, 0]))}
        soc = max(5, int(92 - t / 20))
        volts = int((48.5 - 2.0 * (1 - math.exp(-t / 300))) * 10)                   # x0.1 V
        return {cob.TPDO[0] + n.node: (1.0, bytes([soc]) + volts.to_bytes(2, "little") + bytes([25, 0, 0, 0, 0]))}

    def _tx_loop(self):
        while self._running:
            now = time.time()
            t = now - self.t0
            with self._lock:
                for n in self.nodes.values():
                    if n.state == nmt.BOOT_UP:
                        continue
                    if now >= n.next_hb:
                        self._send(cob.HEARTBEAT + n.node, bytes([n.state]))
                        n.next_hb = now + n.heartbeat_ms / 1000
                    if n.state != nmt.OPERATIONAL:
                        continue
                    for can_id, (period, data) in self._pdos(n, t).items():
                        if now >= n.next_pdo.get(can_id, 0.0):
                            self._send(can_id, data)
                            n.next_pdo[can_id] = now + period
                if self._start_at is not None and now >= self._start_at:
                    self._start_at = None
                    self._send(cob.NMT, nmt.command_frame(nmt.START, nmt.ALL_NODES)[1])
                    self._nmt(nmt.START, nmt.ALL_NODES, now)
                while self.emcy_plan and now - self.started >= self.emcy_plan[0][0]:
                    _, node, code, reg, mfr = self.emcy_plan.pop(0)
                    n = self.nodes[node]
                    n.register = reg
                    if code and n.history is not None:
                        n.history.insert(0, code)
                    self._send(cob.EMCY + node, emcy.encode(code, reg, mfr))
            time.sleep(0.005)

    # --- receiving -------------------------------------------------------------------------------
    def _nmt(self, cmd: int, target: int, now: float):
        for n in self.nodes.values():
            if target not in (nmt.ALL_NODES, n.node):
                continue
            if cmd == nmt.START:
                n.state = nmt.OPERATIONAL
            elif cmd == nmt.STOP:
                n.state = nmt.STOPPED
            elif cmd == nmt.ENTER_PREOP:
                n.state = nmt.PRE_OPERATIONAL
            elif cmd in (nmt.RESET_NODE, nmt.RESET_COMM):
                self._boot(n, now)

    def _sdo(self, n: _Node, data: bytes):
        f = sdo.parse(data, from_node=False)
        if f is None or n.state == nmt.STOPPED:
            return
        reply = lambda d: self._send(cob.SDO_TX + n.node, d)   # noqa: E731
        if f.kind == "upload":
            od = n.od()
            if (f.index, f.sub) not in od:
                exists = any(i == f.index for i, _ in od)
                reply(sdo.abort_answer(f.index, f.sub, 0x06090011 if exists else 0x06020000))
                return
            frames = sdo.upload_answer(f.index, f.sub, od[(f.index, f.sub)])
            n.upload, n.toggle = frames[1:], 0
            reply(frames[0])
        elif f.kind == "upload_segment":
            if not n.upload or f.toggle != n.toggle:
                reply(sdo.abort_answer(0, 0, 0x05030000 if n.upload else 0x05040001))
                n.upload = []
                return
            reply(n.upload.pop(0))
            n.toggle ^= 1
        elif f.kind == "download":
            if (f.index, f.sub) == (objects.ERROR_HISTORY, 0) and n.history is not None and f.data[:1] == b"\0":
                n.history.clear()
                n.register = 0
                reply(bytes([sdo.SCS_INITIATE_DOWNLOAD << 5]) + data[1:4] + b"\0" * 4)
            else:
                reply(sdo.abort_answer(f.index, f.sub, 0x06010002))        # read only: we take no writes
        elif f.kind == "abort":
            n.upload = []

    def _rx_loop(self):
        while self._running:
            try:
                fr = self.backend.recv()
            except OSError:
                time.sleep(0.2)
                continue
            if fr is None or fr.extended or fr.error:
                continue
            kind, node = cob.classify(fr.can_id)
            with self._lock:
                if kind == "nmt":
                    cmd = nmt.parse_command(bytes(fr.data))
                    if cmd:
                        self.nmt_log.append(cmd)
                        self._nmt(cmd[0], cmd[1], time.time())
                        if cmd[0] in (nmt.RESET_NODE, nmt.RESET_COMM):
                            self._start_at = time.time() + 0.3      # the master brings it back up
                elif kind == "sdo_rx" and node in self.nodes:
                    self._sdo(self.nodes[node], bytes(fr.data))

    # ------------------------------------------------------------------
    def start(self):
        self.backend.open()
        self._running = True
        now = time.time()
        with self._lock:
            for n in self.nodes.values():
                self._boot(n, now)
        for target, name in ((self._tx_loop, "cosim-tx"), (self._rx_loop, "cosim-rx")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._running = False
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()
        try:
            self.backend.close()
        except OSError:
            pass

    @property
    def running(self) -> bool:
        return self._running
