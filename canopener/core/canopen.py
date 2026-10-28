"""CANopen on a CANopener ``Session``: reading nodes over SDO, the confirm-first commands, and the
glue that lets the J1939 screens show CANopen too (sources, fault codes, node info).

Reads (SDO upload) are like J1939 Requests: they only ask, and they're refused in listen-only.
The commands that change a node are an NMT command (start / stop / reset) and clearing a node's
error history (writing 0 to object 0x1003 sub 0, the CiA 301 way); front-ends say what each
changes and ask first. The ``Session`` itself follows all CANopen traffic passively
(``session.canopen``), including the answers to these reads, so whatever is read here also shows
up in the node's info without being passed around.
"""

import queue
import threading
import time
from dataclasses import dataclass

from .. import canopen
from ..can.frame import CanFrame
from ..canopen import cob, emcy, nmt, objects, sdo
from ..canopen.sdo import SdoAbort, SdoError
from ..j1939.addresses import sa_name

__all__ = ["CanopenClient", "SdoAbort", "SdoError", "CanopenCode", "codes", "source_name", "node_src", "id_src"]

# Front-ends key rows by a "source" int: a J1939 address (0-255), a CANopen node, or an 11-bit ID.
NODE_SRC = 0x10000
ID_SRC = 0x20000


def node_src(node: int) -> int:
    return NODE_SRC + node


def id_src(can_id: int) -> int:
    """The source for an 11-bit ID: its node if it's a predefined CANopen ID, else the ID."""
    node = cob.node_of(can_id)
    return node_src(node) if node else ID_SRC + can_id


def is_node(src: int) -> bool:
    return NODE_SRC <= src < ID_SRC


def source_name(src: int, with_hex: bool = True) -> str:
    """'27 Machine controller', 'Node 05 Traction controller' or 'ID 1A3'."""
    if src >= ID_SRC:
        return f"ID {src - ID_SRC:03X}"
    if src >= NODE_SRC:
        return canopen.node_name(src - NODE_SRC, with_hex)
    return sa_name(src, with_hex=with_hex)


def warning_nmt(command: int, node: int) -> str:
    """The confirmation text for an NMT command."""
    label, what = nmt.COMMANDS[command]
    who = "EVERY node on the bus" if node == nmt.ALL_NODES else canopen.node_name(node)
    return (f"Send NMT '{label}' to {who}?\n\nThis changes the machine: {what}. Only do this with the "
            "machine safe (parked, wheels chocked, nothing moving), because a controller that stops or "
            "restarts can drop out of drive or release outputs.")


def warning_clear(node: int) -> str:
    return (f"Clear the stored error history of {canopen.node_name(node)}?\n\nThis writes 0 to its "
            "pre-defined error field (object 1003 sub 0), the standard CANopen way. The history is "
            "gone afterwards: note or save the codes first. Active errors come back if their cause "
            "is still there.")


