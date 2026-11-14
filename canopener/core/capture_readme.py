"""``README.md`` in every capture folder: what was logged, why, and what each file holds.

Written for whoever opens the folder next, a person or an AI assistant reading an upload: the
machine, the concern, the question, how it was connected, the timestamps, what the bus said about
itself, and the traffic nothing decoded (usually where "the data doesn't parse" starts).

Rendered from the capture's own state only (``capture.json``, including ``meta["bus"]``, which
``capture_report`` fills in at save), so it's cheap to rewrite on every change, and editing the
machine details of a saved capture needs no re-analysis. Standard library only.
"""

import datetime
import os

from .capture import (
    ADAPTER_TRACE,
    CAPTURE_LOG,
    FULL_LOG,
    FULL_MEASUREMENTS,
    LIVE_CSV,
    MACHINE_FIELDS,
    MARKERS_CSV,
    META_JSON,
    README_MD,
    SAVED,
    SUMMARY_TXT,
    TRAFFIC_CSV,
    Capture,
    fmt_elapsed,
)
from .measure import MEASUREMENTS_CSV

UNDECODED_SHOWN = 60        # rows of undecoded traffic in the README (traffic.csv has them all)

FILES = {
    CAPTURE_LOG: "every CAN frame, candump format: `(epoch seconds) interface ID#DATA` "
                 "(29-bit IDs have 8 hex digits, 11-bit have 3)",
    SUMMARY_TXT: "the timeline: markers, fault codes going active / clearing, lamps, ECUs dropping out",
    LIVE_CSV: "every decoded live value twice a second, with the marker phase it fell in",
    MARKERS_CSV: "the timestamps (wall clock and seconds since the start)",
    TRAFFIC_CSV: "every PGN / source and 11-bit ID: frame count, rate, length, and whether it decoded",
    MEASUREMENTS_CSV: "meter readings (wall-clock times, same clock as capture.log)",
    ADAPTER_TRACE: "the Bluetooth OBD adapter's own text (commands sent, every reply line): on a non-CAN "
                   "vehicle (K-line, J1850) the frames in capture.log are rebuilt from this, so a reply "
                   "that didn't parse only shows here",
    META_JSON: "the capture's state: machine details, markers, connection, analysis summary",
    "kline_probe.txt": "the K-line deep probe: every read-only request tried on this vehicle and each raw reply",
    "kline_probe.json": "the deep probe as data (the last run): every step, and the requests worth reading again",
    "kline_values.csv": "values read again from a K-line module (the requests a deep probe's full scan found): "
                        "time, pass, request, the answer's data bytes in hex",
    "report.txt": "the scan report (fault codes, machine info) from when this was recorded",
    FULL_LOG: "the untrimmed log (this capture was trimmed to the stretch around its timestamps)",
    FULL_MEASUREMENTS: "the untrimmed meter readings",
}


def _when(t: float | None) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "?"


