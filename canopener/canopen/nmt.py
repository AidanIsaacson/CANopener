"""NMT (network management): node states as heartbeats report them, and the master's commands."""

BOOT_UP = 0x00
STOPPED = 0x04
OPERATIONAL = 0x05
PRE_OPERATIONAL = 0x7F

STATES = {BOOT_UP: "Boot-up", STOPPED: "Stopped", OPERATIONAL: "Operational",
          PRE_OPERATIONAL: "Pre-operational"}

START, STOP, ENTER_PREOP, RESET_NODE, RESET_COMM = 0x01, 0x02, 0x80, 0x81, 0x82
COMMANDS = {
    START: ("Start (operational)", "the node starts sending and acting on its process data (PDOs)"),
    STOP: ("Stop", "the node stops its process data and most communication; a motor controller "
                   "may drop out of drive"),
    ENTER_PREOP: ("Enter pre-operational", "the node stops its process data (PDOs) but still answers "
                                           "configuration requests"),
    RESET_NODE: ("Reset node", "the node restarts as if powered off and on (its settings reload)"),
    RESET_COMM: ("Reset communication", "the node restarts its CAN communication (its communication "
                                        "settings reload)"),
}
ALL_NODES = 0


def state_name(state: int | None) -> str:
    if state is None:
        return "unknown"
    return STATES.get(state & 0x7F, f"0x{state & 0x7F:02X}")


def parse_heartbeat(data: bytes) -> int | None:
    """The NMT state in a heartbeat / boot-up / node-guard answer, or None if it doesn't look
    like one (exactly one byte, a known state; bit 7 is the node-guarding toggle)."""
    if len(data) != 1 or (data[0] & 0x7F) not in STATES:
        return None
    return data[0] & 0x7F


def command_frame(command: int, node: int = ALL_NODES) -> tuple[int, bytes]:
    """(COB-ID, data) for an NMT command to ``node`` (0 = every node)."""
    if command not in COMMANDS:
        raise ValueError(f"unknown NMT command 0x{command:02X}")
    if not 0 <= node <= 127:
        raise ValueError("node must be 0-127")
    return 0x000, bytes([command, node])


def parse_command(data: bytes) -> tuple[int, int] | None:
    """(command, node) from an NMT frame seen on the bus."""
    if len(data) != 2 or data[0] not in COMMANDS:
        return None
    return data[0], data[1]
