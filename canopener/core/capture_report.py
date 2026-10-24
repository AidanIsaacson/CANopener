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
from ..j1939.signals import all_signals
from ..j1939.spn import spn_name
from . import units
from .capture import LIVE_CSV, SUMMARY_TXT, Capture, fmt_elapsed
from .session import Session

SAMPLE_PERIOD = 0.5     # seconds between live_data.csv rows
SILENT_AFTER = 2.0      # an ECU that sends nothing for this long counts as "went silent"


def _when(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


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
        self.obd = obd.ObdSniffer()

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
        if not fr.error:
            self.obd.feed(fr.can_id, fr.extended, bytes(fr.data), t)
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
        for sig in all_signals():
            for sa, _, d in self.session.latest_for(sig.pgn):
                if not sig.accepts(sa):
                    continue
                v = sig.decode(d)
                if v is None:
                    continue
                key = (sig.key, sa)
                if key not in self.columns:
                    self.columns[key] = len(self.col_titles)
                    unit = units.display_unit(sig.unit, self.system)
                    who = "" if sa == 0x00 else f" ({sa_name(sa, with_hex=False)})"
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


def analyze(capture: Capture, system: str = units.METRIC, progress=None) -> _Analysis:
    frames = read_log(capture.log_path) if capture.log_path and os.path.exists(capture.log_path) else []
    a = _Analysis(capture, system)
    for i, fr in enumerate(frames):
        a.feed(fr)
        if progress and i % 5000 == 0:
            progress(i, len(frames))
    a.finish(capture.stopped or (frames[-1].timestamp if frames else a.t0))
    return a


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
            row += ["" if values.get(i) is None else f"{values[i]:.6g}" for i in range(len(a.col_titles))]
            w.writerow(row)


def summary_text(a: _Analysis) -> str:
    c = a.c
    L = [f"=== Capture: {c.name.strip() or '(unnamed)'} ==="]
    if c.started:
        L.append(f"Recorded     {_when(c.started)}  ->  {_when(c.stopped or c.started)}"
                 f"   ({fmt_elapsed(c.elapsed())[1:]} long, {a.frames} frames)")
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

    L += ["", "-- ECUs --"]
    for e in a.session.ecu_list():
        if e.sa >= 0xF8:
            continue
        L.append(f"  {sa_name(e.sa):<28} {e.frames:>8} frames   first {fmt_elapsed(e.first_seen - a.t0)}"
                 f"   last {fmt_elapsed(e.last_seen - a.t0)}")
    return "\n".join(L) + "\n"


def write_outputs(capture: Capture, system: str = units.METRIC, progress=None) -> _Analysis:
    a = analyze(capture, system, progress)
    write_live_csv(a, os.path.join(capture.folder, LIVE_CSV))
    with open(os.path.join(capture.folder, SUMMARY_TXT), "w") as f:
        f.write(summary_text(a))
    return a
