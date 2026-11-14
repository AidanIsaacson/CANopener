"""Turn a capture's raw log + markers into a summary and a live-data spreadsheet.

Everything is derived from ``capture.log`` again on each save, so relabelling markers
afterwards just regenerates the outputs.

* ``summary.txt``: markers, then one timeline of markers, fault codes going active / clearing,
  lamp changes, and ECUs going silent / coming back, each with the marker phase it fell in.
* ``live_data.csv``: every decoded live parameter sampled twice a second, with ``event`` (a
  marker tagged in that half-second) and ``phase`` (the latest marker) columns.

OBD-II traffic in the log (the app polling a car while recording) is decoded too, by the same
``obd.ObdSniffer`` the live view uses: its PIDs become columns, and codes read, cleared and tests
run join the timeline.

Meter readings (``measurements.csv``, see ``core/measure.py``) join the same way: each channel
is a column (its latest reading at that half-second), going out of its normal range or giving no
reading ("OL") are timeline events, and the summary lists every channel's range and its value at
each timestamp. A capture with meters but no bus gets rows from the meter readings alone.
"""

import csv
import datetime
import os

from .. import obd
from ..can.logfile import read_log
from ..j1939.addresses import sa_name
from ..j1939.constants import DM1
from ..j1939.dtc import decode_dm, fmi_text
from ..j1939.ids import parse_id
from ..j1939.signals import all_signals, id_signals
from ..j1939.spn import spn_name
from . import canopen, identity, profiles, units
from .capture import LIVE_CSV, SUMMARY_TXT, TRAFFIC_CSV, Capture, fmt_elapsed
from .measure import ChannelSeries, read_measurements, value_at
from .session import Session

SAMPLE_PERIOD = 0.5     # seconds between live_data.csv rows
SILENT_AFTER = 2.0      # an ECU that sends nothing for this long counts as "went silent"
MEASURE_MAX_AGE = 2.0   # a meter reading older than this is left blank in live_data.csv
MAX_CHANNEL_EVENTS = 100    # timeline events per channel before the rest are only counted


