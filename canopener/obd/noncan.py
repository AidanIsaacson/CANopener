"""OBD-II on the older, non-CAN protocols (SAE J1850 PWM / VPW, ISO 9141-2, ISO 14230-4 KWP2000),
as an ELM327-style adapter reports it, turned into the CAN (ISO 15765-4) form the rest of
CANopener decodes.

SAE J1979 defines the same services on every protocol, but the answers are shaped differently
off CAN:

* each message has a 3-byte header (priority / format, target, **source = the module's address**,
  e.g. 0x10 the engine, 0x18 the transmission) and a check byte at the end;
* fault codes (Mode 03 / 07 / 0A) come **three per message with no count**, padded with 00 00, in
  as many messages as needed;
* vehicle info (Mode 09) comes **four data bytes per message**, each message numbered (the VIN is
  five messages: 3 zero bytes then the 17 characters), and the odd PIDs 01 / 03 / 05 / 07 / 09 give
  message counts (CAN has no such PIDs);
* one PID per Mode 01 request (no multi-PID requests);
* Mode 06 uses an older layout (test ID, component ID, value, one limit) that has nothing in common
  with the CAN one: ``normalize`` leaves it out (the backend doesn't ask for it).

``normalize`` merges one request's messages per module and rebuilds each module's answer the way
a CAN module would have sent it, so ``obd.decode`` and everything above it work unchanged.
``to_noncan`` does the opposite, for the simulated adapter. The two follow the same reading of
SAE J1979: **checked against the simulator only, not yet against a real non-CAN car** (see
docs/BLUETOOTH-OBD.md, "Gaps to fill").

Pure: no adapter, no app imports.
"""

# Synthetic 11-bit response IDs for the modules, so they line up with CAN cars: the engine
# (0x10) on 0x7E8, the transmission (0x18) on 0x7E9, others in the order they first answer.
KNOWN_IDS = {0x10: 0x7E8, 0x18: 0x7E9, 0x28: 0x7EA, 0x40: 0x7EB}
ADDRESS_NAMES = {0x10: "engine", 0x11: "engine 2", 0x18: "transmission", 0x19: "transmission 2",
                 0x28: "ABS / brakes", 0x40: "body"}
DTC_MODES = (0x43, 0x47, 0x4A)
INFO_MODE = 0x49


def parse_message(line: str, protocol: str = "") -> tuple[int, bytes] | None:
    """One reply line with headers on (``48 6B 10 41 0C 1A F8 C3``) -> (source address, data
    without header or check byte). None if it isn't a message. ``protocol`` is the ATDPN number;
    for KWP (4 / 5) the format byte's length is checked when it's given."""
    s = line.replace(" ", "").upper()
    if len(s) < 10 or len(s) % 2 or any(c not in "0123456789ABCDEF" for c in s):
        return None
    b = bytes.fromhex(s)
    if len(b) < 5:
        return None
    data = b[3:-1]
    if protocol in ("4", "5") and b[0] & 0x3F and (b[0] & 0x3F) != len(data):
        return None
    return b[2], data           # the third header byte is the sender


class IdMap:
    """Module address <-> synthetic response ID, stable for a connection."""

    def __init__(self):
        self.by_address: dict[int, int] = {}

    def response_id(self, address: int) -> int:
        if address not in self.by_address:
            used = set(self.by_address.values())
            want = KNOWN_IDS.get(address)
            if want is None or want in used:
                spare = [i for i in range(0x7E8, 0x7F0) if i not in used and i not in KNOWN_IDS.values()]
                want = (spare or [i for i in range(0x7E8, 0x7F0) if i not in used] or [0x7EF])[0]
            self.by_address[address] = want
        return self.by_address[address]

    def address(self, response_id: int) -> int | None:
        return next((a for a, i in self.by_address.items() if i == response_id), None)

    def label(self, response_id: int) -> str:
        a = self.address(response_id)
        if a is None:
            return ""
        name = ADDRESS_NAMES.get(a)
        return f"{name} (0x{a:02X})" if name else f"module 0x{a:02X}"


def normalize(request: bytes, messages: list[tuple[int, bytes]]) -> dict[int, bytes]:
    """One request's messages -> {module address: its answer in CAN form}."""
    by_src: dict[int, list[bytes]] = {}
    for src, data in messages:
        if data:
            by_src.setdefault(src, []).append(bytes(data))
    out = {}
    for src, msgs in by_src.items():
        body = _merge(request, msgs)
        if body is not None:
            out[src] = body
    return out


def _merge(request: bytes, msgs: list[bytes]) -> bytes | None:
    first = msgs[0]
    if first[0] == 0x7F:                        # negative answer: as is
        return first
    mode = first[0]
    if mode in DTC_MODES:
        codes = b"".join(m[1:] for m in msgs if m[0] == mode)
        pairs = [codes[i:i + 2] for i in range(0, len(codes) - 1, 2)]
        pairs = [p for p in pairs if p != b"\x00\x00"]
        return bytes([mode, len(pairs)]) + b"".join(pairs)
    if mode == INFO_MODE and len(first) >= 2:
        pid = first[1]
        if pid % 2 and pid < 0x0A:              # message counts: CAN has no such PIDs
            return None
        same = sorted((m for m in msgs if m[0] == mode and len(m) >= 3 and m[1] == pid), key=lambda m: m[2])
        if pid in (0x00, 0x20, 0x40, 0x60):     # supported-PID masks: drop the message number
            return bytes([mode, pid]) + same[0][3:7]
        data = b"".join(m[3:7] for m in same)
        if pid == 0x02:                         # VIN: 3 zero bytes, then 17 characters
            vin = data[-17:] if len(data) >= 17 else data.lstrip(b"\x00")
            return bytes([mode, pid, 1]) + vin
        size = {0x04: 16, 0x06: 4, 0x08: 2, 0x0A: 20, 0x0B: 2}.get(pid, 4)
        return bytes([mode, pid, max(1, len(data) // size)]) + data
    if mode == 0x46:                            # the older Mode 06 layout: not decoded
        return None
    return first                                # Mode 01 / 02 / 04 / 08: the same shape


def to_noncan(body: bytes) -> list[bytes]:
    """A CAN-form answer -> the messages a non-CAN module would send (for the simulator)."""
    if not body:
        return []
    mode = body[0]
    if mode in DTC_MODES and len(body) >= 2:
        codes = body[2:2 + 2 * body[1]]
        out = []
        for i in range(0, max(len(codes), 1), 6):
            chunk = codes[i:i + 6]
            out.append(bytes([mode]) + chunk.ljust(6, b"\x00"))
        return out
    if mode == INFO_MODE and len(body) >= 2:
        pid = body[1]
        if pid in (0x00, 0x20, 0x40, 0x60):
            return [bytes([mode, pid, 1]) + body[2:6]]
        data = body[3:]
        if pid == 0x02:
            data = b"\x00\x00\x00" + data
        chunks = [data[i:i + 4].ljust(4, b"\x00") for i in range(0, len(data), 4)]
        return [bytes([mode, pid, n + 1]) + c for n, c in enumerate(chunks)]
    return [body]

