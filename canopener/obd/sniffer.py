"""Follow OBD-II traffic in a stream of CAN frames: whatever a tester asks and whatever the ECUs
answer, live or from a recorded log.

This is the OBD-II counterpart of feeding J1939 frames into a ``Session``: one decode path for the
live view, a decoded log file and the Log tool's capture report. It needs no request of its own,
so it also follows another scan tool's session when CANopener is only listening.

``feed`` takes one frame (id, 29-bit flag, data, time) and returns the answers it completed. The
sniffer keeps the latest of everything: live values, codes per module and kind, readiness,
freeze frame, vehicle info, Mode 06 results, plus a timeline of events (codes read, cleared,
tests started). Pure protocol: no app imports.
"""

from dataclasses import dataclass, field

from .decode import (
    CONTROL_TESTS,
    MODE_CLEAR_DTCS,
    MODE_CONTROL,
    MODE_CURRENT_DATA,
    MODE_FREEZE_FRAME,
    MODE_PENDING_DTCS,
    MODE_PERMANENT_DTCS,
    MODE_STORED_DTCS,
    MODE_TEST_RESULTS,
    MODE_VEHICLE_INFO,
    ObdResponse,
    decode,
)
from .isotp import ELEVEN_BIT, FUNCTIONAL_REQUEST_ID, TWENTY_NINE_BIT, IsoTpReassembler
from .pids import SUPPORT_PIDS
from .readiness import MonitorStatus, decode_monitor_status

DTC_KINDS = {MODE_STORED_DTCS: "stored", MODE_PENDING_DTCS: "pending", MODE_PERMANENT_DTCS: "permanent"}


@dataclass
class Reading:
    t: float
    value: object               # float, or str for text PIDs
    unit: str