def _md(text) -> str:
    """One table cell: no pipes or line breaks."""
    return str(text).replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def render(c: Capture) -> str:
    title = c.name.strip() or c.machine_summary() or "Capture"
    L = [f"# {_md(title)}", ""]
    if c.state != SAVED:
        L += [f"_Not saved yet ({c.state}): the analysis sections appear when it is._", ""]
    rows = [(label, c.machine[k]) for k, label in MACHINE_FIELDS if c.machine.get(k)]
    if c.started:
        long = fmt_elapsed((c.stopped or c.started) - c.started)[1:]
        rows.append(("Recorded", f"{_when(c.started)} to {_when(c.stopped or c.started)} ({long})"))
    counts = f"{c.frames:,} CAN frames" + (f", {c.samples:,} meter readings" if c.samples else "")
    rows.append(("Recorded data", counts))
    meta = c.meta or {}
    for key, label in (("source", "Connection"), ("interface", "Interface"), ("bitrate", "Bitrate"),
                       ("vehicle_protocol", "Vehicle protocol"), ("profile", "Profile"),
                       ("listen_only", "Listen-only"), ("app_version", "CANopener version"),
                       ("front_end", "Recorded with")):
        if key in meta and meta[key] not in ("", None):
            rows.append((label, meta[key]))
    if meta.get("plugins"):
        rows.append(("Plugins", ", ".join(meta["plugins"])))
    tr = meta.get("trimmed")
    if tr:
        rows.append(("Trimmed", f"{tr.get('before_s', 0) / 60:g} min before the first timestamp to "
                                f"{tr.get('after_s', 0) / 60:g} min after the last"))
    L += ["| | |", "|---|---|"] + [f"| {label} | {_md(v)} |" for label, v in rows] + [""]

    if c.notes.strip():
        L += ["## Customer concern", "", c.notes.strip(), ""]
    if c.question.strip():
        L += ["## What to look at", "", c.question.strip(), ""]

    L += ["## Timestamps", ""]
    if c.markers:
        L += ["| # | Label | Time | Note |", "|---|---|---|---|"]
        for i, m in enumerate(c.markers):
            when = fmt_elapsed(m.t - c.started) if m.tagged and c.started else "not tagged"
            L.append(f"| {i + 1} | {_md(m.label)} | {when} | {_md(m.note)} |")
    else:
        L.append("None.")
    L.append("")

    bus = meta.get("bus") or {}
    if bus:
        ids = bus.get("identity") or []
        quiet = bus.get("quiet") or []
        if quiet:
            L += ["## Nothing answered", ""] + [_md(x) for x in quiet] + [""]
        for label in bus.get("thin") or []:
            L += [f"**{_md(label)} lists no live data at all**, so 'no codes' from it may not be the whole story "
                  "(some engine computers answer OBD-II only as a formality).", ""]
        L += ["## What the bus said about itself", ""]
        L += [f"- {_md(x)}" for x in ids] if ids else [
            "Nothing: no Component ID, VIN or software ID was heard (Read Machine Info asks for them)."]
        L.append("")
        ecus = bus.get("ecus") or []
        if ecus:
            L += ["## Senders", "", "| Source | Frames |", "|---|---|"]
            L += [f"| {_md(e['name'])} | {e['frames']:,} |" for e in ecus] + [""]
        und = bus.get("undecoded") or []
        L += ["## Traffic nothing decoded", ""]
        if und:
            L += [f"{bus.get('undecoded_count', len(und))} of {bus.get('traffic_count', '?')} PGN / ID streams "
                  f"had no decoder ({bus.get('undecoded_frames', 0):,} frames). Busiest first; "
                  f"`traffic.csv` lists every stream.", "",
                  "| ID | Source | Name | Frames | Rate | Bytes | Example data |", "|---|---|---|---|---|---|---|"]
            for r in und[:UNDECODED_SHOWN]:
                rate = f"{r['rate_hz']:.1f} Hz" if r.get("rate_hz") else "once"
                L.append(f"| {r['ident']} | {_md(r['source'])} | {_md(r['name']) or 'unknown'} | {r['frames']:,} "
                         f"| {rate} | {r.get('dlc', '')} | `{r.get('example', '')}` |")
            if len(und) > UNDECODED_SHOWN:
                L.append(f"| ... | {len(und) - UNDECODED_SHOWN} more in traffic.csv | | | | | |")
        elif quiet:
            L.append("Nothing to decode: only this tool's own requests are in the log.")
        else:
            L.append("None: every PGN / ID had a decoder.")
        L.append("")

    present = [n for n in FILES if c.folder and os.path.exists(os.path.join(c.folder, n))]
    extra = sorted(n for n in (os.listdir(c.folder) if c.folder and os.path.isdir(c.folder) else [])
                   if n not in FILES and n != README_MD and not n.startswith(".") and not n.endswith(".tmp"))
    L += ["## Files", ""] + [f"- `{n}`: {FILES[n]}" for n in present] + [f"- `{n}`" for n in extra]
    L.append("")
    return "\n".join(L)


def write_readme(c: Capture):
    if not c.folder or not os.path.isdir(c.folder):
        return
    tmp = os.path.join(c.folder, README_MD + ".tmp")
    with open(tmp, "w") as f:
        f.write(render(c))
    os.replace(tmp, os.path.join(c.folder, README_MD))
