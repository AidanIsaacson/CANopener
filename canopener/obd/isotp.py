"""ISO 15765-2 (ISO-TP) transport for OBD-II over CAN, as used by SAE J1979.

This is *not* J1939 transport protocol (that lives in ``j1939/transport.py``). OBD-II uses the
ISO-TP framing on 11-bit CAN IDs:

* **Functional request** ID ``0x7DF`` — "any ECU, answer this".
* **Physical** request IDs ``0x7E0``-``0x7E7``; each ECU answers on request ID + ``0x08``, i.e.
  ``0x7E8``-``0x7EF``.

or, with 29-bit identifiers (ISO 15765-4, used by some newer vehicles), functional request
``0x18DB33F1`` and each ECU answering on ``0x18DAF1xx`` (xx = its address), physically addressed
at ``0x18DAxxF1``. ``Addressing`` captures the two schemes.

A message is carried as:

* **Single Frame** (first nibble ``0``): ``0x0L`` then ``L`` data bytes (``L`` <= 7).
* **First Frame** (``1``): ``0x1X XX`` = 12-bit length, then the first 6 bytes. The tester must
  reply with a **Flow Control** frame before the ECU sends the rest.
* **Consecutive Frame** (``2``): ``0x2N`` (N = sequence 1..15, wraps) then up to 7 bytes.
* **Flow Control** (``3``): ``0x30 BS STmin`` = clear-to-send.

Pure protocol: no CAN backend, no app imports. Feed it raw 8-byte payloads; it returns a
completed message's data bytes (the service response) when one finishes.
"""

from dataclasses import dataclass, field

FUNCTIONAL_REQUEST_ID = 0x7DF
PHYSICAL_REQUEST_IDS = range(0x7E0, 0x7E8)
RESPONSE_OFFSET = 0x08                      # response id = physical request id + 8
RESPONSE_IDS = range(0x7E8, 0x7F0)

SINGLE, FIRST, CONSECUTIVE, FLOW = 0, 1, 2, 3


def is_response_id(can_id: int) -> bool:
    return can_id in RESPONSE_IDS


@dataclass(frozen=True)
class Addressing:
    """11-bit or 29-bit OBD addressing (ISO 15765-4)."""

    name: str
    extended: bool
    functional_id: int

    def is_response(self, can_id: int, extended: bool) -> bool:
        if extended != self.extended:
            return False
        if self.extended:
            return (can_id & 0x1FFFFF00) == 0x18DAF100
        return can_id in RESPONSE_IDS

    def request_id(self, response_id: int) -> int:
        """The physical request ID of the ECU that answers on ``response_id`` (flow control goes here)."""
        if self.extended:
            return 0x18DA00F1 | ((response_id & 0xFF) << 8)
        return response_id - RESPONSE_OFFSET

    def label(self, response_id: int) -> str:
        return f"{response_id:08X}" if self.extended else f"{response_id:03X}"


ELEVEN_BIT = Addressing("11-bit", False, FUNCTIONAL_REQUEST_ID)
TWENTY_NINE_BIT = Addressing("29-bit", True, 0x18DB33F1)


def request_frame(service: int, pid: int | None = None, *, extra: bytes = b"") -> bytes:
    """A single-frame OBD request payload (padded to 8 bytes with 0x55, the J1979 filler).

    ``service`` is a mode (0x01 current data, 0x03 stored DTCs, 0x09 vehicle info, ...). ``pid``
    is included for the modes that take one (01, 02, 09); omit it for 03/04/07/0A.
    """
    body = bytes([service]) + (bytes([pid]) if pid is not None else b"") + extra
    return bytes([len(body)]) + body + b"\x55" * (7 - len(body))


def segment(payload: bytes, pad: int = 0x55) -> list[bytes]:
    """Split a message into ISO-TP frames: one Single Frame if it fits in 7 bytes, else a First
    Frame followed by Consecutive Frames (the sender waits for flow control after the first)."""
    if len(payload) <= 7:
        return [bytes([len(payload)]) + payload + bytes([pad]) * (7 - len(payload))]
    if len(payload) > 0xFFF:
        raise ValueError("ISO-TP message too long (max 4095 bytes)")
    frames = [bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]]
    rest, seq = payload[6:], 1
    while rest:
        chunk, rest = rest[:7], rest[7:]
        frames.append(bytes([0x20 | (seq & 0x0F)]) + chunk + bytes([pad]) * (7 - len(chunk)))
        seq += 1
    return frames


def parse_flow_control(data: bytes) -> tuple[int, int, float] | None:
    """(flag, block size, separation seconds) from a Flow Control frame; flag 0 = continue,
    1 = wait, 2 = overflow/abort. None if it isn't one."""
    if len(data) < 3 or data[0] >> 4 != FLOW:
        return None
    st = data[2]
    sep = st / 1000 if st <= 0x7F else (st - 0xF0) / 10000 if 0xF1 <= st <= 0xF9 else 0.127
    return data[0] & 0x0F, data[1], sep


def flow_control_frame(block_size: int = 0, st_min: int = 0) -> bytes:
    """A clear-to-send flow-control frame (send all, no separation time by default)."""
    return bytes([0x30, block_size & 0xFF, st_min & 0xFF]) + b"\x55" * 5


@dataclass
class _Pending:
    length: int
    data: bytearray = field(default_factory=bytearray)
    next_seq: int = 1


@dataclass
class IsoTpReassembler:
    """Reassembles ISO-TP messages per response ID. ``feed`` returns ``(rx_id, data)`` when a
    message completes, else ``None``. ``needs_flow_control`` lists IDs that just sent a First
    Frame and are waiting for our clear-to-send."""

    pending: dict[int, _Pending] = field(default_factory=dict)
    needs_flow_control: list[int] = field(default_factory=list)

    def feed(self, can_id: int, data: bytes) -> tuple[int, bytes] | None:
        if not data:
            return None
        pci = data[0] >> 4
        if pci == SINGLE:
            n = data[0] & 0x0F
            return (can_id, bytes(data[1:1 + n]))
        if pci == FIRST:
            length = ((data[0] & 0x0F) << 8) | data[1]
            p = _Pending(length)
            p.data += data[2:8]
            self.pending[can_id] = p
            self.needs_flow_control.append(can_id)
            return None
        if pci == CONSECUTIVE:
            p = self.pending.get(can_id)
            if p is None:
                return None
            if (data[0] & 0x0F) != (p.next_seq & 0x0F):
                self.pending.pop(can_id, None)      # out of order: give up on this message
                return None
            p.next_seq += 1
            p.data += data[1:8]
            if len(p.data) >= p.length:
                self.pending.pop(can_id, None)
                return (can_id, bytes(p.data[:p.length]))
        return None                                 # FLOW frames are the tester's job, not ours
