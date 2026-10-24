"""Drive an OBD-II (SAE J1979) session over a CANopener ``Session``: a generic scan tool.

The active counterpart to ``scan.py`` for J1939. It *sends* standard OBD requests and reassembles
the ECU replies:

* reads: live data (Mode 01), freeze frame (02), stored / pending / permanent codes (03/07/0A),
  readiness monitors (01 PID 01/41), on-board test results (06), vehicle info (09);
* commands: **clear codes** (Mode 04: clears codes, freeze frame and readiness, turns the MIL
  off) and **on-board tests** (Mode 08, e.g. the EVAP leak test). These change the vehicle's
  state, so front-ends confirm with the user before calling them.

A functional request is answered by every emissions ECU (engine, transmission, hybrid ...), so
reads return one answer per ECU, keyed by its response CAN ID. Live data is polled with physical
(single-ECU) requests, which return as soon as the ECU answers. 11-bit and 29-bit addressing are
both supported and picked automatically. Nothing works in listen-only mode.

The protocol decoding lives in the pure ``canopener.obd`` package; this module is the glue that
puts request frames on a live bus and collects the answers off the receive thread.
"""

import queue
import threading
import time

from .. import obd
from ..can.frame import CanFrame

DEFAULT_TIMEOUT = 0.5         # no answer by then: none coming (J1979 P2CAN is 50 ms)
SETTLE = 0.15               # after an answer, how long to wait for other ECUs before returning
PENDING_TIMEOUT = 5.0       # a 'response pending' ECU gets this long (P2* in ISO 15765-4)
# Live PIDs in a sensible display order for a quick read; the rest of the supported ones follow.
LIVE_PID_ORDER = [0x05, 0x0C, 0x0D, 0x11, 0x04, 0x0F, 0x10, 0x0B, 0x33, 0x2F, 0x46, 0x5C, 0x42, 0x5E, 0x1F]
DTC_MODES = (("stored", obd.MODE_STORED_DTCS), ("pending", obd.MODE_PENDING_DTCS),
             ("permanent", obd.MODE_PERMANENT_DTCS))
# Mode 01 PIDs that aren't live values (support masks, monitor status, freeze DTC).
NOT_LIVE = set(obd.SUPPORT_PIDS) | {0x01, 0x02, 0x41}

# What front-ends show before clear_dtcs(), so every one warns the same way.
CLEAR_WARNING = (
    "Clear trouble codes on every module (OBD-II Mode 04)?\n\n"
    "This also:\n"
    "  • erases the freeze frame and the on-board test results,\n"
    "  • resets every readiness monitor to incomplete: the vehicle will fail an emissions\n"
    "    inspection until it has been driven enough for them to run again,\n"
    "  • turns the check-engine light (MIL) off. It comes back if the fault is still there.\n\n"
    "Permanent codes stay until the module sees the fault is fixed.\n"
    "Do it with the engine off and the key on.")


