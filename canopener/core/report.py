"""Plain-text reports from a ScanResult (shown in the GUI, printed by the CLI, saved to disk)."""

import datetime

from ..j1939.addresses import sa_name
from ..j1939.constants import is_proprietary, pgn_label
from ..j1939.fields import hexs
from ..j1939.signals import all_signals
from ..j1939.spn import spn_name
from . import units
from .scan import ScanResult
from .session import Session


def _when(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def code_table(result: ScanResult) -> list[str]:
    lines = ["FAULT CODES", "-" * 100]
    for r in result.rows:
        lines.append(f"{r.status:<13} {sa_name(r.sa):<26} SPN {r.spn_label:<14} FMI {r.fmi:<3} "
                     f"OC {r.oc_text:<4} {r.spn_text} - {r.fmi_text}   [{r.lamps}]")
    for q in result.quiet:
        lines.append(f"{q.status:<13} {sa_name(q.sa):<26} no codes reported   [{q.lamps}]")
    if not result.rows and not result.quiet:
        lines.append("(no DM responses received)")
    return lines


def live_snapshot(session: Session, system: str = units.METRIC, since: float = 0.0) -> list[str]:
    lines = []
    for sig in all_signals():
        src = [s for s in session.latest_for(sig.pgn)
               if s[1] >= since and sig.accepts(s[0])]
        if not src:
            continue
        sa, t, d = src[0]
        v = sig.decode(d)
        if v is not None:
            lines.append(f"  {sig.name:<28} {units.fmt(v, sig.unit, sig.decimals, system):<14} [{sa_name(sa)}]")
    return lines


def bus_survey(session: Session) -> list[str]:
    lines = []
    by_sa: dict[int, list] = {}
    for (sa, pgn), st in session.stats_copy().items():
        by_sa.setdefault(sa, []).append((pgn, st))
    ecus = {e.sa: e for e in session.ecu_list()}
    for sa in sorted(by_sa):
        e = ecus.get(sa)
        claim = f"   NAME: {e.name.summary()}" if e and e.name else ""
        lines.append(f"  {sa_name(sa)}{claim}")
        for pgn, st in sorted(by_sa[sa], key=lambda x: x[0]):
            rate = f"{st.rate:.1f}/s" if st.rate else ""
            lines.append(f"      PGN {pgn:05X} ({pgn:>6})  x{st.count:<7} {rate:>8}  {pgn_label(pgn)}")
    return lines


def build_report(result: ScanResult, session: Session | None = None, *, full: bool = False,
                 system: str = units.METRIC) -> str:
    L = [f"=== {result.title} - {_when(result.finished)} ==="]
    if result.source:
        L.append(f"Source: {result.source}")
    if not result.offline and result.listen_only:
        L.append("Listen-only mode: no requests sent, only broadcast data (DM1, live data) shown.")
    if result.tx_error:
        L.append(f"!! Transmit error: {result.tx_error} (nothing ACKing on the bus? wrong bitrate?)")
    if result.retried:
        L.append("Retried directly at each ECU: " + ", ".join(pgn_label(p) or f"{p:04X}" for p in result.retried))
    L.append("")

    L += code_table(result)
    L.append("")

    L.append("-- Responses --")
    for _, label, outcome in result.response_summary():
        L.append(f"  {label:<42} {outcome}")
    L.append("")

    lamps = result.lamps()
    if lamps:
        L.append("-- Warning lamps (from DM1) --")
        for sa, lp in lamps.items():
            L.append(f"  {sa_name(sa):<28} {lp}")
        L.append("")

    for sa, r in result.readiness().items():
        L.append(f"-- DM5 readiness {sa_name(sa)}: {r.active if r.active is not None else 'n/a'} active, "
                 f"{r.previously_active if r.previously_active is not None else 'n/a'} previously active, "
                 f"OBD: {r.obd_text}")
    if result.readiness():
        L.append("")

    for sa, (frames, raw) in result.freeze_frames().items():
        L.append(f"-- Freeze frames (DM4) from {sa_name(sa)} --")
        if not frames:
            L.append(f"  none / undecodable: {hexs(raw)}")
        for f in frames:
            dt = f.dtc
            L.append(f"  SPN {dt.spn} FMI {dt.fmi} ({spn_name(dt.spn)}), OC {dt.oc}")
            L.append(f"     rpm {units.fmt(f.rpm, 'rpm', 0)}, load {units.fmt(f.load_pct, '%', 0)}, "
                     f"coolant {units.fmt(f.coolant_c, '°C', 0, system)}, "
                     f"boost {units.fmt(f.boost_kpa, 'kPa', 0, system)}, "
                     f"speed {units.fmt(f.speed_kmh, 'km/h', 1, system)}, torque mode {f.torque_mode}")
            if f.extra:
                L.append(f"     manufacturer data: {hexs(f.extra)}")
        L.append("")

    info = result.info()
    if info:
        L.append("-- Machine / ECU info --")
        for it in info:
            L.append(f"  {it.label:<18} [{sa_name(it.sa)}] {it.text}")
        L.append("")

    if session is not None and (full or result.offline):
        L.append("-- Live data snapshot --")
        snap = live_snapshot(session, system, 0.0 if result.offline else result.started)
        L += snap or ["  (no standard live PGNs seen - key on / engine running?)"]
        L.append("")

        L.append("-- Bus survey (source addresses and PGNs seen this session) --")
        L += bus_survey(session)
        L.append("")

        prop = [(p, sa, d) for (p, sa), (t, d) in sorted(result.responses.items()) if is_proprietary(p)]
        if prop:
            L.append("-- Proprietary frames (latest payload) --")
            for p, sa, d in prop:
                L.append(f"  PGN {p:05X} from {sa_name(sa):<26} {hexs(d)}")
            L.append("")

        h = session.health
        if h.rx_errors:
            L.append(f"-- Bus errors: {h.rx_errors} error frame(s), last: {h.last_error}")
            L.append("")

    return "\n".join(L).rstrip() + "\n"
