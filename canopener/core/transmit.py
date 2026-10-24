"""A generic, confirm-first transmit path for standard public CAN protocols.

Most of CANopener is receive-only. This module adds a deliberately separate, opt-in way to *send*
standard frames (a J1939 Request, or a raw CAN frame the user composes), with three safeguards
baked in:

* **Off by default.** A ``Transmitter`` starts disabled; nothing is sent until transmit mode is
  explicitly turned on.
* **Never in listen-only.** Sending is refused whenever the session is listen-only (or offline).
* **Logged.** Every attempt is recorded in ``log`` with its outcome.

The *confirmation* step (showing the exact CAN ID, bytes and destination before sending) is the UI's
job — see ``gui/transmit_dialog.py``. The actual byte-level writing is still ``Session.send_frame``.

Only generic, publicly documented building blocks live here. The standard OBD-II / UDS commands
(clear codes, on-board tests, module reset) have their own confirm-first paths in ``core/obd.py`` /
``core/uds.py``. OEM or proprietary commands are NOT defined in this repository; a separately
installed plugin registers those as ``TransmitAction``s through ``register_action`` and they then
appear in the same confirm-first UI.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..can.frame import CanFrame
from ..j1939.constants import GLOBAL_SA, PGN_REQUEST, pgn_label
from ..j1939.ids import build_id


class TransmitDisabled(Exception):
    """Raised when a send is attempted while transmit mode is off."""


@dataclass
class TxRecord:
    t: float
    frame: CanFrame
    ok: bool
    label: str = ""


@dataclass
class Transmitter:
    """Gate + log around ``Session.send_frame``. One per live session."""

    session: object
    enabled: bool = False
    log: list[TxRecord] = field(default_factory=list)

    @property
    def can_send(self) -> bool:
        return (self.enabled and self.session is not None
                and not getattr(self.session, "listen_only", True))

    def send(self, frame: CanFrame, label: str = "") -> bool:
        if not self.enabled:
            raise TransmitDisabled("transmit mode is off")
        ok = bool(self.session is not None and self.session.send_frame(frame))
        self.log.append(TxRecord(time.time(), frame, ok, label))
        return ok


# --- standard, public frame builders -------------------------------------------------

def j1939_request_frame(pgn: int, da: int = GLOBAL_SA, sa: int = 0xF9, priority: int = 6) -> CanFrame:
    """A J1939 Request (PGN 0xEA00) asking ``da`` to broadcast ``pgn``. This is the same standard
    request the app already sends for Read Stored / Full Scan, surfaced as a composable action."""
    data = bytes([pgn & 0xFF, (pgn >> 8) & 0xFF, (pgn >> 16) & 0xFF])
    return CanFrame(build_id(PGN_REQUEST, sa, da, priority=priority), data, extended=True)


def raw_frame(can_id: int, data: bytes, extended: bool = True) -> CanFrame:
    """A raw CAN frame the user composed. Standard CAN: 11-bit id <= 0x7FF, 29-bit <= 0x1FFFFFFF,
    up to 8 data bytes."""
    if not 0 <= can_id <= (0x1FFFFFFF if extended else 0x7FF):
        raise ValueError("CAN id out of range")
    if len(data) > 8:
        raise ValueError("a CAN frame carries at most 8 data bytes")
    return CanFrame(can_id, bytes(data), extended=extended)


def describe(frame: CanFrame) -> str:
    """Human-readable one-liner for a confirmation dialog / log."""
    kind = "29-bit" if frame.extended else "11-bit"
    hexd = frame.data.hex(" ").upper() or "(no data)"
    extra = ""
    if frame.extended:
        from ..j1939.ids import parse_id
        jid = parse_id(frame.can_id)
        dest = "all ECUs" if jid.da == GLOBAL_SA else f"SA {jid.da:02X}"
        extra = f"  [PGN {jid.pgn:04X} {pgn_label(jid.pgn)}, from SA {jid.sa:02X} to {dest}]"
    return f"{frame.id_str()} ({kind})  {hexd}{extra}"


# --- action registry (standard actions + plugin-contributed ones) --------------------

@dataclass
class TransmitAction:
    """A named transmit action for the UI. ``fields`` are (key, label, default) text inputs; ``build``
    turns a dict of those values into the frame(s) to send after the user confirms."""

    name: str
    description: str
    fields: list[tuple[str, str, str]]
    build: Callable[[dict], list[CanFrame]]


_ACTIONS: list[TransmitAction] = []


def register_action(action: TransmitAction):
    _ACTIONS.append(action)


def actions() -> list[TransmitAction]:
    return list(_ACTIONS)


def _build_request(v: dict) -> list[CanFrame]:
    pgn = int(v["pgn"], 16)
    da = int(v["da"], 16) if v.get("da") else GLOBAL_SA
    return [j1939_request_frame(pgn, da)]


def _build_raw(v: dict) -> list[CanFrame]:
    can_id = int(v["id"], 16)
    data = bytes.fromhex(v.get("data", "").replace(" ", ""))
    extended = can_id > 0x7FF or len(f"{can_id:x}") > 3
    return [raw_frame(can_id, data, extended=extended)]


def _build_uds(v: dict) -> list[CanFrame]:
    """A single-frame UDS (ISO 14229) request, ISO-TP wrapped. The user supplies the service +
    payload bytes from the public standard (e.g. '22 F1 90' = ReadDataByIdentifier 0xF190); OEM
    routine/DID/IO-control identifiers are theirs to provide. Multi-frame requests (>7 payload
    bytes) aren't built here — use the UDS client for those."""
    can_id = int(v["id"], 16)
    payload = bytes.fromhex(v.get("request", "").replace(" ", ""))
    if not 1 <= len(payload) <= 7:
        raise ValueError("UDS request payload must be 1-7 bytes (single frame)")
    frame = bytes([len(payload)]) + payload
    extended = can_id > 0x7FF
    return [raw_frame(can_id, frame.ljust(8, b"\x00"), extended=extended)]


def register_standard_actions():
    """Register the built-in, public-protocol actions once."""
    if _ACTIONS:
        return
    register_action(TransmitAction(
        "J1939 Request (PGN 0xEA00)",
        "Ask an ECU (or everyone) to broadcast a PGN. The standard, read-only J1939 request.",
        [("pgn", "PGN (hex, e.g. FEEB)", "FEEB"), ("da", "To address (hex, blank = all)", "")],
        _build_request))
    register_action(TransmitAction(
        "Raw CAN frame",
        "Send a frame you compose yourself (standard CAN).",
        [("id", "CAN ID (hex)", "18EAFFFE"), ("data", "Data bytes (hex)", "EB FE 00")],
        _build_raw))
    register_action(TransmitAction(
        "UDS request (ISO 14229, single frame)",
        "Send a UDS request to a module (ISO-TP wrapped). Service + payload are standard ISO 14229; "
        "OEM routine/DID identifiers are yours to supply. Example reads VIN (0x22 0xF190).",
        [("id", "Module request ID (hex, e.g. 7E0)", "7E0"),
         ("request", "Service + payload (hex)", "22 F1 90")],
        _build_uds))
