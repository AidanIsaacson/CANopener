#!/usr/bin/env python3
"""
J1939 Code Reader
=================
Reads active (DM1) and stored (DM2) fault codes from a J1939 bus, plus whatever
else the ECUs will give up: freeze frames (DM4), readiness counts (DM5),
pending/emissions codes, component/software IDs, engine hours, a live data
snapshot, a bus survey, and raw proprietary frames.

Built for the Deutz EMR4 on a Genie GTH-1056, works on any J1939 machine.

Requirements (Linux Mint / Ubuntu):
    sudo apt install python3-tk can-utils
Uses the kernel SocketCAN interface directly (no python-can needed).
Works with gs_usb / candleLight / 8devices USB2CAN / PEAK etc. — anything that shows up as can0.

Run:
    python3 j1939_codes.py

READ-ONLY: the tool only sends J1939 Requests (PGN 59904) and transport-protocol
flow control (CTS / end-of-message ACK). It never sends DM3/DM11 clears or writes.
Tick "Listen-only" to send nothing at all (DM1 still works since it's broadcast).
"""

import datetime
import os
import queue
import re
import socket
import struct
import subprocess
import threading
import time
from collections import deque

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
TOOL_SA = 0xF9          # Off-board diagnostic tool #1
REQUEST_DLC = 3         # J1939-21 request length. Set to 8 if an ECU ignores short requests.

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
FRAME_FMT = "=IB3x8s"

PGN_ACK, PGN_REQUEST, PGN_TPDT, PGN_TPCM = 0xE800, 0xEA00, 0xEB00, 0xEC00
DM1, DM2, DM4, DM5, DM6, DM12, DM23 = 0xFECA, 0xFECB, 0xFECD, 0xFECE, 0xFECF, 0xFED4, 0xFDB5

DTC_PGNS = {DM1: "Active", DM2: "Stored", DM6: "Pending", DM12: "Emis. active", DM23: "Emis. stored"}
STATUS_ORDER = {"Active": 0, "Pending": 1, "Emis. active": 2, "Stored": 3, "Emis. stored": 4}

PGN_LABEL = {
    DM1: "DM1 active codes", DM2: "DM2 stored codes", DM4: "DM4 freeze frames",
    DM5: "DM5 diagnostic readiness", DM6: "DM6 pending codes",
    DM12: "DM12 emissions-related active", DM23: "DM23 emissions-related previously active",
}
INFO_PGNS = {
    0xFEEB: "Component ID", 0xFEDA: "Software ID", 0xFDC5: "ECU ID",
    0xFEEC: "VIN", 0xFEE5: "Engine hours", 0xFEE9: "Fuel consumption",
}
PGN_LABEL.update(INFO_PGNS)

SA_NAMES = {
    0x00: "Engine #1", 0x01: "Engine #2", 0x03: "Transmission #1", 0x0B: "Brakes",
    0x17: "Instrument cluster", 0x19: "Climate control", 0x21: "Body controller",
    0x27: "Management computer", 0x31: "Cab controller", 0x3D: "Exhaust emission ctrl",
    0x55: "DPF controller", 0xF9: "Off-board tool #1", 0xFA: "Off-board tool #2",
}

FMI_TEXT = {
    0: "Above normal range (most severe)",
    1: "Below normal range (most severe)",
    2: "Data erratic, intermittent or incorrect",
    3: "Voltage above normal / shorted high",
    4: "Voltage below normal / shorted low",
    5: "Current below normal / open circuit",
    6: "Current above normal / grounded circuit",
    7: "Mechanical system not responding / out of adj.",
    8: "Abnormal frequency, pulse width or period",
    9: "Abnormal update rate",
    10: "Abnormal rate of change",
    11: "Root cause not known",
    12: "Bad intelligent device or component",
    13: "Out of calibration",
    14: "Special instructions",
    15: "Above normal (least severe)",
    16: "Above normal (moderately severe)",
    17: "Below normal (least severe)",
    18: "Below normal (moderately severe)",
    19: "Received network data in error",
    20: "Data drifted high",
    21: "Data drifted low",
    31: "Condition exists",
}