def _when(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def own_request(can_id: int, extended: bool) -> bool:
    """Is this frame one of a scan tool's own requests (ours), not something the vehicle said?
    J1939 from a tool address (0xF8 and up), OBD-II / UDS requests (0x7DF, 0x7E0-0x7E7, 29-bit from 0xF1)."""
    if not extended:
        return can_id == 0x7DF or 0x7E0 <= can_id <= 0x7E7
    return (can_id & 0xFF) >= 0xF8 or ((can_id & 0x1FFF0000) in (0x18DA0000, 0x18DB0000) and (can_id & 0xFF) == 0xF1)


def nothing_heard_lines(frames: int, heard: int, errors: list) -> list[str]:
    """Plain words for a capture in which the vehicle said nothing (empty when it said something)."""
    if heard:
        return []
    if not frames:
        out = ["Nothing was recorded from the bus: no frames at all."]
    else:
        out = [f"Nothing answered: all {frames} frames in this log are this tool's own requests. "
               "No module was heard."]
    if errors:
        out.append(f"The adapter reported trouble {len(errors)} time(s); first: {errors[0][1]}.")
    return out


class _Analysis:
    def __init__(self, capture: Capture, system: str):
        self.c = capture
        self.system = system
        self.t0 = capture.started or 0.0
        self.session = Session(None)
        self.events: list[tuple[float, str]] = []
        self.dm1_state: dict[int, tuple[frozenset, str]] = {}
        self.dm1_seen_t: dict[int, float] = {}
        self.last_seen: dict[int, float] = {}
        self.silent: dict[int, float] = {}       # sa -> time it went silent
        self.rows: list[list] = []
        self.columns: dict[tuple, int] = {}      # (signal key, sa) -> column index in values
        self.col_titles: list[str] = []
        self.next_sample = None
        self.frames = 0
        self.heard = 0                      # frames that aren't this tool's own requests
        self.errors: list[tuple[float, str]] = []    # error frames (the adapter or the bus reporting trouble)
        self.thin: set[int] = set()         # OBD-II modules that listed no live values at all
        self.obd = obd.ObdSniffer()
        self.series: dict[str, ChannelSeries] = {}
        # Text signals (display messages): the timeline notes each different message once, the
        # first time it shows (a blinking "LOW BTRY" would otherwise fill it).
        self.text_by_id = {cid: [s for s in sigs if s.is_text] for cid, sigs in id_signals().items()}
        self.text_by_id = {cid: sigs for cid, sigs in self.text_by_id.items() if sigs}
        self.text_by_pgn: dict[int, list] = {}
        for sig in all_signals():
            if sig.can_id is None and sig.is_text:
                self.text_by_pgn.setdefault(sig.pgn, []).append(sig)
        self.texts_seen: dict[tuple, set] = {}

    # --- per frame ---
    def feed(self, fr):
        self.frames += 1
        t = fr.timestamp
        if self.next_sample is None:
            self.next_sample = t
        while t >= self.next_sample:
            self._sample(self.next_sample)
            self.next_sample += SAMPLE_PERIOD
        self.session.feed(fr)
        if fr.error:
            self.errors.append((t, str(fr.error)))
        elif not own_request(fr.can_id, fr.extended):
            self.heard += 1
            data = bytes(fr.data)
            if not fr.extended and 0x7E8 <= fr.can_id <= 0x7EF and data[:3] == b"\x06\x41\x00":
                (self.thin.discard if any(data[3:7]) else self.thin.add)(fr.can_id)
        if not fr.error:
            self.obd.feed(fr.can_id, fr.extended, bytes(fr.data), t)
            if self.text_by_id or self.text_by_pgn:
                self._texts(fr)
        if not fr.extended or fr.error or (fr.can_id & 0x1FFF0000) in (0x18DA0000, 0x18DB0000):
            return                      # (29-bit OBD-II traffic isn't J1939 ECUs)
        sa = parse_id(fr.can_id).sa
        self._check_silence(t)
        if sa in self.silent:
            gone = self.silent.pop(sa)
            self.events.append((t, f"{sa_name(sa)} back on the bus (silent {t - gone:.1f} s)"))
        if sa not in self.last_seen and sa < 0xF8 and t - self.t0 > SILENT_AFTER and self.last_seen:
            self.events.append((t, f"{sa_name(sa)} appeared on the bus"))
        self.last_seen[sa] = t
        latest = self.session.latest.get((DM1, sa))
        if latest and latest[0] != self.dm1_seen_t.get(sa):
            self.dm1_seen_t[sa] = latest[0]
            self._dm1(t, sa, latest[1])

    def _texts(self, fr):
        if fr.extended:
            jid = parse_id(fr.can_id)
            sigs, src = [s for s in self.text_by_pgn.get(jid.pgn, ()) if s.accepts(jid.sa)], jid.sa
        else:
            sigs, src = self.text_by_id.get(fr.can_id, ()), canopen.id_src(fr.can_id)
        for sig in sigs:
            v = sig.decode(fr.data)
            seen = self.texts_seen.setdefault((sig.key, src), set())
            if v and v not in seen:
                seen.add(v)
                self.events.append((fr.timestamp, f'{sig.name}: "{v}"'))

    def _check_silence(self, t: float):
        for sa, last in self.last_seen.items():
            if sa not in self.silent and t - last > SILENT_AFTER and sa < 0xF8:
                self.silent[sa] = last
                self.events.append((last, f"{sa_name(sa)} went silent"))

    def _dm1(self, t: float, sa: int, data: bytes):
        lamps, dtcs = decode_dm(data)
        codes = frozenset((d.spn, d.fmi) for d in dtcs)
        lamp_text = str(lamps)
        prev = self.dm1_state.get(sa)
        prev_codes, prev_lamps = prev if prev else (frozenset(), None)
        who = sa_name(sa)
        for spn, fmi in sorted(codes - prev_codes):
            what = "already active (first DM1 from this ECU)" if prev is None else "went ACTIVE"
            self.events.append((t, f"{who}: SPN {spn} FMI {fmi} {what} - {spn_name(spn)}, {fmi_text(fmi)}"))
        for spn, fmi in sorted(prev_codes - codes):
            self.events.append((t, f"{who}: SPN {spn} FMI {fmi} cleared"))
        if prev_lamps is not None and lamp_text != prev_lamps:
            self.events.append((t, f"{who}: lamps now {lamp_text}"))
        elif prev is None and lamps.any_on:
            self.events.append((t, f"{who}: lamps {lamp_text}"))
        self.dm1_state[sa] = (codes, lamp_text)

    def _sample(self, t: float):
        values = {}
        found = []
        for sig in all_signals():
            if sig.can_id is None:
                found += [(sig, sa, d) for sa, _, d in self.session.latest_for(sig.pgn) if sig.accepts(sa)]
        for cid, sigs in id_signals().items():
            got = self.session.latest_std.get(cid)
            if got:
                found += [(sig, canopen.id_src(cid), got[1]) for sig in sigs]
        for sig, sa, d in found:
            v = sig.decode(d)
            if v is None:
                continue
            key = (sig.key, sa)
            if key not in self.columns:
                self.columns[key] = len(self.col_titles)
                unit = units.display_unit(sig.unit, self.system)
                who = "" if sa == 0x00 else f" ({canopen.source_name(sa, with_hex=False)})"
                self.col_titles.append(f"{sig.name}{who} [{unit}]" if unit else f"{sig.name}{who}")
            values[self.columns[key]] = units.convert(v, sig.unit, self.system)[0]
        for (ecu, pid, name), r in self.obd.values.items():
            if not isinstance(r.value, (int, float)):
                continue
            key = ("obd", ecu, pid, name)
            if key not in self.columns:
                self.columns[key] = len(self.col_titles)
                unit = units.display_unit(r.unit, self.system)
                self.col_titles.append(f"{name} ({self.obd.label(ecu)}) [{unit}]" if unit
                                       else f"{name} ({self.obd.label(ecu)})")
            values[self.columns[key]] = units.convert(r.value, r.unit, self.system)[0]
        self.rows.append([t, values])

    def finish(self, t_end: float):
        if self.next_sample is not None:
            self._sample(self.next_sample)
        self._check_silence(t_end)      # only ECUs that were already quiet before the end
        sn = self.session.canopen       # CANopen: errors, restarts, state changes, NMT commands
        if not profiles.speaks_canopen():
            return
        for e in sn.emcy_log:
            who = canopen.source_name(canopen.node_src(e.node))
            if e.emcy.reset:
                self.events.append((e.t, f"{who}: CANopen errors cleared (EMCY 0000)"))
            else:
                self.events.append((e.t, f"{who}: EMCY {e.emcy.code:04X} {e.emcy.text}"
                                         f" (register {e.emcy.register_text})"))
        for t, text in sn.events:
            self.events.append((t, "CANopen " + text))


def analyze(capture: Capture, system: str = units.METRIC, progress=None) -> _Analysis:
    frames = read_log(capture.log_path) if capture.log_path and os.path.exists(capture.log_path) else []
    a = _Analysis(capture, system)
    for i, fr in enumerate(frames):
        a.feed(fr)
        if progress and i % 5000 == 0:
            progress(i, len(frames))
    a.finish(capture.stopped or (frames[-1].timestamp if frames else a.t0))
    mpath = capture.measurements_path
    if mpath and os.path.exists(mpath):
        a.series = read_measurements(mpath, capture.meta.get("channels"))
        _add_measurements(a)
    return a


def _add_measurements(a: _Analysis):
    """Meter channels -> live_data columns (sampled on the same half-second grid) and timeline events."""
    series = [s for s in a.series.values() if s.points]
    if not series:
        return
    first = min(s.points[0][0] for s in series)
    last = max(s.points[-1][0] for s in series)
    t = a.rows[-1][0] + SAMPLE_PERIOD if a.rows else first
    while t <= last + 1e-9:                 # meter readings past the last CAN frame (or with no bus)
        a.rows.append([t, {}])
        t += SAMPLE_PERIOD
    for s in series:
        ch = s.channel
        col = len(a.col_titles)
        unit = units.display_unit(ch.unit, a.system)
        a.col_titles.append(f"{ch.name} [{unit}]" if unit else ch.name)
        for t, values in a.rows:
            v = value_at(s.points, t, MEASURE_MAX_AGE)
            if v is not None:
                values[col] = units.convert(v, ch.unit, a.system)[0]
        a.events += _channel_events(s, a.system)


def _channel_events(s: ChannelSeries, system: str) -> list[tuple[float, str]]:
    """Out of range / back in range, and no reading (flagged) / reading again."""
    ch = s.channel
    out, state, skipped = [], "ok", 0

    def fmt(v):
        return units.fmt(v, ch.unit, ch.decimals, system)

    rng = ""
    if ch.low is not None or ch.high is not None:
        rng = f", normal {'' if ch.low is None else fmt(ch.low)} to {'' if ch.high is None else fmt(ch.high)}"
    flags = dict(s.flags)
    for t, v in s.points:
        if v is None:
            now, text = "flag", f"{ch.name}: no reading ({flags.get(t) or 'no value'})"
        elif (ch.low is not None and v < ch.low) or (ch.high is not None and v > ch.high):
            now, text = "out", f"{ch.name} out of range: {fmt(v)}{rng}"
        else:
            now, text = "ok", f"{ch.name} back to normal: {fmt(v)}"
        if now == state:
            continue
        state = now
        if len(out) < MAX_CHANNEL_EVENTS:
            out.append((t, text))
        else:
            skipped += 1
    if skipped:
        out.append((s.points[-1][0], f"{ch.name}: {skipped} more range changes not listed"))
    return out


def measurement_summary(a: _Analysis) -> list[str]:
    if not a.series:
        return []
    c = a.c
    L = ["", "-- Measurements --"]
    for s in a.series.values():
        ch = s.channel
        vals = [v for _, v in s.points if v is not None]
        if not vals:
            L.append(f"  {ch.name:<28} no valid readings ({len(s.points)} flagged)")
            continue

        def fmt(v, ch=ch):
            return units.fmt(v, ch.unit, ch.decimals, a.system)

        flagged = f", {len(s.points) - len(vals)} without a reading" if len(vals) < len(s.points) else ""
        L.append(f"  {ch.name:<28} min {fmt(min(vals)):<12} max {fmt(max(vals)):<12} "
                 f"avg {fmt(sum(vals) / len(vals)):<12} {len(vals)} readings{flagged}")
    tagged = c.tagged()
    if tagged:
        L += ["", "-- Measurements at each timestamp --"]
        for i, m in tagged:
            parts = []
            for s in a.series.values():
                v = value_at(s.points, m.t, MEASURE_MAX_AGE)
                shown = "n/a" if v is None else units.fmt(v, s.channel.unit, s.channel.decimals, a.system)
                parts.append(f"{s.channel.name} {shown}")
            L.append(f"  {c.title(i):<34} " + ", ".join(parts))
    return L


def _cell(v) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else f"{v:.6g}"


def write_live_csv(a: _Analysis, path: str):
    c = a.c
    tagged = [(m.t, c.title(i)) for i, m in c.tagged()]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "elapsed_s", "event", "phase"] + a.col_titles)
        prev = None
        for t, values in a.rows:
            events = [title for mt, title in tagged if (prev is None or mt > prev) and mt <= t]
            prev = t
            wall = datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-4] if t > 1e9 else f"{t:.1f}"
            row = [wall, f"{t - a.t0:.1f}", " / ".join(events), c.phase_at(t)]
            row += [_cell(values.get(i)) for i in range(len(a.col_titles))]
            w.writerow(row)