class CanopenClient:
    """SDO reads (expedited and segmented), error history, node discovery, and the two
    confirm-first commands. One request at a time; answers come from a Session listener."""

    def __init__(self, session, timeout: float = 1.0):
        self.session = session
        self.timeout = timeout
        self._lock = threading.Lock()
        self._rx: queue.Queue = queue.Queue()
        self._want: set[int] = set()
        session.add_listener(self._on_frame)

    def close(self):
        self.session.remove_listener(self._on_frame)

    @property
    def available(self) -> bool:
        return self.session is not None and not self.session.listen_only

    def _on_frame(self, fr: CanFrame):
        if not fr.extended and fr.can_id in self._want:
            self._rx.put((fr.can_id, bytes(fr.data)))

    def _send(self, can_id: int, data: bytes):
        if not self.session.send_frame(CanFrame(can_id, data, extended=False)):
            raise SdoError("could not send (listen-only, or the bus refused it)")

    def _answer(self, node: int, deadline: float) -> sdo.SdoFrame:
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise SdoError(f"no answer from node {node:02X}")
            try:
                can_id, data = self._rx.get(timeout=left)
            except queue.Empty:
                continue
            if can_id == cob.SDO_TX + node:
                f = sdo.parse(data, from_node=True)
                if f is not None:
                    return f

    def _begin(self, node: int):
        if not self.available:
            raise SdoError("CANopen reads need to transmit; not available in listen-only")
        if not 1 <= node <= 127:
            raise SdoError("node must be 1-127")
        while not self._rx.empty():
            self._rx.get_nowait()
        self._want = {cob.SDO_TX + node}

    def upload(self, node: int, index: int, sub: int = 0) -> bytes:
        """Read object ``index`` / ``sub`` from ``node``. Raises ``SdoAbort`` if the node refuses,
        ``SdoError`` on timeout or a protocol mix-up."""
        with self._lock:
            self._begin(node)
            try:
                self._send(cob.SDO_RX + node, sdo.upload_request(index, sub))
                f = self._answer(node, time.time() + self.timeout)
                if f.kind == "abort":
                    raise SdoAbort(f.abort_code, index, sub)
                if f.kind != "upload" or (f.index, f.sub) != (index, sub):
                    raise SdoError(f"unexpected answer to {index:04X}.{sub}")
                if f.expedited:
                    return f.data
                buf, toggle = bytearray(), 0
                for _ in range(4096):          # 28 KB: far beyond any object we read
                    self._send(cob.SDO_RX + node, sdo.segment_request(toggle))
                    seg = self._answer(node, time.time() + self.timeout)
                    if seg.kind == "abort":
                        raise SdoAbort(seg.abort_code, index, sub)
                    if seg.kind != "upload_segment" or seg.toggle != toggle:
                        self._send(cob.SDO_RX + node, sdo.abort_request(index, sub, 0x05030000))
                        raise SdoError(f"segment out of step reading {index:04X}.{sub}")
                    buf += seg.data
                    if seg.last:
                        return bytes(buf[:f.size] if f.size else buf)
                    toggle ^= 1
                raise SdoError(f"{index:04X}.{sub} is too long")
            finally:
                self._want = set()

    def _download_expedited(self, node: int, index: int, sub: int, value: bytes):
        """Write up to 4 bytes. Used only by ``clear_error_history`` (behind a confirmation)."""
        with self._lock:
            self._begin(node)
            try:
                n = 4 - len(value)
                cmd = bytes([sdo.CCS_INITIATE_DOWNLOAD << 5 | n << 2 | 0x03])
                self._send(cob.SDO_RX + node, cmd + bytes([index & 0xFF, index >> 8, sub]) + value.ljust(4, b"\0"))
                f = self._answer(node, time.time() + self.timeout)
                if f.kind == "abort":
                    raise SdoAbort(f.abort_code, index, sub)
                if f.kind != "download":
                    raise SdoError(f"unexpected answer writing {index:04X}.{sub}")
            finally:
                self._want = set()

    # --- reads ---------------------------------------------------------------------------------
    INFO = [(objects.DEVICE_TYPE, 0), (objects.DEVICE_NAME, 0), (objects.HARDWARE_VERSION, 0),
            (objects.SOFTWARE_VERSION, 0), (objects.IDENTITY, 1), (objects.IDENTITY, 2),
            (objects.IDENTITY, 3), (objects.IDENTITY, 4), (objects.ERROR_REGISTER, 0),
            (objects.HEARTBEAT_TIME, 0)]

    def node_info(self, node: int) -> list[tuple[str, str]]:
        """(label, text) for the standard identity objects the node has; ones it refuses are
        left out. Raises ``SdoError`` only if it doesn't answer at all."""
        out, answered = [], False
        for index, sub in self.INFO:
            try:
                v = self.upload(node, index, sub)
            except SdoAbort:
                answered = True
                continue
            except SdoError:
                if not answered:
                    raise
                continue
            answered = True
            out.append((objects.name(index, sub), objects.show(index, sub, v)))
        return out

    def error_history(self, node: int) -> list[int]:
        """The node's stored errors (object 0x1003), newest first, as 32-bit values (low 16 bits
        = the EMCY error code, high 16 = manufacturer extra). Empty if it has no history object."""
        try:
            count = self.upload(node, objects.ERROR_HISTORY, 0)
        except SdoAbort:
            return []
        out = []
        for sub in range(1, min(count[0] if count else 0, 254) + 1):
            try:
                out.append(objects.as_uint(self.upload(node, objects.ERROR_HISTORY, sub)))
            except SdoAbort:
                break
        return out

    def find_nodes(self, listen: float = 0.6) -> list[int]:
        """Ask every node ID (1-127) for its device type, to find nodes that don't send heartbeats.
        Read-only: 127 SDO reads, paced. Returns the node IDs that answered."""
        if not self.available:
            raise SdoError("finding nodes needs to transmit; not available in listen-only")
        with self._lock:
            while not self._rx.empty():
                self._rx.get_nowait()
            self._want = {cob.SDO_TX + n for n in range(1, 128)}
            try:
                for n in range(1, 128):
                    self._send(cob.SDO_RX + n, sdo.upload_request(objects.DEVICE_TYPE, 0))
                    time.sleep(0.002)
                found, deadline = set(), time.time() + listen
                while time.time() < deadline:
                    try:
                        can_id, _ = self._rx.get(timeout=max(0.0, deadline - time.time()))
                    except queue.Empty:
                        break
                    found.add(can_id - cob.SDO_TX)
                return sorted(found)
            finally:
                self._want = set()

    # --- commands (front-ends confirm first) ---------------------------------------------------
    def nmt(self, command: int, node: int = nmt.ALL_NODES):
        """Send an NMT command. Changes the machine: confirm first (``warning_nmt``)."""
        if not self.available:
            raise SdoError("NMT commands need to transmit; not available in listen-only")
        can_id, data = nmt.command_frame(command, node)
        self._send(can_id, data)

    def clear_error_history(self, node: int):
        """Write 0 to 0x1003 sub 0. Changes the machine: confirm first (``warning_clear``)."""
        self._download_expedited(node, objects.ERROR_HISTORY, 0, b"\x00")


