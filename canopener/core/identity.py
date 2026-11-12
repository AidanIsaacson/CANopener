"""What the bus says about the machine, and which of its traffic we could decode.

Two jobs, both read-only (they look at what a ``Session`` already received; nothing is sent):

* ``candidates(session)``: ways to fill in a capture's machine details (``capture.MACHINE_FIELDS``)
  from the bus: each ECU's J1939 Component ID (make / model / serial / unit), the VIN, each
  CANopen node's identity (0x1008 name, 0x1018 serial) and an OBD-II VIN. Engine hours ride along
  with each. Component ID and VIN are usually only sent when asked: Read Machine Info (or a Full
  Scan) sends the standard J1939 Requests for them.
* ``traffic(session)``: one row per J1939 PGN / source and per 11-bit ID, with frame counts, rates
  and whether anything decoded it. A capture's README lists the undecoded ones: that's where "the
  data doesn't parse" usually starts.

Standard library only (no Qt / Pygame).
"""

from dataclasses import dataclass, field

from ..canopen import cob, objects
from ..j1939 import fields
from ..j1939.addresses import sa_name
from ..j1939.constants import (
    DTC_PGNS,
    INFO_PGNS,
    PGN_ACK,
    PGN_ADDRESS_CLAIM,
    PGN_COMPONENT_ID,
    PGN_ENGINE_HOURS,
    PGN_REQUEST,
    PGN_TPCM,
    PGN_TPDT,
    PGN_VIN,
    pgn_label,
)
from ..j1939.info import decode_ascii_fields, describe
from ..j1939.signals import all_signals, id_signals
from . import canopen

# J1939 PGNs the app understands without a live-data signal: protocol plumbing, codes, IDs.
STRUCTURAL_PGNS = {PGN_ACK, PGN_REQUEST, PGN_TPDT, PGN_TPCM, PGN_ADDRESS_CLAIM} | set(DTC_PGNS) | set(INFO_PGNS)
# 11-bit CANopen kinds that are decoded as such (PDOs only through a profile's signals).
STRUCTURAL_KINDS = {"nmt", "sync", "time", "emcy", "sdo_tx", "sdo_rx", "heartbeat", "lss"}
OBD_IDS = {0x7DF} | set(range(0x7E0, 0x7F0))


@dataclass
class Candidate:
    """One way to fill the machine details: a label for the picker and the fields it sets."""
    label: str
    fields: dict = field(default_factory=dict)
    source: str = ""            # where it came from, e.g. "Engine #1 (0x00), Component ID"


def _hours(latest: dict) -> str:
    """Engine hours from PGN 65253, engine first: "" when none heard."""
    found = sorted((sa, d) for (p, sa), (_, d) in latest.items() if p == PGN_ENGINE_HOURS)
    for _sa, d in found:
        h = fields.u32(bytes(d), 0, 0.05)
        if h is not None:
            return f"{h:.1f}"
    return ""


def _summary(f: dict) -> str:
    parts = [f.get("make", ""), f.get("model", "")]
    if f.get("serial"):
        parts.append(f"SN {f['serial']}")
    if f.get("unit"):
        parts.append(f"unit {f['unit']}")
    return " ".join(p for p in parts if p)


def candidates(session, obd_info: dict | None = None) -> list[Candidate]:
    """Machine-detail suggestions from what ``session`` has heard (J1939 + CANopen), plus an
    OBD-II VIN from ``obd_info`` (``ObdSniffer.info``: ecu -> {name: text}) when given."""
    out: list[Candidate] = []
    if session is not None:
        with session.lock:
            latest = dict(session.latest)
        hours = _hours(latest)
        extra = {"hours": hours} if hours else {}
        for (pgn, sa), (_, d) in sorted(latest.items(), key=lambda kv: kv[0][1]):
            if pgn == PGN_COMPONENT_ID:
                parts = decode_ascii_fields(d)
                if not parts:
                    continue
                keys = ("make", "model", "serial", "unit")
                f = {k: v for k, v in zip(keys, parts)} if len(parts) <= 4 else {"model": " ".join(parts)}
                f.update(extra)
                out.append(Candidate(f"{sa_name(sa)}: {_summary(f) or describe(pgn, d)}", f,
                                     f"{sa_name(sa)}, Component ID (PGN 65259)"))
        for (pgn, sa), (_, d) in sorted(latest.items(), key=lambda kv: kv[0][1]):
            if pgn == PGN_VIN:
                parts = decode_ascii_fields(d)
                if parts:
                    out.append(Candidate(f"VIN {parts[0]} ({sa_name(sa)})", {"serial": parts[0], **extra},
                                         f"{sa_name(sa)}, VIN (PGN 65260)"))
        for n in session.canopen_nodes():
            name = n.value(objects.DEVICE_NAME)
            serial = n.value(objects.IDENTITY, 4)
            vendor = n.value(objects.IDENTITY, 1)
            if not (name or serial):
                continue
            f = {}
            if name:
                f["model"] = objects.show(objects.DEVICE_NAME, 0, name)
            if serial:
                f["serial"] = objects.show(objects.IDENTITY, 4, serial)
            if vendor:
                f["make"] = objects.show(objects.IDENTITY, 1, vendor)
            who = canopen.source_name(canopen.node_src(n.node))
            out.append(Candidate(f"{who}: {_summary(f)}", f, f"{who}, CANopen identity (0x1008 / 0x1018)"))
        if hours and not out:
            out.append(Candidate(f"Engine hours {hours} h", dict(extra), "Engine hours (PGN 65253)"))
    for ecu, info in sorted((obd_info or {}).items()):
        vin = (info or {}).get("VIN")
        if vin:
            out.append(Candidate(f"VIN {vin} (OBD-II)", {"serial": vin}, f"OBD-II Mode 09 from {ecu:X}"))
    return out