@dataclass
class ObdSniffer:
    values: dict = field(default_factory=dict)        # (ecu, pid, value name) -> Reading
    dtcs: dict = field(default_factory=dict)          # ecu -> {"stored": [...], "pending": ..., ...}
    readiness: dict = field(default_factory=dict)     # ecu -> MonitorStatus (PID 01)
    freeze: dict = field(default_factory=dict)        # ecu -> {pid: ObdResponse}
    info: dict = field(default_factory=dict)          # ecu -> {name: text}
    results: dict = field(default_factory=dict)       # (ecu, mid, tid) -> TestResult
    names: dict = field(default_factory=dict)         # ecu -> "ECM" (Mode 09 PID 0A)
    events: list = field(default_factory=list)        # (t, text)
    extended: bool | None = None                      # 29-bit seen (None: no OBD traffic yet)
    last_answer_t: float = 0.0                        # when a module last answered (0: never)

    def __post_init__(self):
        self._reasm = IsoTpReassembler()

    # ------------------------------------------------------------------
    def label(self, ecu: int) -> str:
        addr = TWENTY_NINE_BIT if ecu > 0x7FF else ELEVEN_BIT
        name = self.names.get(ecu)
        return f"{name} ({addr.label(ecu)})" if name else f"ECU {addr.label(ecu)}"

    @staticmethod
    def is_request(can_id: int, extended: bool) -> bool:
        if extended:
            return can_id == TWENTY_NINE_BIT.functional_id or (can_id & 0x1FFF00FF) == 0x18DA00F1
        return can_id == FUNCTIONAL_REQUEST_ID or 0x7E0 <= can_id <= 0x7E7

    def feed(self, can_id: int, extended: bool, data: bytes, t: float) -> list[tuple[int, ObdResponse]]:
        """One frame in; the (ecu, answer) pairs it completed out."""
        if not data:
            return []
        if self.is_request(can_id, extended):
            if data[0] >> 4 == 0 and len(data) > 1:             # single-frame request
                self._request(t, bytes(data[1:1 + (data[0] & 0x0F)]))
            return []
        addr = TWENTY_NINE_BIT if extended else ELEVEN_BIT
        if not addr.is_response(can_id, extended):
            return []
        done = self._reasm.feed(can_id, data)
        self._reasm.needs_flow_control.clear()               # the tester's job, not ours
        if done is None:
            return []
        resp = decode(done[1])
        if resp is None:
            return []
        self.extended = extended
        self.last_answer_t = t
        self._answer(t, can_id, resp)
        return [(can_id, resp)]

    # ------------------------------------------------------------------
    def _request(self, t: float, body: bytes):
        if not body:
            return
        if body[0] == MODE_CLEAR_DTCS:
            self.events.append((t, "Clear codes (Mode 04) sent"))
        elif body[0] == MODE_CONTROL and len(body) > 1 and body[1] not in SUPPORT_PIDS:
            self.events.append((t, f"On-board test TID {body[1]:02X} "
                                   f"({CONTROL_TESTS.get(body[1], 'manufacturer-defined')}) requested"))

    def _answer(self, t: float, ecu: int, r: ObdResponse):
        who = self.label(ecu)
        if not r.ok:
            if r.mode in (MODE_CLEAR_DTCS, MODE_CONTROL) and not r.pending:
                what = "clear codes" if r.mode == MODE_CLEAR_DTCS else "on-board test"
                self.events.append((t, f"{who} refused {what}: {r.error}"))
            return
        if r.mode == MODE_CURRENT_DATA and r.pid is not None and r.pid not in SUPPORT_PIDS:
            if r.pid in (0x01, 0x41) and len(r.raw) >= 6:
                status = decode_monitor_status(r.raw[2:6])
                if r.pid == 0x01:
                    prev = self.readiness.get(ecu)
                    if prev is not None and prev.mil != status.mil:
                        self.events.append((t, f"{who}: MIL {'ON' if status.mil else 'off'}"))
                    self.readiness[ecu] = status
                return
            for name, value, unit in r.values:
                self.values[(ecu, r.pid, name)] = Reading(t, value, unit)
        elif r.mode == MODE_FREEZE_FRAME and r.pid is not None and r.pid not in SUPPORT_PIDS:
            self.freeze.setdefault(ecu, {})[r.pid] = r
        elif r.mode in DTC_KINDS:
            kind = DTC_KINDS[r.mode]
            per = self.dtcs.setdefault(ecu, {k: [] for k in DTC_KINDS.values()})
            new = [c for c in r.dtcs if c not in per[kind]]
            gone = [c for c in per[kind] if c not in r.dtcs]
            if new:
                self.events.append((t, f"{who}: {kind} code(s) {', '.join(new)}"))
            if gone and kind != "permanent":
                self.events.append((t, f"{who}: {kind} code(s) {', '.join(gone)} no longer reported"))
            per[kind] = list(r.dtcs)
        elif r.mode == MODE_CLEAR_DTCS:
            self.events.append((t, f"{who}: codes cleared"))
            per = self.dtcs.get(ecu)
            if per:
                per["stored"], per["pending"] = [], []
            self.freeze.pop(ecu, None)
            self.results = {k: v for k, v in self.results.items() if k[0] != ecu}
        elif r.mode == MODE_TEST_RESULTS:
            for res in r.results:
                self.results[(ecu, res.mid, res.tid)] = res
        elif r.mode == MODE_CONTROL and r.pid is not None and r.pid not in SUPPORT_PIDS:
            self.events.append((t, f"{who}: started {r.name}"))
        elif r.mode == MODE_VEHICLE_INFO and r.pid is not None and r.text and r.pid not in SUPPORT_PIDS:
            self.info.setdefault(ecu, {})[r.name] = r.text
            if r.pid == 0x0A:
                self.names[ecu] = r.text.split("-")[0].strip() or r.text

    # ------------------------------------------------------------------
    def modules(self) -> set[int]:
        """Every module that has answered anything."""
        return {k[0] for k in self.values} | set(self.dtcs) | set(self.info) | set(self.readiness)

    def freeze_rows(self, ecu: int) -> list[ObdResponse]:
        """A module's freeze frame, the code that caused it first."""
        frame = self.freeze.get(ecu, {})
        return sorted(frame.values(), key=lambda r: (r.pid != 0x02, r.pid))

    def freeze_cause(self, ecu: int) -> str | None:
        r = self.freeze.get(ecu, {}).get(0x02)
        return r.text if r is not None and r.text else None

    def mil(self) -> MonitorStatus | None:
        return next(iter(self.readiness.values()), None)