# Common standard SAE SPNs. Names are the usual J1939 descriptions; Deutz-specific
# codes live in the proprietary range and need the Deutz EMR4 fault list.
SPN_NAMES = {
    27: "EGR valve position", 51: "Throttle position", 84: "Wheel-based vehicle speed",
    91: "Accelerator pedal position 1", 94: "Fuel delivery pressure", 97: "Water in fuel",
    98: "Engine oil level", 100: "Engine oil pressure", 102: "Intake manifold pressure (boost)",
    105: "Intake manifold temperature", 106: "Air inlet pressure",
    107: "Air filter differential pressure", 108: "Barometric pressure",
    110: "Engine coolant temperature", 111: "Coolant level", 157: "Fuel rail pressure",
    158: "Keyswitch battery potential", 164: "Injection control pressure",
    168: "Battery potential / power input", 171: "Ambient air temperature",
    172: "Air inlet temperature", 174: "Fuel temperature", 175: "Engine oil temperature",
    190: "Engine speed", 237: "VIN", 412: "EGR temperature", 513: "Actual engine torque",
    558: "Accelerator pedal low-idle switch", 626: "Start enable device (intake heater)",
    627: "ECU power supply", 629: "Controller #1 (ECU internal)", 630: "Calibration memory",
    636: "Camshaft position sensor", 637: "Crankshaft position/timing sensor",
    639: "J1939 network #1", 651: "Injector cylinder 1", 652: "Injector cylinder 2",
    653: "Injector cylinder 3", 654: "Injector cylinder 4", 655: "Injector cylinder 5",
    656: "Injector cylinder 6", 677: "Starter motor relay", 723: "Engine speed sensor #2",
    729: "Inlet air heater driver", 1136: "ECU temperature",
    1347: "Fuel pump pressurizing assembly (metering unit)", 1569: "Engine protection torque derate",
    2659: "EGR mass flow rate", 2791: "EGR valve control", 3031: "DEF tank temperature",
    3216: "Aftertreatment intake NOx", 3226: "Aftertreatment outlet NOx",
    3242: "DPF intake temperature", 3246: "DPF outlet temperature",
    3251: "DPF differential pressure", 3361: "DEF dosing unit", 3364: "DEF quality",
    3464: "Throttle actuator control command", 3516: "DEF concentration",
    3719: "DPF soot load", 4334: "DEF doser pressure", 4364: "SCR conversion efficiency",
    4765: "DOC intake temperature", 4766: "DOC outlet temperature",
    5246: "SCR operator inducement severity",
}


def spn_name(spn):
    if spn in SPN_NAMES:
        return SPN_NAMES[spn]
    if 516096 <= spn <= 524287:
        return "Manufacturer-specific (Deutz/OEM) - check EMR4 fault list"
    return "(not in built-in table)"


def sa_name(sa):
    return f"{sa:02X} {SA_NAMES.get(sa, '')}".strip()


# ----------------------------------------------------------------------------
# Field helpers (return None for J1939 'not available' / 'error' values)
# ----------------------------------------------------------------------------
def s8(d, i, scale=1.0, off=0.0):
    if i >= len(d) or d[i] > 0xFA:
        return None
    return d[i] * scale + off


def s16(d, i, scale=1.0, off=0.0):
    if i + 1 >= len(d):
        return None
    v = d[i] | (d[i + 1] << 8)
    return None if v > 0xFAFF else v * scale + off


def s32(d, i, scale=1.0, off=0.0):
    if i + 3 >= len(d):
        return None
    v = d[i] | (d[i + 1] << 8) | (d[i + 2] << 16) | (d[i + 3] << 24)
    return None if v > 0xFAFFFFFF else v * scale + off


def fmt(v, unit, nd=1):
    return "n/a" if v is None else f"{v:.{nd}f} {unit}"


def hexs(d):
    return " ".join(f"{b:02X}" for b in d)


def parse_id(can_id):
    dp = (can_id >> 24) & 0x3          # EDP + DP
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    sa = can_id & 0xFF
    if pf < 240:                       # PDU1: PS is destination address
        return (dp << 16) | (pf << 8), sa, ps
    return (dp << 16) | (pf << 8) | ps, sa, 0xFF


# ----------------------------------------------------------------------------
# DM decoding
# ----------------------------------------------------------------------------
def lamp_str(b0, b1):
    out = []
    for name, sh in (("MIL", 6), ("Stop", 4), ("Warn", 2), ("Protect", 0)):
        st, fl = (b0 >> sh) & 3, (b1 >> sh) & 3
        if st == 1:
            out.append(f"{name}:ON" + {0: "(slow flash)", 1: "(fast flash)"}.get(fl, ""))
        elif st == 0:
            out.append(f"{name}:off")
        else:
            out.append(f"{name}:n/a")
    return " ".join(out)


def decode_dtc(b):
    if len(b) < 4 or b in (b"\xff\xff\xff\xff", b"\x00\x00\x00\x00"):
        return None
    spn = b[0] | (b[1] << 8) | ((b[2] & 0xE0) << 11)
    fmi = b[2] & 0x1F
    cm = b[3] >> 7
    oc = b[3] & 0x7F
    if (spn == 0 and fmi == 0) or spn == 0x7FFFF:
        return None
    # CM=1 means the ECU uses an old (pre-version-4) SPN byte order; show the alternate too.
    alt = ((b[0] << 11) | (b[1] << 3) | (b[2] >> 5)) if cm else None
    return {"spn": spn, "fmi": fmi, "oc": None if oc == 0x7F else oc, "cm": cm, "alt": alt}