def seen_lines(session, obd_info: dict | None = None) -> list[str]:
    """Plain lines for a report: every identity item heard, with who sent it."""
    lines = []
    if session is not None:
        with session.lock:
            latest = dict(session.latest)
        for (pgn, sa), (_, d) in sorted(latest.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            if pgn in INFO_PGNS:
                lines.append(f"{sa_name(sa)}: {INFO_PGNS[pgn]}: {describe(pgn, d)}")
        for n in session.canopen_nodes():
            who = canopen.source_name(canopen.node_src(n.node))
            for (index, sub), v in sorted(n.od.items()):
                if index in (objects.DEVICE_NAME, objects.IDENTITY) or index in objects.TEXT_OBJECTS:
                    lines.append(f"{who}: {objects.name(index, sub)}: {objects.show(index, sub, v)}")
    for ecu, info in sorted((obd_info or {}).items()):
        for name, text in sorted((info or {}).items()):
            lines.append(f"OBD-II {ecu:X}: {name}: {text}")
    return lines


@dataclass
class TrafficRow:
    kind: str               # "j1939" or "11-bit"
    ident: str              # "PGN 65265 (0xFEF1)" / "0x185"
    source: str             # sender ("Engine #1 (0x00)", "node 05") or ""
    name: str               # what the PGN / ID is, "" when unknown
    frames: int
    rate_hz: float
    decoded: bool           # live-data signals (or a built-in decoder) cover it
    first: float = 0.0
    last: float = 0.0
    dlc: int = 0
    key: int = 0            # the PGN (J1939) or the 11-bit ID
    sa: int | None = None   # J1939 source address


def _rate(st) -> float:
    span = st.last - st.first
    return (st.count - 1) / span if st.count > 1 and span > 0 else 0.0


def traffic(session) -> list[TrafficRow]:
    """Every PGN / source and 11-bit ID the session saw, busiest first."""
    if session is None:
        return []
    with session.lock:
        j = {k: (v.count, v.first, v.last, v.dlc) for k, v in session.stats.items()}
    std = session.std_stats_copy()
    by_pgn: dict[int, list] = {}
    for sig in all_signals():
        if sig.can_id is None:
            by_pgn.setdefault(sig.pgn, []).append(sig)
    by_id = id_signals()
    rows = []
    for (sa, pgn), (count, first, last, dlc) in j.items():
        decoded = pgn in STRUCTURAL_PGNS or any(s.accepts(sa) for s in by_pgn.get(pgn, ()))
        span = last - first
        rate = (count - 1) / span if count > 1 and span > 0 else 0.0
        rows.append(TrafficRow("j1939", f"PGN {pgn} (0x{pgn:04X})", sa_name(sa), pgn_label(pgn), count, rate,
                               decoded, first, last, dlc, pgn, sa))
    for cid, st in std.items():
        kind, node = cob.classify(cid)
        if cid in OBD_IDS:
            name, decoded = "OBD-II request / answer", True
        else:
            name = cob.KIND_LABELS.get(kind, "") if kind else ""
            decoded = bool(by_id.get(cid)) or kind in STRUCTURAL_KINDS
        src = canopen.source_name(canopen.id_src(cid), with_hex=False) if kind and node else ""
        rows.append(TrafficRow("11-bit", f"0x{cid:03X}", src, name, st.count, _rate(st), decoded,
                               st.first, st.last, st.dlc, cid))
    rows.sort(key=lambda r: (-r.frames, r.ident))
    return rows