def summary_text(a: _Analysis) -> str:
    c = a.c
    L = [f"=== Capture: {c.name.strip() or '(unnamed)'} ==="]
    if c.started:
        L.append(f"Recorded     {_when(c.started)}  ->  {_when(c.stopped or c.started)}"
                 f"   ({fmt_elapsed(c.elapsed())[1:]} long, {a.frames} frames"
                 + (f", {sum(len(x.points) for x in a.series.values())} meter readings)" if a.series else ")"))
    for k in ("source", "interface", "bitrate", "profile", "listen_only", "app_version"):
        if k in c.meta:
            L.append(f"{k.replace('_', ' ').capitalize():<13}{c.meta[k]}")
    tr = c.meta.get("trimmed")
    if tr and tr.get("original_start") and tr.get("original_stop"):
        full = f"; full log kept as {tr['full_log']}" if tr.get("full_log") else ""
        L.append(f"Trimmed      to {tr['before_s'] / 60:g} min before the first timestamp and "
                 f"{tr['after_s'] / 60:g} min after the last, from a "
                 f"{fmt_elapsed(tr['original_stop'] - tr['original_start'])[1:]} recording "
                 f"({_when(tr['original_start'])} -> {_when(tr['original_stop'])}, "
                 f"{tr.get('dropped_frames', 0)} frames cut{full})")
    quiet = nothing_heard_lines(a.frames - len(a.errors), a.heard, a.errors)
    if quiet:
        L += [""] + [f"!! {line}" for line in quiet]
    for ecu in sorted(a.thin):
        L += ["", f"!! {a.obd.label(ecu)} lists no live data at all: 'no codes' from it may not be the whole "
                  "story (docs/BLUETOOTH-OBD.md, \"Thin K-line computers\")."]
    if c.notes.strip():
        L += ["", "Notes / customer concern:"] + [f"  {line}" for line in c.notes.strip().splitlines()]

    L += ["", "-- Timestamps --"]
    if not c.markers:
        L.append("  (none)")
    for i, m in enumerate(c.markers):
        when = fmt_elapsed(m.t - a.t0) if m.tagged else "not tagged"
        note = f"   ({m.note})" if m.note else ""
        L.append(f"  {c.title(i):<34} {when}{note}")

    timeline = [(m.t, f"[{c.title(i)}]") for i, m in c.tagged()] + list(a.events) + list(a.obd.events)
    timeline.sort(key=lambda x: (x[0], not x[1].startswith("[")))
    L += ["", "-- Timeline (markers in [brackets]; other lines show the phase they happened in) --"]
    if not timeline:
        L.append("  (nothing happened: no markers, fault codes or ECU dropouts)")
    for t, text in timeline:
        phase = "" if text.startswith("[") else c.phase_at(t)
        during = f"   (during {phase})" if phase else ""
        L.append(f"  {fmt_elapsed(t - a.t0):>11}  {text}{during}")

    hist = sorted(a.session.dtc_hist.values(), key=lambda h: (h.sa, h.spn, h.fmi))
    L += ["", "-- Fault codes seen in this capture --"]
    if not hist:
        L.append("  none")
    for h in hist:
        oc = h.oc if h.oc is not None else "n/a"
        L.append(f"  {h.status:<12} {sa_name(h.sa):<26} SPN {h.spn:<7} FMI {h.fmi:<3} OC {oc:<4}"
                 f" {spn_name(h.spn)} - {fmi_text(h.fmi)}")
        L.append(f"  {'':<12} first {fmt_elapsed(h.first - a.t0)} ({c.phase_at(h.first) or 'before first marker'}),"
                 f" last {fmt_elapsed(h.last - a.t0)}, seen {h.seen}x")

    if a.obd.dtcs:
        L += ["", "-- OBD-II codes read in this capture (last answer from each module) --"]
        for ecu, kinds in sorted(a.obd.dtcs.items()):
            for kind, codes in kinds.items():
                L += [f"  {kind:<10} {a.obd.label(ecu):<18} {code}  {obd.describe_dtc(code)}" for code in codes]
            if not any(kinds.values()):
                L.append(f"  {a.obd.label(ecu)} answered: no codes ({', '.join(kinds) or 'stored'})")

    L += measurement_summary(a)

    L += ["", "-- ECUs --"]
    ecus = [e for e in a.session.ecu_list() if e.sa < 0xF8]
    if not ecus:
        modules = ", ".join(a.obd.label(ecu) for ecu in sorted(a.obd.dtcs)) if a.obd.dtcs else ""
        L.append(f"  no J1939 ECUs (OBD-II modules that answered: {modules})" if modules else "  none heard")
    for e in ecus:
        L.append(f"  {sa_name(e.sa):<28} {e.frames:>8} frames   first {fmt_elapsed(e.first_seen - a.t0)}"
                 f"   last {fmt_elapsed(e.last_seen - a.t0)}")
    return "\n".join(L) + "\n"


