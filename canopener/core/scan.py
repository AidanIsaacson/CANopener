"""Diagnostic reads: send requests, wait for answers, and collect a ``ScanResult``.

A scan covers whatever the bus speaks. J1939: DM requests (skipped on a bus that has CANopen
nodes and no 29-bit traffic, so nothing sends extended frames to controllers that don't expect
them). CANopen: active errors come from the EMCY messages the nodes broadcast; reading stored
codes also reads each node's error history (object 0x1003) and machine info reads their identity
over SDO (``core.canopen``; not in listen-only).
"""

import time
from dataclasses import dataclass, field

from ..j1939.addresses import sa_name
from ..j1939.constants import (
    DM1,
    DM2,
    DM4,
    DM5,
    DM6,
    DM12,
    DM23,
    DTC_PGNS,
    GLOBAL_SA,
    INFO_PGNS,
    PGN_ADDRESS_CLAIM,
    STATUS_ORDER,
    pgn_label,
)
from ..j1939.dtc import FreezeFrame, Lamps, Readiness, decode_dm, decode_dm4, decode_dm5, fmi_text
from ..j1939.info import describe as describe_info
from ..j1939.spn import spn_name
from . import canopen
from .canopen import CanopenClient, CanopenCode, SdoError
from .session import Ack, Session

ACTIVE = [DM1]
STORED = [DM2]
EMISSIONS = [DM6, DM12, DM23, DM5]
MACHINE_INFO = list(INFO_PGNS) + [PGN_ADDRESS_CLAIM]
FULL = [DM1, DM2, DM6, DM12, DM23, DM4, DM5] + MACHINE_INFO

PRESETS = {
    "active": ("Active codes", ACTIVE, 2.5),
    "stored": ("Stored codes", STORED, 2.5),
    "emissions": ("Pending / emissions codes", EMISSIONS, 2.5),
    "info": ("Machine info", MACHINE_INFO, 2.0),
    "full": ("Full scan", FULL, 4.0),
}


@dataclass
class DtcRow:
    pgn: int
    sa: int
    spn: int
    fmi: int
    oc: int | None
    cm: int
    alt_spn: int | None
    lamps: Lamps
    first: float
    last: float
    seen: int

    @property
    def status(self) -> str:
        return DTC_PGNS[self.pgn]

    @property
    def spn_text(self) -> str:
        return spn_name(self.spn)

    @property
    def fmi_text(self) -> str:
        return fmi_text(self.fmi)

    @property
    def spn_label(self) -> str:
        return str(self.spn) + (f" (alt {self.alt_spn})" if self.alt_spn is not None else "")

    @property
    def oc_text(self) -> str:
        return "n/a" if self.oc is None else str(self.oc)


@dataclass
class QuietEcu:
    """An ECU that answered a DTC request but reported no codes."""
    pgn: int
    sa: int
    lamps: Lamps

    @property
    def status(self) -> str:
        return DTC_PGNS[self.pgn]


@dataclass
class InfoItem:
    pgn: int
    sa: int
    label: str
    text: str


@dataclass
class ScanResult:
    title: str
    started: float
    finished: float
    requested: list[int]
    responses: dict[tuple[int, int], tuple[float, bytes]]
    rows: list[DtcRow]
    quiet: list[QuietEcu]
    acks: list[Ack]
    listen_only: bool
    offline: bool = False
    tx_error: str | None = None
    source: str = ""
    retried: list[int] = field(default_factory=list)
    canopen: list[CanopenCode] = field(default_factory=list)     # CANopen errors (active / stored)
    canopen_errors: list[str] = field(default_factory=list)      # nodes that didn't answer a read
    j1939_skipped: bool = False     # a CANopen-only bus: no J1939 requests were sent

    @property
    def code_count(self) -> int:
        return len(self.rows) + len(self.canopen)

    def responders(self, pgn: int) -> list[int]:
        return sorted(sa for (p, sa) in self.responses if p == pgn)

    def nacks(self, pgn: int) -> list[Ack]:
        return [a for a in self.acks if a.pgn == pgn and a.control != 0]

    def response_summary(self) -> list[tuple[int, str, str]]:
        """(pgn, label, outcome) for every requested PGN (none if J1939 was skipped)."""
        out = []
        if self.j1939_skipped:
            return out
        for p in self.requested:
            sas = self.responders(p)
            if sas:
                outcome = "from " + ", ".join(sa_name(s) for s in sas)
            else:
                outcome = "not in log" if self.offline else "no response"
                na = self.nacks(p)
                if na:
                    outcome += "; " + ", ".join(f"{sa_name(a.sa)}: {a.text}" for a in na)
            out.append((p, pgn_label(p) or f"PGN {p:04X}", outcome))
        return out

    def freeze_frames(self) -> dict[int, tuple[list[FreezeFrame], bytes]]:
        return {sa: (decode_dm4(d), d) for (p, sa), (t, d) in sorted(self.responses.items()) if p == DM4}

    def readiness(self) -> dict[int, Readiness]:
        out = {}
        for (p, sa), (_t, d) in sorted(self.responses.items()):
            if p == DM5:
                r = decode_dm5(d)
                if r:
                    out[sa] = r
        return out

    def lamps(self) -> dict[int, Lamps]:
        return {sa: decode_dm(d)[0] for (p, sa), (t, d) in sorted(self.responses.items()) if p == DM1}

    def info(self) -> list[InfoItem]:
        return [InfoItem(p, sa, INFO_PGNS[p], describe_info(p, d))
                for (p, sa), (t, d) in sorted(self.responses.items()) if p in INFO_PGNS]


