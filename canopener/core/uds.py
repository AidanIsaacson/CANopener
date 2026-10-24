"""Drive a UDS (ISO 14229) session over a CANopener ``Session``.

Sends a UDS request to a module's physical request ID and reassembles the ISO-TP response from its
response ID (request + 8 by convention). Reads (DTCs with status, data identifiers), the standard
commands (clear diagnostic information, ECU reset) and a generic ``request`` for any service the
caller composes, single- or multi-frame. UDS requires transmitting, so it is unavailable in
listen-only. Front-ends confirm with the user before anything that changes a module.
"""

import queue
import threading
import time

from .. import uds
from ..can.frame import CanFrame
from ..obd.isotp import IsoTpReassembler, flow_control_frame, parse_flow_control, segment

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

    def _send_frames(self, frames: list[bytes]):
        """Send an ISO-TP message: the first frame, then (multi-frame) the consecutive frames as the
        module's flow control allows (block size, separation time, wait)."""
        if not self.session.send_frame(CanFrame(self.request_id, frames[0], extended=False)):
            raise uds.UdsError("could not send (listen-only, or the bus refused it)")
        rest = frames[1:]
        while rest:
            fc = None
            deadline = time.time() + self.timeout
            while fc is None and time.time() < deadline:
                try:
                    fc = parse_flow_control(self._rx.get(timeout=max(0.0, deadline - time.time())))
                except queue.Empty:
                    break
            if fc is None:
                raise uds.UdsError(f"module 0x{self.request_id:X} didn't send flow control")
            flag, block, sep = fc
            if flag == 1:                   # wait: another flow control follows
                continue
            if flag != 0:
                raise uds.UdsError("module refused the request (ISO-TP overflow)")
            n = block or len(rest)
            for fr in rest[:n]:
                if sep:
                    time.sleep(sep)
                self.session.send_frame(CanFrame(self.request_id, fr, extended=False))
            rest = rest[n:]

    def request(self, payload: bytes) -> bytes:
        """Send one UDS request and return the reassembled response bytes.

        Follows ISO-TP both ways: a long request goes out as First + Consecutive frames paced by
        the module's flow control, and a multi-frame reply gets our flow control. Keeps waiting
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
                self._send_frames(segment(bytes(payload), pad=0x00))
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

    def clear_dtcs(self, group: int = uds.ALL_DTC_GROUPS) -> None:
        """ClearDiagnosticInformation (0x14): clear the module's codes (all groups by default)."""
        uds.parse_response(0x14, self.request(uds.clear_diagnostic_information(group)))

    def ecu_reset(self, kind: int = uds.RESET_HARD) -> None:
        """ECUReset (0x11): restart the module (0x01 hard, 0x02 key off/on, 0x03 soft)."""
        uds.parse_response(0x11, self.request(uds.ecu_reset(kind)))

    def read_data_by_identifier(self, did: int) -> bytes:
        payload = uds.parse_response(0x22, self.request(uds.read_data_by_identifier(did)))
        return payload[2:] if len(payload) >= 2 else payload   # drop the echoed DID