# --- fault codes, shaped for the Fault Codes screen ------------------------------------------------

@dataclass
class CanopenCode:
    """A CANopen error as a fault-code row: an active EMCY, or an entry of the stored history."""
    status: str                 # "Active", "Stored" or "Cleared" (an EMCY since reset)
    node: int
    code: int                   # EMCY error code
    register: int | None = None
    extra: bytes = b""          # manufacturer bytes (EMCY) / high 16 bits (history)
    first: float = 0.0
    last: float = 0.0
    seen: int = 1

    @property
    def src(self) -> int:
        return node_src(self.node)

    @property
    def text(self) -> str:
        return emcy.describe(self.code)

    @property
    def register_text(self) -> str:
        return "" if self.register is None else f"{self.register:02X} ({emcy.register_flags(self.register)})"

    @property
    def extra_text(self) -> str:
        return self.extra.hex(" ").upper()


def codes(session, since: float = 0.0, history: bool = False) -> list[CanopenCode]:
    """CANopen fault codes from what the session has followed: active EMCY errors, stored ones
    (from error histories read over SDO, by us or another tool) and, with ``history``, every
    EMCY seen since ``since`` that has been reset since."""
    out: list[CanopenCode] = []
    with session.lock:
        sn = session.canopen
        active = sn.active_errors()
        log = list(sn.emcy_log)
        stored = {n: [(sub, v) for (i, sub), v in info.od.items() if i == objects.ERROR_HISTORY and sub > 0]
                  for n, info in sn.nodes.items()}
        counts = {n: info.od.get((objects.ERROR_HISTORY, 0)) for n, info in sn.nodes.items()}
    seen: dict[tuple[int, int], list] = {}
    for e in log:
        if not e.emcy.reset:
            seen.setdefault((e.node, e.emcy.code), []).append(e)
    for e in active:
        evs = seen.get((e.node, e.emcy.code), [e])
        out.append(CanopenCode("Active", e.node, e.emcy.code, e.emcy.register, e.emcy.data,
                               evs[0].t, e.t, len(evs)))
    act = {(c.node, c.code) for c in out}
    for n, entries in stored.items():
        count = counts.get(n)
        limit = count[0] if count else len(entries)
        for _sub, v in sorted(entries)[:limit]:
            val = objects.as_uint(v)
            out.append(CanopenCode("Stored", n, val & 0xFFFF, None, (val >> 16).to_bytes(2, "little")))
    if history:
        for (n, code), evs in seen.items():
            if (n, code) not in act and evs[-1].t >= since:
                out.append(CanopenCode("Cleared", n, code, evs[-1].emcy.register, evs[-1].emcy.data,
                                       evs[0].t, evs[-1].t, len(evs)))
    order = {"Active": 0, "Stored": 1, "Cleared": 2}
    out.sort(key=lambda c: (order[c.status], c.node, c.code))
    return out


def node_summary(info) -> str:
    """'Operational · CiA 402 drive · heartbeat 500 ms' for a ``canopen.NodeInfo``."""
    parts = [info.state_text]
    dt = info.value(objects.DEVICE_TYPE)
    if dt is not None and len(dt) >= 4:
        parts.append(objects.device_profile(objects.as_uint(dt)))
    name = info.value(objects.DEVICE_NAME)
    if name:
        parts.append(objects.as_text(name))
    if info.heartbeat_ms:
        parts.append(f"heartbeat {info.heartbeat_ms} ms")
    if info.boots > 1:
        parts.append(f"restarted {info.boots - 1}x")
    if info.active:
        parts.append(f"{len(info.active)} active error(s)")
    return " · ".join(parts)


def node_details(info) -> list[tuple[str, str]]:
    """Everything known about a node, for Machine Info and reports."""
    out = [("State", info.state_text)]
    if info.heartbeat_ms:
        out.append(("Heartbeat", f"every {info.heartbeat_ms} ms"))
    for (index, sub), v in sorted(info.od.items()):
        if index == objects.ERROR_HISTORY:
            continue
        out.append((objects.name(index, sub), objects.show(index, sub, v)))
    hidden = ("heartbeat", "sdo_tx", "sdo_rx")     # shown elsewhere / just our own reads
    pdos = [(cid, st) for cid, st in sorted(info.cobs.items()) if cob.classify(cid)[0] not in hidden]
    for cid, st in pdos:
        rate = f", every {st.period * 1000:.0f} ms" if st.period else ""
        out.append((f"{cid:03X} {cob.KIND_LABELS.get(cob.classify(cid)[0], '')}", f"{st.count} frame(s){rate}"))
    return out
