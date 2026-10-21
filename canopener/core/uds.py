"""Drive a UDS (ISO 14229) session over a CANopener ``Session``.

Sends a UDS request to a module's physical request ID and reassembles the ISO-TP response from its
response ID (request + 8 by convention). Reads (DTCs with status, data identifiers) are the main
use; a generic ``request`` sends any service the caller composes. UDS requires transmitting, so it
is unavailable in listen-only. Nothing here clears or writes on its own — the caller decides what
to send, and the GUI routes any state-changing request through the confirm-first transmit gate.
"""

import queue
import threading
import time

from .. import uds
from ..can.frame import CanFrame
from ..obd.isotp import IsoTpReassembler, flow_control_frame

DEFAULT_REQUEST_ID = 0x7E0
RESPONSE_OFFSET = 0x08


class UdsClient:
    def __init__(self, session, request_id: int = DEFAULT_REQUEST_ID, response_id: int | None = None,
                 timeout: float = 1.5):
        self.session = session
        self.request_id = request_id
        self.response_id = response_id if response_id is not None else request_id + RESPONSE_OFFSET
        self.timeout = timeout
        self._lock = threading.Lock()
        self._rx: queue.Queue = queue.Queue()
        self._active = False
        session.add_listener(self._on_frame)

    def close(self):
        self.session.remove_listener(self._on_frame)

    @property
    def available(self) -> bool:
        return self.session is not None and not self.session.listen_only

    def _on_frame(self, fr: CanFrame):
        if self._active and not fr.extended and fr.can_id == self.response_id:
            self._rx.put(bytes(fr.data))

    def request(self, payload: bytes) -> bytes:
        """Send one UDS request (single-frame) and return the reassembled response bytes.

        Follows ISO-TP: sends a flow-control frame for a multi-frame reply, and keeps waiting
        through a 0x78 "response pending" negative response. Raises ``uds.UdsError`` on a (non
        pending) negative response or timeout.
        """
        with self._lock:
            if not self.available:
                raise uds.UdsError("UDS needs to transmit; not available in listen-only")
            reasm = IsoTpReassembler()
            while not self._rx.empty():
                self._rx.get_nowait()
            self._active = True
            try:
                frame = bytes([len(payload)]) + payload + b"\x00" * (7 - len(payload))
                if not self.session.send_frame(CanFrame(self.request_id, frame[:8], extended=False)):
                    raise uds.UdsError("could not send (listen-only, or the bus refused it)")
                deadline = time.time() + self.timeout
                while time.time() < deadline:
                    try:
                        data = self._rx.get(timeout=max(0.0, deadline - time.time()))
                    except queue.Empty:
                        break
                    done = reasm.feed(self.response_id, data)
                    for rx in reasm.needs_flow_control:
                        self.session.send_frame(CanFrame(rx - RESPONSE_OFFSET, flow_control_frame(),
                                                         extended=False))
                    reasm.needs_flow_control.clear()
                    if done is None:
                        continue
                    msg = done[1]
                    if msg and msg[0] == 0x7F and len(msg) > 2 and msg[2] == 0x78:
                        deadline = time.time() + self.timeout     # response pending: keep waiting
                        continue
                    return msg
                raise uds.UdsError(f"no answer from module 0x{self.request_id:X}")
            finally:
                self._active = False

    # convenience reads
    def read_dtcs(self, status_mask: int = 0xFF) -> list[tuple[str, int, list[str]]]:
        return uds.read_dtcs(self.request(uds.read_dtc_by_status_mask(status_mask)))

    def read_data_by_identifier(self, did: int) -> bytes:
        payload = uds.parse_response(0x22, self.request(uds.read_data_by_identifier(did)))
        return payload[2:] if len(payload) >= 2 else payload   # drop the echoed DID