def decode_dm(data):
    lamps = lamp_str(data[0], data[1]) if len(data) >= 2 else ""
    dtcs = []
    for i in range(2, len(data) - 3, 4):
        d = decode_dtc(bytes(data[i:i + 4]))
        if d:
            dtcs.append(d)
    return lamps, dtcs


def decode_dm4(data):
    """Freeze frames: [len][DTC x4][torque mode][boost][rpm x2][load][coolant][vspeed x2][mfr...]"""
    frames, i = [], 0
    while i < len(data):
        ln = data[i]
        if ln in (0, 0xFF) or i + 1 + ln > len(data):
            break
        body = bytes(data[i + 1:i + 1 + ln])
        i += 1 + ln
        dtc = decode_dtc(body[0:4])
        if not dtc:
            continue
        frames.append({
            "dtc": dtc,
            "torque_mode": (body[4] & 0x0F) if len(body) > 4 else None,
            "boost": s8(body, 5, 2),
            "rpm": s16(body, 6, 0.125),
            "load": s8(body, 8, 1),
            "coolant": s8(body, 9, 1, -40),
            "vspeed": s16(body, 10, 1 / 256),
            "extra": body[12:],
        })
    return frames


def decode_ascii(d, skip_first=False):
    raw = bytes(d[1:] if skip_first else d)
    s = raw.decode("ascii", "replace")
    parts = [p.strip().strip("\x00\xff") for p in s.split("*")]
    parts = [p for p in parts if p and all(32 <= ord(c) < 127 for c in p)]
    return " | ".join(parts) if parts else f"(raw) {hexs(raw)}"


# (pgn, label, decoder, unit)
LIVE = [
    (0xF004, "Engine speed", lambda d: s16(d, 3, 0.125), "rpm"),
    (0xF004, "Actual engine torque", lambda d: s8(d, 2, 1, -125), "%"),
    (0xF003, "Accelerator pedal 1", lambda d: s8(d, 1, 0.4), "%"),
    (0xF003, "Load at current speed", lambda d: s8(d, 2, 1), "%"),
    (0xFEEE, "Coolant temp", lambda d: s8(d, 0, 1, -40), "°C"),
    (0xFEEE, "Fuel temp", lambda d: s8(d, 1, 1, -40), "°C"),
    (0xFEEE, "Oil temp", lambda d: s16(d, 2, 0.03125, -273), "°C"),
    (0xFEEF, "Fuel delivery pressure", lambda d: s8(d, 0, 4), "kPa"),
    (0xFEEF, "Oil level", lambda d: s8(d, 2, 0.4), "%"),
    (0xFEEF, "Oil pressure", lambda d: s8(d, 3, 4), "kPa"),
    (0xFEEF, "Coolant level", lambda d: s8(d, 7, 0.4), "%"),
    (0xFEF6, "Boost pressure", lambda d: s8(d, 1, 2), "kPa"),
    (0xFEF6, "Intake manifold temp", lambda d: s8(d, 2, 1, -40), "°C"),
    (0xFEF6, "Air inlet pressure", lambda d: s8(d, 3, 2), "kPa"),
    (0xFEF6, "Air filter diff. pressure", lambda d: s8(d, 4, 0.05), "kPa"),
    (0xFEF6, "Exhaust gas temp", lambda d: s16(d, 5, 0.03125, -273), "°C"),
    (0xFEF7, "Charging system voltage", lambda d: s16(d, 2, 0.05), "V"),
    (0xFEF7, "Battery voltage", lambda d: s16(d, 4, 0.05), "V"),
    (0xFEF7, "Keyswitch voltage", lambda d: s16(d, 6, 0.05), "V"),
    (0xFEF5, "Barometric pressure", lambda d: s8(d, 0, 0.5), "kPa"),
    (0xFEF5, "Ambient air temp", lambda d: s16(d, 3, 0.03125, -273), "°C"),
    (0xFEF5, "Air inlet temp", lambda d: s8(d, 5, 1, -40), "°C"),
    (0xFEF2, "Fuel rate", lambda d: s16(d, 0, 0.05), "L/h"),
    (0xFEFC, "Fuel level", lambda d: s8(d, 1, 0.4), "%"),
    (0xFE56, "DEF tank level", lambda d: s8(d, 0, 0.4), "%"),
    (0xFE56, "DEF tank temp", lambda d: s8(d, 1, 1, -40), "°C"),
]