class ObdClient:
    """One request at a time; answers collected from a Session listener."""

    def __init__(self, session, timeout: float = DEFAULT_TIMEOUT, addressing: obd.Addressing | None = None):
        self.session = session
        self.timeout = timeout
        self.addressing = addressing            # None: detect on first use
        self.ecu_names: dict[int, str] = {}     # response id -> "ECM" etc., from Mode 09 PID 0A
        self.known_ecus: set[int] = set()       # every ECU that has answered a functional request
        self._lock = threading.RLock()
        self._rx: queue.Queue = queue.Queue()
        self._active = False
        session.add_listener(self._on_frame)

    def close(self):
        self.session.remove_listener(self._on_frame)

    # receive thread: keep anything that could be an OBD answer, the transaction filters it
    def _on_frame(self, fr: CanFrame):
        if self._active and (fr.extended or obd.is_response_id(fr.can_id)):
            self._rx.put((fr.can_id, fr.extended, bytes(fr.data)))

    @property
    def available(self) -> bool:
        return self.session is not None and not self.session.listen_only

    # ------------------------------------------------------------------
    # transactions
    # ------------------------------------------------------------------
    def _send(self, can_id: int, extended: bool, data: bytes) -> bool:
        return self.session.send_frame(CanFrame(can_id, data, extended=extended))

    def _transact(self, addr: obd.Addressing, target: int | None, mode: int, pid: int | None, extra: bytes,
                  timeout: float, first_only: bool) -> dict[int, obd.ObdResponse]:
        """Send one request (functional, or physical to ``target``) and collect the answers."""
        with self._lock:
            reasm = obd.IsoTpReassembler()
            while not self._rx.empty():
                self._rx.get_nowait()
            self._active = True
            out: dict[int, obd.ObdResponse] = {}
            pending: set[int] = set()
            try:
                req_id = addr.functional_id if target is None else target
                if not self._send(req_id, addr.extended, obd.request_frame(mode, pid, extra=extra)):
                    return out
                last = time.time()          # when the last answer frame arrived
                deadline = last + timeout
                while True:
                    now = time.time()
                    if out and not pending:
                        deadline = min(deadline, last + SETTLE)
                    if now >= deadline:
                        break
                    try:
                        can_id, ext, data = self._rx.get(timeout=deadline - now)
                    except queue.Empty:
                        break
                    if not addr.is_response(can_id, ext):
                        continue
                    last = time.time()
                    done = reasm.feed(can_id, data)
                    for rx_id in reasm.needs_flow_control:
                        self._send(addr.request_id(rx_id), addr.extended, obd.flow_control_frame())
                    reasm.needs_flow_control.clear()
                    if done is None:
                        continue
                    resp = obd.decode(done[1])
                    if resp is None or resp.mode != mode:
                        continue
                    if resp.pending:                    # the real answer follows
                        pending.add(can_id)
                        deadline = max(deadline, time.time() + PENDING_TIMEOUT)
                        continue
                    if resp.ok and pid is not None and resp.pid not in (None, pid):
                        continue
                    pending.discard(can_id)
                    out[can_id] = resp
                    if first_only or (target is None and not pending and self.known_ecus
                                      and self.known_ecus <= out.keys()):
                        break               # everyone we know of has answered: no need to settle
                if target is None:
                    self.known_ecus |= out.keys()
                return out
            finally:
                self._active = False

    def _addr(self) -> obd.Addressing:
        if self.addressing is None:
            self.detect_addressing()
        return self.addressing or obd.ELEVEN_BIT

    def detect_addressing(self) -> obd.Addressing | None:
        """Try 11-bit, then 29-bit, with the 'supported PIDs' request every OBD-II ECU answers."""
        for addr in (obd.ELEVEN_BIT, obd.TWENTY_NINE_BIT):
            if self._transact(addr, None, 0x01, 0x00, b"", self.timeout, False):
                self.addressing = addr
                self.read_ecu_names()
                return addr
        return None

    def read_ecu_names(self) -> dict[int, str]:
        """Mode 09 PID 0A: 'ECM', 'TCM' ... for the modules that report a name (labels only)."""
        for ecu, r in self.query_all(obd.MODE_VEHICLE_INFO, 0x0A).items():
            if r.ok and r.text:
                self.ecu_names[ecu] = r.text.split("-")[0].strip() or r.text
        return dict(self.ecu_names)

    def query_all(self, mode: int, pid: int | None = None, extra: bytes = b"",
                  timeout: float | None = None) -> dict[int, obd.ObdResponse]:
        """Functional request: every ECU's answer, keyed by its response CAN ID."""
        return self._transact(self._addr(), None, mode, pid, extra, timeout or self.timeout, False)

    def query(self, mode: int, pid: int | None = None, extra: bytes = b"") -> obd.ObdResponse | None:
        """The first ECU's positive answer (lowest response ID), else its negative one, else None."""
        answers = self.query_all(mode, pid, extra)
        ok = [answers[k] for k in sorted(answers) if answers[k].ok]
        return ok[0] if ok else next((answers[k] for k in sorted(answers)), None)

    def query_ecu(self, ecu: int, mode: int, pid: int | None = None, extra: bytes = b"") -> obd.ObdResponse | None:
        """Physical request to one ECU (by its response ID); returns as soon as it answers."""
        addr = self._addr()
        got = self._transact(addr, addr.request_id(ecu), mode, pid, extra, self.timeout, True)
        return got.get(ecu)

    def ecu_label(self, ecu: int) -> str:
        name = self.ecu_names.get(ecu)
        addr = self.addressing or obd.ELEVEN_BIT
        return f"{name} ({addr.label(ecu)})" if name else f"ECU {addr.label(ecu)}"

    # ------------------------------------------------------------------
    # support masks
    # ------------------------------------------------------------------
    def supported_by_ecu(self, mode: int = obd.MODE_CURRENT_DATA, extra: bytes = b"") -> dict[int, list[int]]:
        """Walk the 0x00 / 0x20 / 0x40 ... bitmasks: what each ECU supports in ``mode``."""
        found: dict[int, set[int]] = {}
        base = 0x00
        while base <= 0xE0:
            answers = self.query_all(mode, base, extra)
            more = False
            for ecu, r in answers.items():
                if r.ok and r.supported:
                    found.setdefault(ecu, set()).update(p for p in r.supported if p % 0x20)
                    more |= base + 0x20 in r.supported
            if not more:
                break
            base += 0x20
        return {ecu: sorted(p) for ecu, p in sorted(found.items())}

    def supported_pids(self) -> list[int]:
        """Every Mode 01 PID any ECU reports."""
        return sorted({p for pids in self.supported_by_ecu().values() for p in pids})

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def live_pids(self) -> list[tuple[int, int]]:
        """(ecu, pid) for every live value the vehicle offers, display order first."""
        out = []
        for ecu, pids in self.supported_by_ecu().items():
            pids = [p for p in pids if p not in NOT_LIVE]
            pids.sort(key=lambda p: (LIVE_PID_ORDER.index(p) if p in LIVE_PID_ORDER else 99, p))
            out += [(ecu, p) for p in pids]
        return out

    def read_pid(self, ecu: int, pid: int) -> obd.ObdResponse | None:
        r = self.query_ecu(ecu, obd.MODE_CURRENT_DATA, pid)
        return r if r is not None and r.ok else None

    def read_live(self, pids: list[int] | None = None) -> list[obd.ObdResponse]:
        """One reading of each live PID (from the first ECU that has it)."""
        if pids is None:
            seen, todo = set(), []
            for ecu, pid in self.live_pids():
                if pid not in seen and pid in LIVE_PID_ORDER:
                    seen.add(pid)
                    todo.append((ecu, pid))
        else:
            todo = [(None, p) for p in pids]
        out = []
        for ecu, pid in todo:
            r = self.read_pid(ecu, pid) if ecu is not None else self.query(obd.MODE_CURRENT_DATA, pid)
            if r is not None and r.ok and r.values:
                out.append(r)
        return out

    def read_dtcs_by_ecu(self) -> dict[int, dict[str, list[str]]]:
        """Stored (Mode 03), pending (07) and permanent (0A) codes, per ECU that answered."""
        out: dict[int, dict[str, list[str]]] = {}
        for label, mode in DTC_MODES:
            for ecu, r in self.query_all(mode).items():
                out.setdefault(ecu, {k: [] for k, _ in DTC_MODES})[label] = r.dtcs if r.ok else []
        return dict(sorted(out.items()))

    def read_dtcs(self) -> dict[str, list[str]]:
        """Stored / pending / permanent codes from every ECU together."""
        out = {label: [] for label, _ in DTC_MODES}
        for per in self.read_dtcs_by_ecu().values():
            for label, codes in per.items():
                out[label] += [c for c in codes if c not in out[label]]
        return out

    def read_readiness(self, this_cycle: bool = False) -> dict[int, obd.MonitorStatus]:
        """Monitor status per ECU: since codes were cleared (PID 01) or this drive cycle (41)."""
        out = {}
        for ecu, r in self.query_all(obd.MODE_CURRENT_DATA, 0x41 if this_cycle else 0x01).items():
            if r.ok and len(r.raw) >= 6:
                out[ecu] = obd.decode_monitor_status(r.raw[2:6])
        return dict(sorted(out.items()))

    def read_freeze_frame(self, frame: int = 0) -> dict[int, list[obd.ObdResponse]]:
        """Freeze frame ``frame`` per ECU: the code that caused it, then every stored PID."""
        out = {}
        for ecu, pids in self.supported_by_ecu(obd.MODE_FREEZE_FRAME, bytes([frame])).items():
            rows = []
            for pid in [0x02] + [p for p in pids if p not in NOT_LIVE]:
                if pid == 0x02 and pid not in pids:
                    continue
                r = self.query_ecu(ecu, obd.MODE_FREEZE_FRAME, pid, bytes([frame]))
                if r is not None and r.ok and (r.values or r.text):
                    rows.append(r)
            if rows:
                out[ecu] = rows
        return out

    def read_test_results(self) -> list[tuple[int, obd.TestResult]]:
        """Mode 06 on-board monitor test results from every ECU: (ecu, result)."""
        out = []
        for ecu, mids in self.supported_by_ecu(obd.MODE_TEST_RESULTS).items():
            for mid in mids:
                r = self.query_ecu(ecu, obd.MODE_TEST_RESULTS, mid)
                if r is not None and r.ok:
                    out += [(ecu, t) for t in r.results]
        return out

    def read_info_by_ecu(self) -> dict[int, list[tuple[str, str]]]:
        """Mode 09 vehicle info per ECU: VIN, calibration IDs, CVNs, ECU name, in-use counters ..."""
        out: dict[int, list[tuple[str, str]]] = {}
        for pid in (0x02, 0x04, 0x06, 0x0A, 0x0D, 0x0F, 0x08, 0x0B):
            for ecu, r in self.query_all(obd.MODE_VEHICLE_INFO, pid).items():
                if not r.ok:
                    continue
                if pid == 0x0A and r.text:
                    self.ecu_names[ecu] = r.text.split("-")[0].strip() or r.text
                if r.values:
                    out.setdefault(ecu, []).extend((f"{r.name}: {n}", f"{v:g}") for n, v, _ in r.values)
                elif r.text:
                    out.setdefault(ecu, []).append((r.name, r.text))
        return dict(sorted(out.items()))

    def read_info(self) -> list[tuple[str, str]]:
        """Mode 09 vehicle info: VIN, calibration ID, CVN and ECU name (those the ECU answers)."""
        out = []
        for pid in (0x02, 0x04, 0x06, 0x0A):
            resp = self.query(obd.MODE_VEHICLE_INFO, pid)
            if resp is not None and resp.ok and resp.text:
                out.append((resp.name, resp.text))
        return out

    def read_vin(self) -> str | None:
        resp = self.query(obd.MODE_VEHICLE_INFO, 0x02)
        return resp.text if resp is not None and resp.ok and resp.text else None

    # ------------------------------------------------------------------
    # commands (change the vehicle's state: confirm with the user first)
    # ------------------------------------------------------------------
    def clear_dtcs(self) -> dict[int, tuple[bool, str]]:
        """Mode 04: clear stored / pending codes, freeze frame and Mode 06 results, reset the
        readiness monitors and turn the MIL off, on every ECU. Permanent codes stay until the
        ECU sees the fault is gone. Usually needs engine off, key on. Returns (ok, message) per ECU."""
        answers = self.query_all(obd.MODE_CLEAR_DTCS, timeout=max(self.timeout, 2.0))
        return {ecu: (r.ok, "cleared" if r.ok else r.error) for ecu, r in sorted(answers.items())}

    def control_tests(self) -> dict[int, list[int]]:
        """Mode 08 test IDs each ECU supports."""
        return self.supported_by_ecu(obd.MODE_CONTROL)

    def run_control_test(self, tid: int, data: bytes = b"") -> dict[int, tuple[bool, str]]:
        """Mode 08: start an on-board test (TID 0x01 = EVAP leak test). Returns (ok, message) per ECU."""
        answers = self.query_all(obd.MODE_CONTROL, tid, data, timeout=max(self.timeout, 2.0))
        return {ecu: (r.ok, "started" if r.ok else r.error) for ecu, r in sorted(answers.items())}