def collect(session: Session, requested: list[int], since: float, *, title: str = "Scan",
            started: float | None = None) -> ScanResult:
    """Build a ScanResult from whatever the session has received since ``since``."""
    got = session.snapshot_since(since)
    with session.lock:
        hist = [h for h in session.dtc_hist.values() if h.last >= since and h.pgn in requested]
    rows = [DtcRow(h.pgn, h.sa, h.spn, h.fmi, h.oc, h.cm, h.alt_spn, h.lamps, h.first, h.last, h.seen)
            for h in hist]
    rows.sort(key=lambda r: (STATUS_ORDER.get(r.status, 9), r.sa, r.spn, r.fmi))

    with_codes = {(r.pgn, r.sa) for r in rows}
    quiet = [QuietEcu(p, sa, decode_dm(d)[0]) for (p, sa), (t, d) in sorted(got.items())
             if p in DTC_PGNS and p in requested and (p, sa) not in with_codes]
    quiet.sort(key=lambda q: (STATUS_ORDER.get(q.status, 9), q.sa))

    wanted = ({"Active"} if DM1 in requested else set()) | ({"Stored"} if DM2 in requested else set())
    return ScanResult(
        title=title, started=since if started is None else started, finished=time.time(),
        requested=list(requested), responses=got, rows=rows, quiet=quiet,
        acks=session.acks_since(since), listen_only=session.listen_only, offline=session.offline,
        tx_error=session.health.tx_error, source=session.describe(),
        canopen=[c for c in canopen.codes(session) if c.status in wanted],
    )


def read_canopen(session: Session, pgns: list[int]) -> list[str]:
    """The CANopen part of a scan: each node's error history (with DM2, stored codes) and identity
    (with machine info). Answers land in ``session.canopen`` as they arrive. Returns problems."""
    nodes = [n.node for n in session.canopen_nodes()]
    if not nodes or session.listen_only:
        return []
    client = CanopenClient(session, timeout=0.5)
    problems = []
    try:
        for node in nodes:
            try:
                if DM2 in pgns:
                    client.error_history(node)
                if PGN_ADDRESS_CLAIM in pgns:
                    client.node_info(node)
            except SdoError as e:
                problems.append(f"{canopen.source_name(canopen.node_src(node))}: {e}")
    finally:
        client.close()
    return problems


def run_scan(session: Session, pgns: list[int], listen: float = 2.5, *, title: str = "Scan",
             retry_wait: float = 2.0, max_retry_targets: int = 8, sleep=time.sleep) -> ScanResult:
    """Request each PGN globally, wait, then retry anything unanswered directly at each ECU.

    Some ECUs ignore global requests for DM2/DM4 or ID PGNs but answer a request addressed
    to them, so the retry goes to every ECU heard on the bus (busiest first).
    """
    t0 = time.time()
    sent = False
    j1939 = session.has_j1939 or not session.has_canopen
    for p in pgns if j1939 else ():
        sent |= session.request(p, GLOBAL_SA)
        sleep(0.05)
    problems = read_canopen(session, pgns)
    sleep(listen if j1939 else min(listen, 1.0))
    retried = []
    if sent:
        got = session.snapshot_since(t0)
        nacked = {a.pgn for a in session.acks_since(t0)}
        missing = [p for p in pgns if p not in nacked and not any(k[0] == p for k in got)]
        targets = session.known_ecus()[:max_retry_targets] or [0x00]
        if missing:
            for p in missing:
                for da in targets:
                    session.request(p, da)
                    sleep(0.02)
            retried = missing
            sleep(retry_wait)
    result = collect(session, pgns, t0, title=title)
    result.retried = retried
    result.canopen_errors = problems
    result.j1939_skipped = not j1939
    return result


def run_preset(session: Session, preset: str, **kw) -> ScanResult:
    title, pgns, listen = PRESETS[preset]
    return run_scan(session, pgns, listen, title=title, **kw)


def offline_result(session: Session, title: str = "Log file decode") -> ScanResult:
    return collect(session, FULL, 0.0, title=title)