# ----------------------------------------------------------------------------
# J1939 link: socket, receive thread, transport protocol, storage
# ----------------------------------------------------------------------------
class J1939Link:
    def __init__(self):
        self.sock = None
        self.channel = None
        self.running = False
        self.listen_only = False
        self.lock = threading.Lock()
        self.latest = {}        # (pgn, sa) -> (t, bytes)
        self.stats = {}         # (sa, pgn) -> [count, first_t, last_t]
        self.dtc_hist = {}      # (pgn, sa, spn, fmi) -> dict
        self.acks = []          # (t, sa, control, pgn)
        self.raw = deque(maxlen=500000)
        self.sessions = {}
        self.frames = 0
        self.err = None
        self.tx_err = None

    # --- socket ---
    def open(self, channel):
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind((channel,))
        s.settimeout(0.2)
        self.sock, self.channel, self.running = s, channel, True
        threading.Thread(target=self._rx_loop, daemon=True).start()

    def close(self):
        self.running = False
        time.sleep(0.3)
        if self.sock:
            self.sock.close()
            self.sock = None

    def _rx_loop(self):
        while self.running:
            try:
                frame = self.sock.recv(16)
            except socket.timeout:
                continue
            except OSError as e:
                self.err = str(e)
                time.sleep(0.5)
                continue
            self.err = None
            can_id, dlc, data = struct.unpack(FRAME_FMT, frame)
            if can_id & (CAN_ERR_FLAG | CAN_RTR_FLAG):
                continue
            ext = bool(can_id & CAN_EFF_FLAG)
            self.feed(time.time(), can_id & CAN_EFF_MASK, data[:min(dlc, 8)], ext, live=True)

    def send(self, can_id, data):
        if self.listen_only or not self.sock:
            return False
        frame = struct.pack(FRAME_FMT, can_id | CAN_EFF_FLAG, len(data), bytes(data).ljust(8, b"\xff"))
        try:
            self.sock.send(frame)
            self.tx_err = None
            return True
        except OSError as e:
            self.tx_err = str(e)
            return False

    def request(self, pgn, da=0xFF):
        data = bytes([pgn & 0xFF, (pgn >> 8) & 0xFF, (pgn >> 16) & 0xFF])
        if REQUEST_DLC == 8:
            data = data + b"\xff" * 5
        return self.send(0x18EA0000 | (da << 8) | TOOL_SA, data)

    # --- frame processing ---
    def feed(self, t, can_id, data, ext=True, live=True):
        data = bytes(data)
        self.raw.append((t, can_id, ext, data))
        self.frames += 1
        if not ext:
            return
        pgn, sa, da = parse_id(can_id)
        with self.lock:
            st = self.stats.get((sa, pgn))
            if st:
                st[0] += 1
                st[2] = t
            else:
                self.stats[(sa, pgn)] = [1, t, t]
        if pgn == PGN_TPCM:
            self._tpcm(t, sa, da, data, live)
        elif pgn == PGN_TPDT:
            self._tpdt(t, sa, da, data, live)
        elif pgn == PGN_ACK:
            if len(data) >= 8:
                self.acks.append((t, sa, data[0], data[5] | (data[6] << 8) | (data[7] << 16)))
            self._store(t, pgn, sa, data)
        else:
            self._store(t, pgn, sa, data)

    def _store(self, t, pgn, sa, data):
        with self.lock:
            self.latest[(pgn, sa)] = (t, data)
            if pgn in DTC_PGNS:
                lamps, dtcs = decode_dm(data)
                for d in dtcs:
                    key = (pgn, sa, d["spn"], d["fmi"])
                    h = self.dtc_hist.get(key)
                    if h:
                        h.update(last=t, oc=d["oc"], lamps=lamps)
                        h["seen"] += 1
                    else:
                        self.dtc_hist[key] = dict(d, first=t, last=t, lamps=lamps, seen=1)

    def _send_cts(self, sa, sess):
        remaining = sess["packets"] - sess["next"] + 1
        n = remaining if sess["max"] == 0xFF else min(remaining, sess["max"])
        n = max(1, min(n, 255))
        sess["win_end"] = sess["next"] + n - 1
        p = sess["pgn"]
        self.send(0x1CEC0000 | (sa << 8) | TOOL_SA,
                  bytes([0x11, n, sess["next"], 0xFF, 0xFF, p & 0xFF, (p >> 8) & 0xFF, (p >> 16) & 0xFF]))

    def _tpcm(self, t, sa, da, d, live):
        if len(d) < 8:
            return
        ctrl = d[0]
        if ctrl in (0x20, 0x10):     # BAM or RTS
            sess = {
                "pgn": d[5] | (d[6] << 8) | (d[7] << 16),
                "size": d[1] | (d[2] << 8), "packets": d[3], "max": d[4],
                "parts": {}, "rts": ctrl == 0x10, "next": 1, "win_end": d[3],
            }
            self.sessions[(sa, da)] = sess
            if sess["rts"] and da == TOOL_SA and live:
                self._send_cts(sa, sess)
        elif ctrl == 0xFF:            # abort
            self.sessions.pop((sa, da), None)

    def _tpdt(self, t, sa, da, d, live):
        sess = self.sessions.get((sa, da))
        if not sess or len(d) < 2:
            return
        seq = d[0]
        sess["parts"][seq] = d[1:8]
        if len(sess["parts"]) >= sess["packets"]:
            try:
                buf = b"".join(sess["parts"][i] for i in range(1, sess["packets"] + 1))
            except KeyError:
                self.sessions.pop((sa, da), None)
                return
            buf = buf[:sess["size"]]
            self.sessions.pop((sa, da), None)
            if sess["rts"] and da == TOOL_SA and live:
                p = sess["pgn"]
                self.send(0x1CEC0000 | (sa << 8) | TOOL_SA,
                          bytes([0x13, sess["size"] & 0xFF, sess["size"] >> 8, sess["packets"], 0xFF,
                                 p & 0xFF, (p >> 8) & 0xFF, (p >> 16) & 0xFF]))
            self._store(t, sess["pgn"], sa, buf)
        elif sess["rts"] and da == TOOL_SA and live and seq >= sess["win_end"]:
            sess["next"] = seq + 1
            self._send_cts(sa, sess)

    def snapshot_since(self, t0):
        with self.lock:
            return {k: v for k, v in self.latest.items() if v[0] >= t0}


