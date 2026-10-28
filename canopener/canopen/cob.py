"""CANopen (CiA 301) 11-bit COB-IDs: which kind of message an identifier carries, and for which node.

These are the predefined connection set's defaults. A manufacturer may move PDOs (and rarely the
others) to other identifiers, so a message that doesn't fit is reported as unknown rather than
guessed at, and the sniffer also checks the payload (a heartbeat is one byte, for instance).
"""

NMT = 0x000
SYNC = 0x080            # SYNC is 0x080 itself; 0x081-0x0FF are EMCY from nodes 1-127
EMCY = 0x080
TIME = 0x100
TPDO = (0x180, 0x280, 0x380, 0x480)       # node -> network (what a controller broadcasts)
RPDO = (0x200, 0x300, 0x400, 0x500)       # network -> node
SDO_TX = 0x580          # server (the node) -> client (us / the master): answers
SDO_RX = 0x600          # client -> server (the node): requests
HEARTBEAT = 0x700       # also boot-up and node-guarding answers
LSS_SLAVE = 0x7E4       # layer setting services (CiA 305)
LSS_MASTER = 0x7E5
NMT_GUARD_RTR = HEARTBEAT

KIND_LABELS = {
    "nmt": "NMT command", "sync": "SYNC", "time": "TIME", "emcy": "EMCY (emergency)",
    "tpdo1": "TPDO1", "tpdo2": "TPDO2", "tpdo3": "TPDO3", "tpdo4": "TPDO4",
    "rpdo1": "RPDO1", "rpdo2": "RPDO2", "rpdo3": "RPDO3", "rpdo4": "RPDO4",
    "sdo_tx": "SDO answer", "sdo_rx": "SDO request", "heartbeat": "Heartbeat", "lss": "LSS",
}


def classify(can_id: int) -> tuple[str | None, int]:
    """(kind, node) for an 11-bit identifier, e.g. 0x185 -> ("tpdo1", 5). Node 0 for
    broadcast messages (NMT, SYNC, TIME, LSS). (None, 0) if it isn't a predefined CANopen ID."""
    if not 0 <= can_id <= 0x7FF:
        return None, 0
    if can_id == NMT:
        return "nmt", 0
    if can_id == SYNC:
        return "sync", 0
    if can_id == TIME:
        return "time", 0
    if can_id in (LSS_SLAVE, LSS_MASTER):
        return "lss", 0
    base, node = can_id & 0x780, can_id & 0x7F
    if node == 0:
        return None, 0
    if base == EMCY:
        return "emcy", node
    for i, b in enumerate(TPDO):
        if base == b:
            return f"tpdo{i + 1}", node
    for i, b in enumerate(RPDO):
        if base == b:
            return f"rpdo{i + 1}", node
    if base == SDO_TX:
        return "sdo_tx", node
    if base == SDO_RX:
        return "sdo_rx", node
    if base == HEARTBEAT:
        return "heartbeat", node
    return None, 0


def label(can_id: int) -> str:
    """'TPDO1 node 05', 'SYNC', or '' for an identifier outside the predefined set."""
    kind, node = classify(can_id)
    if kind is None:
        return ""
    text = KIND_LABELS[kind]
    return f"{text} node {node:02X}" if node else text


def node_of(can_id: int) -> int | None:
    kind, node = classify(can_id)
    return node if kind is not None and node else None
