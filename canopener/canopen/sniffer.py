"""Follow CANopen traffic in a stream of 11-bit CAN frames, live or from a log, without sending.

From the frames alone: which nodes are there, each
node's NMT state and heartbeat period, EMCY errors (active ones, and the history), NMT commands
the master sent, what each node's PDOs carry, and any object values read over SDO by us or by
another tool (identity, names, versions, error history). Pure protocol: no app imports. Not
thread-safe on its own: the ``Session`` feeds it under its lock.

A node counts as present only once it confirms itself with a heartbeat / boot-up (CiA 301 nodes
must send one, or answer node guarding on the same ID) or an SDO answer. Plenty of machines use
plain 11-bit IDs that merely fall in CANopen's ranges (0x181, 0x201 ...); those never become
"nodes". What an unconfirmed node ID sent just before is kept briefly and replayed once it's
confirmed, so an EMCY right before the first heartbeat isn't lost.
"""

from collections import deque
from dataclasses import dataclass, field

from . import cob, emcy, nmt, sdo


@dataclass
class CobStats:
    count: int = 0
    first: float = 0.0
    last: float = 0.0
    period: float = 0.0         # smoothed seconds between frames
    data: bytes = b""

    def seen(self, t: float, data: bytes):
        if self.count:
            dt = t - self.last
            self.period = dt if not self.period else self.period * 0.9 + dt * 0.1
        else:
            self.first = t
        self.count += 1
        self.last = t
        self.data = data


@dataclass
class EmcyEvent:
    t: float
    node: int
    emcy: emcy.Emcy


@dataclass
class NodeInfo:
    node: int
    first_seen: float = 0.0
    last_seen: float = 0.0
    frames: int = 0
    state: int | None = None
    state_t: float = 0.0
    heartbeat: CobStats = field(default_factory=CobStats)
    boots: int = 0                      # boot-up messages seen (restarts)
    cobs: dict[int, CobStats] = field(default_factory=dict)      # every COB-ID to / from it
    active: dict[int, EmcyEvent] = field(default_factory=dict)  # EMCY code -> latest, since the last reset
    register: int = 0                   # error register from the latest EMCY
    od: dict[tuple[int, int], bytes] = field(default_factory=dict)  # values read over SDO
    aborts: dict[tuple[int, int], int] = field(default_factory=dict)  # reads the node refused

    @property
    def state_text(self) -> str:
        return nmt.state_name(self.state)

    @property
    def heartbeat_ms(self) -> int | None:
        return round(self.heartbeat.period * 1000) if self.heartbeat.count > 1 else None

    def value(self, index: int, sub: int = 0) -> bytes | None:
        return self.od.get((index, sub))

    def copy(self) -> "NodeInfo":
        return NodeInfo(self.node, self.first_seen, self.last_seen, self.frames, self.state, self.state_t,
                        CobStats(**vars(self.heartbeat)), self.boots,
                        {k: CobStats(**vars(v)) for k, v in self.cobs.items()}, dict(self.active),
                        self.register, dict(self.od), dict(self.aborts))


class _Upload:
    __slots__ = ("index", "sub", "buf", "size")

    def __init__(self, index, sub, size):
        self.index, self.sub, self.buf, self.size = index, sub, bytearray(), size