# ----------------------------------------------------------------------------
# Report building
# ----------------------------------------------------------------------------
def build_results(link, t0, requested, got, offline=False, full=False):
    rows = []
    with link.lock:
        hist = [h for k, h in link.dtc_hist.items() if h["last"] >= t0 and k[0] in requested]
        hist_keys = [k for k, h in link.dtc_hist.items() if h["last"] >= t0 and k[0] in requested]

    for (pgn, sa, spn, fmi), h in zip(hist_keys, hist):
        spn_s = str(spn) + (f" (alt {h['alt']})" if h["alt"] is not None else "")
        rows.append((DTC_PGNS[pgn], sa_name(sa), spn_s, spn_name(spn), str(fmi),
                     FMI_TEXT.get(fmi, "Reserved"), "n/a" if h["oc"] is None else str(h["oc"]),
                     h["lamps"]))

    # ECUs that answered a DM request but reported nothing
    with_codes = {(k[0], k[1]) for k in hist_keys}
    for (pgn, sa), (t, d) in got.items():
        if pgn in DTC_PGNS and pgn in requested and (pgn, sa) not in with_codes:
            lamps, _ = decode_dm(d)
            rows.append((DTC_PGNS[pgn], sa_name(sa), "-", "No codes reported", "", "", "", lamps))

    rows.sort(key=lambda r: (STATUS_ORDER.get(r[0], 9), r[1], r[2]))

    L = []
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L.append(f"=== {'Log file decode' if offline else 'Live read'} — {when} ===")
    if not offline and link.listen_only:
        L.append("Listen-only mode: no requests sent, only broadcast data (DM1, live data) shown.")
    if link.tx_err:
        L.append(f"!! Transmit error: {link.tx_err} (nothing ACKing on the bus? wrong bitrate?)")
    L.append("")

    # Responses per requested PGN
    L.append("-- Responses --")
    acks = [a for a in link.acks if a[0] >= t0]
    ack_txt = {0: "ACK", 1: "NACK (not supported)", 2: "Access denied", 3: "Cannot respond (busy)"}
    for p in requested:
        sas = sorted(sa for (pg, sa) in got if pg == p)
        label = PGN_LABEL.get(p, f"PGN {p:04X}")
        if sas:
            L.append(f"  {label:<40} from {', '.join(sa_name(s) for s in sas)}")
        else:
            note = "not in log" if offline else "no response"
            na = [a for a in acks if a[3] == p]
            if na:
                note += "; " + ", ".join(f"{sa_name(a[1])}: {ack_txt.get(a[2], a[2])}" for a in na)
            L.append(f"  {label:<40} {note}")
    L.append("")

    # Lamps
    dm1 = {sa: d for (pg, sa), (t, d) in got.items() if pg == DM1}
    if dm1:
        L.append("-- Warning lamps (from DM1) --")
        for sa, d in sorted(dm1.items()):
            L.append(f"  {sa_name(sa):<28} {lamp_str(d[0], d[1]) if len(d) >= 2 else 'n/a'}")
        L.append("")

    # DM5
    for (pg, sa), (t, d) in sorted(got.items()):
        if pg == DM5 and len(d) >= 3:
            a = "n/a" if d[0] == 0xFF else d[0]
            b = "n/a" if d[1] == 0xFF else d[1]
            L.append(f"-- DM5 readiness {sa_name(sa)}: {a} active, {b} previously active, "
                     f"OBD compliance 0x{d[2]:02X}")
    L.append("")

    # Freeze frames
    for (pg, sa), (t, d) in sorted(got.items()):
        if pg != DM4:
            continue
        frames = decode_dm4(d)
        L.append(f"-- Freeze frames (DM4) from {sa_name(sa)} --")
        if not frames:
            L.append(f"  none / undecodable: {hexs(d)}")
        for f in frames:
            dt = f["dtc"]
            L.append(f"  SPN {dt['spn']} FMI {dt['fmi']} ({spn_name(dt['spn'])}), OC {dt['oc']}")
            L.append(f"     rpm {fmt(f['rpm'], 'rpm', 0)}, load {fmt(f['load'], '%', 0)}, "
                     f"coolant {fmt(f['coolant'], '°C', 0)}, boost {fmt(f['boost'], 'kPa', 0)}, "
                     f"speed {fmt(f['vspeed'], 'km/h')}, torque mode {f['torque_mode']}")
            if f["extra"]:
                L.append(f"     manufacturer data: {hexs(f['extra'])}")
        L.append("")

    # Machine info
    info = [(p, sa, d) for (p, sa), (t, d) in sorted(got.items()) if p in INFO_PGNS]
    if info:
        L.append("-- Machine / ECU info --")
        for p, sa, d in info:
            if p == 0xFEE5:
                val = f"{fmt(s32(d, 0, 0.05), 'h', 2)}, {fmt(s32(d, 4, 1000), 'revs', 0)}"
            elif p == 0xFEE9:
                val = f"total fuel used {fmt(s32(d, 4, 0.5), 'L', 1)}"
            elif p == 0xFEDA:
                val = decode_ascii(d, skip_first=True)
            else:
                val = decode_ascii(d)
            L.append(f"  {INFO_PGNS[p]:<18} [{sa_name(sa)}] {val}")
        L.append("")

    if full or offline:
        # Live snapshot
        L.append("-- Live data snapshot --")
        any_live = False
        for pgn, label, fn, unit in LIVE:
            src = [(sa, d) for (p, sa), (t, d) in got.items() if p == pgn]
            src.sort(key=lambda x: x[0] != 0x00)          # prefer engine SA 00
            if src:
                sa, d = src[0]
                v = fn(d)
                if v is not None:
                    any_live = True
                    L.append(f"  {label:<28} {fmt(v, unit)}   [{sa_name(sa)}]")
        if not any_live:
            L.append("  (no standard live PGNs seen — key on / engine running?)")
        L.append("")

        # Bus survey
        L.append("-- Bus survey (source addresses and PGNs seen this session) --")
        with link.lock:
            stats = dict(link.stats)
        by_sa = {}
        for (sa, pgn), (n, f0, f1) in stats.items():
            by_sa.setdefault(sa, []).append((pgn, n, f0, f1))
        for sa in sorted(by_sa):
            L.append(f"  {sa_name(sa)}")
            for pgn, n, f0, f1 in sorted(by_sa[sa]):
                rate = f"{n / (f1 - f0):.1f}/s" if f1 > f0 else ""
                lbl = PGN_LABEL.get(pgn, "")
                L.append(f"      PGN {pgn:05X} ({pgn:>6})  x{n:<6} {rate:>8}  {lbl}")
        L.append("")

        # Proprietary
        prop = [(p, sa, d) for (p, sa), (t, d) in sorted(got.items())
                if 0xFF00 <= p <= 0xFFFF or p == 0xEF00]
        if prop:
            L.append("-- Proprietary frames (latest payload) --")
            for p, sa, d in prop:
                L.append(f"  PGN {p:05X} from {sa_name(sa):<24} {hexs(d)}")
            L.append("")

    return rows, "\n".join(L)