def _example(a: _Analysis, row: identity.TrafficRow) -> str:
    """The latest payload of one stream, as hex (for the README's undecoded table)."""
    s = a.session
    got = s.latest_std.get(row.key) if row.kind == "11-bit" else s.latest.get((row.key, row.sa))
    return bytes(got[1][:16]).hex(" ").upper() if got else ""


def write_traffic_csv(rows: list, path: str):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "id", "source", "name", "frames", "rate_hz", "dlc", "decoded", "first", "last"])
        for r in rows:
            w.writerow([r.kind, r.ident, r.source, r.name, r.frames, f"{r.rate_hz:.3f}", r.dlc,
                        "yes" if r.decoded else "no", f"{r.first:.6f}", f"{r.last:.6f}"])


def bus_summary(a: _Analysis, rows: list) -> dict:
    """What capture_readme shows: identity heard, senders, undecoded traffic (JSON-friendly)."""
    und = [r for r in rows if not r.decoded]
    return {
        "identity": list(dict.fromkeys(identity.seen_lines(a.session, a.obd.info)
                                       + list(a.c.meta.get("identity_before") or []))),
        "ecus": [{"name": sa_name(e.sa), "frames": e.frames} for e in a.session.ecu_list() if e.sa < 0xF8],
        "heard_frames": a.heard,
        "quiet": nothing_heard_lines(a.frames - len(a.errors), a.heard, a.errors),
        "thin": [a.obd.label(ecu) for ecu in sorted(a.thin)],
        "traffic_count": len(rows),
        "undecoded_count": len(und),
        "undecoded_frames": sum(r.frames for r in und),
        "undecoded": [{"ident": r.ident, "source": r.source, "name": r.name, "frames": r.frames,
                       "rate_hz": round(r.rate_hz, 2), "dlc": r.dlc, "example": _example(a, r)}
                      for r in und[:200]],
    }


def write_outputs(capture: Capture, system: str = units.METRIC, progress=None) -> _Analysis:
    a = analyze(capture, system, progress)
    write_live_csv(a, os.path.join(capture.folder, LIVE_CSV))
    with open(os.path.join(capture.folder, SUMMARY_TXT), "w") as f:
        f.write(summary_text(a))
    rows = identity.traffic(a.session)
    write_traffic_csv(rows, os.path.join(capture.folder, TRAFFIC_CSV))
    capture.meta["bus"] = bus_summary(a, rows)
    return a
