"""Drive an OBD-II (SAE J1979) read session over a CANopener ``Session``.

This is the active-read counterpart to ``scan.py`` for J1939: it *sends* standard OBD requests
(functional ID 0x7DF) and reassembles the ECU replies. Like a J1939 scan it needs to transmit, so
it does nothing useful in listen-only mode. It never clears or writes anything — only Modes 01
(current data), 03/07/0A (DTCs) and 09 (vehicle info), all read services.

The protocol decoding lives in the pure ``canopener.obd`` package; this module is the glue that
puts request frames on a live bus and collects the answers off the receive thread.
"""

import queue
import threading
import time

from .. import obd
from ..can.frame import CanFrame

DEFAULT_TIMEOUT = 1.0
# Live PIDs we try in a quick scan, in a sensible display order. Only those the ECU reports as
# supported (and actually answers) show up.
LIVE_PID_ORDER = [0x05, 0x0C, 0x0D, 0x11, 0x04, 0x0F, 0x10, 0x0B, 0x33, 0x2F, 0x46, 0x5C, 0x42, 0x5E, 0x1F]


class ObdClient:
    """One request/response transaction at a time, collected from a Session listener."""

    def __init__(self, session, timeout: float = DEFAULT_TIMEOUT):
        self.session = session
        self.timeout = timeout
        self._lock = threading.Lock()
        self._rx: queue.Queue = queue.Queue()
        self._active = False
        session.add_listener(self._on_frame)

    def close(self):
        self.session.remove_listener(self._on_frame)

    # receive thread
    def _on_frame(self, fr: CanFrame):
        if self._active and not fr.extended and obd.is_response_id(fr.can_id):
            self._rx.put((fr.can_id, bytes(fr.data)))

    @property
    def available(self) -> bool:
        return self.session is not None and not self.session.listen_only

    def query(self, mode: int, pid: int | None = None) -> obd.ObdResponse | None:
        """Send one request and return the first decoded response, or None on timeout.

        Sends the functional request 0x7DF, feeds replies through an ISO-TP reassembler, and
        sends a flow-control frame when an ECU starts a multi-frame answer.
        """
        with self._lock:
            reasm = obd.IsoTpReassembler()
            while not self._rx.empty():
                self._rx.get_nowait()
            self._active = True
            try:
                if not self.session.send_frame(
                        CanFrame(obd.FUNCTIONAL_REQUEST_ID, obd.request_frame(mode, pid), extended=False)):
                    return None
                deadline = time.time() + self.timeout
                while time.time() < deadline:
                    try:
                        can_id, data = self._rx.get(timeout=max(0.0, deadline - time.time()))
                    except queue.Empty:
                        break
                    done = reasm.feed(can_id, data)
                    for rx_id in reasm.needs_flow_control:
                        self.session.send_frame(CanFrame(rx_id - obd.RESPONSE_OFFSET,
                                                         obd.flow_control_frame(), extended=False))
                    reasm.needs_flow_control.clear()
                    if done is not None:
                        resp = obd.decode(done[1])
                        if resp is not None and (pid is None or resp.pid in (None, pid)):
                            return resp
                return None
            finally:
                self._active = False

    def supported_pids(self) -> list[int]:
        """Walk the 0x00/0x20/0x40... support bitmasks and return every PID the ECU reports."""
        found: list[int] = []
        base = 0x00
        while base <= 0xC0:
            resp = self.query(0x01, base)
            if resp is None or not resp.supported:
                break
            found.extend(p for p in resp.supported if p != base + 0x20)
            if base + 0x20 in resp.supported:        # next range advertised
                base += 0x20
            else:
                break
        return found

    def read_live(self, pids: list[int] | None = None) -> list[obd.ObdResponse]:
        want = pids if pids is not None else [p for p in LIVE_PID_ORDER if p in set(self.supported_pids())]
        out = []
        for pid in want:
            resp = self.query(0x01, pid)
            if resp is not None and resp.ok and resp.value is not None:
                out.append(resp)
        return out

    def read_dtcs(self) -> dict[str, list[str]]:
        """Stored (Mode 03), pending (07) and permanent (0A) DTCs."""
        out = {}
        for label, mode in (("stored", obd.MODE_STORED_DTCS), ("pending", obd.MODE_PENDING_DTCS),
                            ("permanent", obd.MODE_PERMANENT_DTCS)):
            resp = self.query(mode)
            out[label] = resp.dtcs if resp is not None and resp.ok else []
        return out

    def read_vin(self) -> str | None:
        resp = self.query(obd.MODE_VEHICLE_INFO, 0x02)
        return resp.text if resp is not None and resp.ok and resp.text else None