class CanopenSniffer:
    def __init__(self, history: int = 2000):
        self.nodes: dict[int, NodeInfo] = {}
        self.emcy_log: deque[EmcyEvent] = deque(maxlen=history)
        self.events: deque[tuple[float, str]] = deque(maxlen=history)     # boot-ups, NMT, state changes
        self.sync = CobStats()
        self.frames = 0                 # frames that were CANopen
        self._uploads: dict[int, _Upload] = {}
        self._waiting: dict[int, deque] = {}        # unconfirmed node -> recent (can_id, data, t)
        self._pending: dict[int, tuple[int, int]] = {}      # node -> (index, sub) a request asked for

    # ------------------------------------------------------------------
    def _node(self, n: int, t: float) -> NodeInfo:
        info = self.nodes.get(n)
        if info is None:
            info = self.nodes[n] = NodeInfo(n, first_seen=t)
        return info

    def feed(self, can_id: int, data: bytes, t: float) -> str | None:
        """Take one 11-bit frame. Returns its kind ("heartbeat", "tpdo1", "emcy" ...) or None if
        it isn't CANopen. Frames that only look like it by ID (OBD-II answers on 0x7E8 sit in the
        heartbeat range) are checked by content."""
        kind, n = cob.classify(can_id)
        data = bytes(data)
        if kind is None:
            return None
        confirms = kind == "heartbeat" and nmt.parse_heartbeat(data) is not None or \
            kind == "sdo_tx" and len(data) == 8 and sdo.parse(data, True) is not None
        if n and n not in self.nodes and kind not in ("nmt", "sync", "time", "lss"):
            if not confirms:
                q = self._waiting.setdefault(n, deque(maxlen=64))
                q.append((can_id, data, t))
                return None
            self._node(n, t)
            for args in self._waiting.pop(n, ()):
                self.feed(*args)
        if kind == "heartbeat":
            state = nmt.parse_heartbeat(data)
            if state is None:
                return None
            node = self._node(n, t)
            node.heartbeat.seen(t, data)
            if state == nmt.BOOT_UP:
                node.boots += 1
                node.heartbeat = CobStats()             # a restart: its period starts again
                self.events.append((t, f"node {n:02X} booted"))
            elif node.state is not None and state != node.state:
                self.events.append((t, f"node {n:02X} now {nmt.state_name(state)}"))
            if state != node.state:
                node.state_t = t
            node.state = state
        elif kind == "nmt":
            cmd = nmt.parse_command(data)
            if cmd is None:
                return None
            who = "every node" if cmd[1] == 0 else f"node {cmd[1]:02X}"
            self.events.append((t, f"NMT {nmt.COMMANDS[cmd[0]][0]} to {who}"))
            self.frames += 1
            return kind
        elif kind == "sync":
            if len(data) > 1:
                return None
            self.sync.seen(t, data)
            self.frames += 1
            return kind
        elif kind in ("time", "lss"):
            self.frames += 1
            return kind
        elif kind == "emcy":
            ev = emcy.decode(data)
            if ev is None or len(data) != 8:
                return None
            node = self._node(n, t)
            e = EmcyEvent(t, n, ev)
            self.emcy_log.append(e)
            node.register = ev.register
            if ev.reset:
                node.active.clear()
            else:
                node.active[ev.code] = e
        elif kind in ("sdo_tx", "sdo_rx"):
            if len(data) != 8:
                return None
            self._sdo(n, kind == "sdo_tx", data)
        node = self._node(n, t)
        node.frames += 1
        node.last_seen = t
        st = node.cobs.get(can_id)
        if st is None:
            st = node.cobs[can_id] = CobStats()
        st.seen(t, data)
        self.frames += 1
        return kind

    def _sdo(self, n: int, from_node: bool, data: bytes):
        f = sdo.parse(data, from_node)
        if f is None:
            return
        if not from_node:
            if f.kind == "upload":
                self._pending[n] = (f.index, f.sub)
            return
        node = self.nodes.get(n) or self._node(n, 0.0)
        if f.kind == "abort":
            node.aborts[(f.index, f.sub)] = f.abort_code
            self._uploads.pop(n, None)
        elif f.kind == "upload":
            if f.expedited:
                node.od[(f.index, f.sub)] = f.data
                node.aborts.pop((f.index, f.sub), None)
            else:
                self._uploads[n] = _Upload(f.index, f.sub, f.size)
        elif f.kind == "upload_segment":
            up = self._uploads.get(n)
            if up is None:
                return
            up.buf += f.data
            if f.last:
                node.od[(up.index, up.sub)] = bytes(up.buf[:up.size] if up.size else up.buf)
                node.aborts.pop((up.index, up.sub), None)
                del self._uploads[n]

    # ------------------------------------------------------------------
    def node_list(self) -> list[NodeInfo]:
        return [self.nodes[n].copy() for n in sorted(self.nodes)]

    def active_errors(self) -> list[EmcyEvent]:
        """Every node's EMCY codes that haven't been reset yet, oldest first."""
        out = [e for info in self.nodes.values() for e in info.active.values()]
        return sorted(out, key=lambda e: e.t)

    def emcy_since(self, t0: float) -> list[EmcyEvent]:
        return [e for e in self.emcy_log if e.t >= t0]