# ----------------------------------------------------------------------------
# Log file import (candump -l format or candump screen format)
# ----------------------------------------------------------------------------
LOG_RE = re.compile(r"\((\d+\.\d+)\)\s+\S+\s+([0-9A-Fa-f]{3,8})#([0-9A-Fa-f]*)\s*$")
SCREEN_RE = re.compile(r"([0-9A-Fa-f]{3,8})\s+\[(\d+)\]\s+([0-9A-Fa-f ]*)")


def load_log(path):
    link = J1939Link()
    n = 0
    with open(path, "r", errors="replace") as f:
        for i, line in enumerate(f):
            m = LOG_RE.search(line)
            if m:
                t, ids, hx = float(m.group(1)), m.group(2), m.group(3)
                data = bytes.fromhex(hx)
            else:
                m = SCREEN_RE.search(line)
                if not m:
                    continue
                t, ids = i * 0.001, m.group(1)
                data = bytes.fromhex("".join(m.group(3).split()))[:int(m.group(2))]
            link.feed(t, int(ids, 16), data, ext=len(ids) == 8, live=False)
            n += 1
    return link, n


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class App:
    COLS = ("Status", "Source", "SPN", "Description", "FMI", "FMI meaning", "Count", "Lamps")
    WIDTHS = (90, 150, 110, 300, 40, 260, 50, 260)

    def __init__(self, root):
        self.root = root
        self.link = None
        self.q = queue.Queue()
        self.last_rows, self.last_text = [], ""
        self.busy = False
        root.title("J1939 Code Reader")
        root.geometry("1280x800")

        top = ttk.Frame(root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Interface").pack(side="left")
        self.ch_var = tk.StringVar(value="can0")
        ttk.Entry(top, textvariable=self.ch_var, width=8).pack(side="left", padx=4)
        ttk.Label(top, text="Bitrate").pack(side="left")
        self.br_var = tk.StringVar(value="250000")
        ttk.Combobox(top, textvariable=self.br_var, width=9,
                     values=["250000", "500000", "125000", "1000000"]).pack(side="left", padx=4)
        self.lo_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Listen-only", variable=self.lo_var).pack(side="left", padx=6)
        ttk.Button(top, text="Set Up Interface", command=self.setup_iface).pack(side="left", padx=4)
        self.conn_btn = ttk.Button(top, text="Connect", command=self.toggle_connect)
        self.conn_btn.pack(side="left", padx=4)
        self.status = ttk.Label(top, text="Not connected", foreground="gray")
        self.status.pack(side="left", padx=12)

        btns = ttk.Frame(root, padding=(6, 0, 6, 6))
        btns.pack(fill="x")
        style = ttk.Style()
        style.configure("Big.TButton", font=("TkDefaultFont", 12, "bold"), padding=8)
        self.action_btns = [
            ttk.Button(btns, text="Read Active Codes", style="Big.TButton",
                       command=lambda: self.run_read([DM1], 2.5)),
            ttk.Button(btns, text="Read Stored Codes", style="Big.TButton",
                       command=lambda: self.run_read([DM2], 2.5)),
            ttk.Button(btns, text="Full Scan", style="Big.TButton",
                       command=lambda: self.run_read([DM1, DM2, DM6, DM12, DM23, DM4, DM5], 4.0, full=True)),
        ]
        for b in self.action_btns:
            b.pack(side="left", padx=4)
        ttk.Button(btns, text="Save Report…", command=self.save_report).pack(side="right", padx=4)
        ttk.Button(btns, text="Decode Log File…", command=self.open_log).pack(side="right", padx=4)

        pane = ttk.PanedWindow(root, orient="vertical")
        pane.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        tf = ttk.Frame(pane)
        self.tree = ttk.Treeview(tf, columns=self.COLS, show="headings", height=10)
        for c, w in zip(self.COLS, self.WIDTHS):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, stretch=c in ("Description", "FMI meaning", "Lamps"))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("Active", background="#ffd6d6")
        self.tree.tag_configure("Stored", background="#fff3cc")
        self.tree.tag_configure("none", foreground="gray")
        pane.add(tf, weight=1)

        self.text = ScrolledText(pane, font=("Monospace", 10), wrap="none")
        pane.add(self.text, weight=2)

        self.root.after(200, self.poll)

    # --- interface ---
    def setup_iface(self):
        ch, br = self.ch_var.get().strip(), self.br_var.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+", ch) or not br.isdigit():
            messagebox.showerror("Setup", "Bad interface name or bitrate.")
            return
        was = self.link is not None
        if was:
            self.disconnect()
        lo = "on" if self.lo_var.get() else "off"
        cmd = (f"ip link set {ch} down; ip link set {ch} type can bitrate {br} listen-only {lo} "
               f"&& ip link set {ch} up")
        try:
            r = subprocess.run(["pkexec", "sh", "-c", cmd], capture_output=True, text=True, timeout=90)
        except Exception as e:
            messagebox.showerror("Setup", f"Could not run pkexec: {e}\n\nRun manually:\nsudo sh -c '{cmd}'")
            return
        if r.returncode != 0:
            messagebox.showerror("Setup", f"Failed:\n{r.stderr.strip()}\n\n"
                                          "If it complains about listen-only, untick it and retry.")
            return
        self.set_status(f"{ch} up at {br} bps (listen-only {lo})", "green")
        if was:
            self.connect()

    def toggle_connect(self):
        self.disconnect() if self.link else self.connect()

    def connect(self):
        ch = self.ch_var.get().strip()
        link = J1939Link()
        try:
            link.open(ch)
        except AttributeError:
            messagebox.showerror("Connect", "This Python has no SocketCAN support (Linux only).")
            return
        except OSError as e:
            messagebox.showerror("Connect", f"Could not open {ch}: {e}\n\n"
                                            "Is the dongle plugged in? Try 'Set Up Interface' first.")
            return
        self.link = link
        self.conn_btn.configure(text="Disconnect")
        self.set_status(f"Connected to {ch}", "green")

    def disconnect(self):
        if self.link:
            self.link.close()
        self.link = None
        self.conn_btn.configure(text="Connect")
        self.set_status("Not connected", "gray")

    def set_status(self, msg, color="black"):
        self.status.configure(text=msg, foreground=color)

    # --- actions ---
    def run_read(self, pgns, listen, full=False):
        if not self.link:
            messagebox.showinfo("Not connected", "Connect to the CAN interface first.")
            return
        if self.busy:
            return
        self.busy = True
        for b in self.action_btns:
            b.state(["disabled"])
        self.set_status("Reading…", "blue")
        link = self.link
        link.listen_only = self.lo_var.get()

        def work():
            try:
                t0 = time.time()
                req = list(pgns) + (list(INFO_PGNS) if full else [])
                sent = False
                for p in req:
                    sent |= link.request(p, 0xFF)
                    time.sleep(0.05)
                time.sleep(listen)
                got = link.snapshot_since(t0)
                missing = [p for p in req if not any(k[0] == p for k in got)]
                if missing and sent:       # retry directed at the engine ECU
                    for p in missing:
                        link.request(p, 0x00)
                        time.sleep(0.05)
                    time.sleep(2.0)
                    got = link.snapshot_since(t0)
                self.q.put(("result",) + build_results(link, t0, req, got, full=full))
            except Exception as e:
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def open_log(self):
        path = filedialog.askopenfilename(title="Open candump log",
                                          filetypes=[("CAN logs", "*.log *.txt"), ("All files", "*")])
        if not path:
            return
        try:
            link, n = load_log(path)
        except Exception as e:
            messagebox.showerror("Log", str(e))
            return
        req = list(DTC_PGNS) + [DM4, DM5] + list(INFO_PGNS)
        rows, text = build_results(link, 0, req, link.snapshot_since(0), offline=True)
        text = f"Source: {path} ({n} frames)\n" + text
        self.show(rows, text)
        self.set_status(f"Decoded {n} frames from log", "green")

    def save_report(self):
        if not self.last_text:
            messagebox.showinfo("Save", "Nothing to save yet.")
            return
        d = os.path.expanduser("~/j1939_reports")
        os.makedirs(d, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(initialdir=d, initialfile=f"report_{stamp}.txt",
                                            defaultextension=".txt")
        if not path:
            return
        lines = ["FAULT CODES", "-" * 100]
        for r in self.last_rows:
            lines.append(f"{r[0]:<13} {r[1]:<26} SPN {r[2]:<14} FMI {r[4]:<3} OC {r[6]:<4} "
                         f"{r[3]} — {r[5]}   [{r[7]}]")
        if not self.last_rows:
            lines.append("(none)")
        lines += ["", self.last_text]
        with open(path, "w") as f:
            f.write("\n".join(lines))
        saved = [path]
        if self.link and self.link.raw:
            logpath = os.path.splitext(path)[0] + ".log"
            ch = self.link.channel or "can0"
            with open(logpath, "w") as f:
                for t, cid, ext, data in list(self.link.raw):
                    ids = f"{cid:08X}" if ext else f"{cid:03X}"
                    f.write(f"({t:.6f}) {ch} {ids}#{data.hex().upper()}\n")
            saved.append(logpath)
        messagebox.showinfo("Saved", "Saved:\n" + "\n".join(saved))

    # --- display ---
    def show(self, rows, text):
        self.last_rows, self.last_text = rows, text
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            tag = "none" if r[2] == "-" else ("Active" if r[0] == "Active" else "Stored")
            self.tree.insert("", "end", values=r, tags=(tag,))
        if not rows:
            self.tree.insert("", "end", values=("", "", "", "No DM responses received", "", "", "", ""),
                             tags=("none",))
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self.busy = False
                for b in self.action_btns:
                    b.state(["!disabled"])
                if msg[0] == "result":
                    self.show(msg[1], msg[2])
                    n = sum(1 for r in msg[1] if r[2] != "-")
                    self.set_status(f"Done — {n} code(s)", "green")
                else:
                    self.set_status("Error", "red")
                    messagebox.showerror("Read failed", msg[1])
        except queue.Empty:
            pass
        if self.link and not self.busy:
            if self.link.err:
                self.set_status(f"Bus error: {self.link.err}", "red")
            elif self.status.cget("text").startswith(("Connected", "Listening", "Bus error")):
                with self.link.lock:
                    sas = len({k[0] for k in self.link.stats})
                self.set_status(f"Listening — {self.link.frames} frames, {sas} source address(es)", "green")
        self.root.after(500, self.poll)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
