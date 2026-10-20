"""
Simulation mode — generates realistic fake J1939 traffic for UI testing
without real hardware. Activated when python-can is unavailable or when
the interface is set to "sim".

Simulates:
  - SA 0x00  Engine #1      (EEC1, EEC2, ET1, EFL/P1, HOURS)
  - SA 0x03  Transmission   (TCI, TRF1)
  - SA 0x0B  Instrument     (CCVS, VD)
  - SA 0x17  Cab Controller (VEP1)
  - SA 0x00  DM1 broadcast  (occasional fault, clears after ~15s)
"""

import threading
import time
import math
import random
from typing import Callable, List, Optional

from .can_manager import CANFrame


# ── helpers ────────────────────────────────────────────────────────────────────

def _pack16(val: float, scale: float, offset: float,
            lo: float = 0, hi: float = 0xFFFE) -> tuple:
    raw = int((val - offset) / scale)
    raw = max(0, min(0xFFFE, raw))
    return (raw & 0xFF, (raw >> 8) & 0xFF)


def _pack8(val: float, scale: float, offset: float) -> int:
    raw = int((val - offset) / scale)
    return max(0, min(0xFE, raw))


def _j1939_id(priority: int, pgn: int, sa: int, da: int = 0xFF) -> int:
    pf = (pgn >> 8) & 0xFF
    if pf < 0xF0:
        return (priority << 26) | ((pgn & 0x3FF00) << 8) | (da << 8) | sa
    return (priority << 26) | (pgn << 8) | sa


# ── frame builders ─────────────────────────────────────────────────────────────

def _eec1(rpm: float, torq: float, sa: int = 0x00) -> CANFrame:
    """EEC1 — 0xF004  Engine speed, torque."""
    r = _pack16(rpm, 0.125, 0)
    d = bytes([
        _pack8(torq, 1, -125),   # driver demand %
        _pack8(torq, 1, -125),   # actual torque %
        0xFF,                     # reserved
        r[0], r[1],               # RPM
        sa, 0xFF, 0xFF,
    ])
    return CANFrame(time.time(), _j1939_id(3, 0xF004, sa), d[:8], True)


def _eec2(throttle: float, load: float, sa: int = 0x00) -> CANFrame:
    """EEC2 — 0xF003  Throttle, engine load."""
    d = bytes([
        _pack8(throttle, 0.4, 0),
        0xFF,
        _pack8(load, 1, 0),
        0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    ])
    return CANFrame(time.time(), _j1939_id(3, 0xF003, sa), d, True)


def _et1(coolant_c: float, oil_c: float, sa: int = 0x00) -> CANFrame:
    """ET1 — 0xFEE6  Engine temps."""
    oil = _pack16(oil_c, 0.03125, -273)
    d = bytes([
        _pack8(coolant_c, 1, -40),
        0xFF,
        oil[0], oil[1],
        0xFF, 0xFF, 0xFF, 0xFF,
    ])
    return CANFrame(time.time(), _j1939_id(6, 0xFEE6, sa), d, True)


def _efl_p1(oil_kpa: float, fuel_kpa: float, sa: int = 0x00) -> CANFrame:
    """EFL/P1 — 0xFEE9  Oil pressure, fuel pressure."""
    d = bytes([
        _pack8(fuel_kpa, 4, 0),
        0xFF, 0xFF,
        _pack8(oil_kpa, 4, 0),
        0xFF, 0xFF, 0xFF, 0xFF,
    ])
    return CANFrame(time.time(), _j1939_id(6, 0xFEE9, sa), d, True)


def _ccvs(speed_kmh: float, sa: int = 0x0B) -> CANFrame:
    """CCVS — 0xFEF1  Vehicle speed."""
    s = _pack16(speed_kmh, 1/256, 0)
    d = bytes([0xFF, s[0], s[1], 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
    return CANFrame(time.time(), _j1939_id(6, 0xFEF1, sa), d, True)


def _tci(current: int, selected: int, requested: int,
         sa: int = 0x03) -> CANFrame:
    """TCI — 0xFEFB  Transmission gear info."""
    # Gear encoding: 0=neutral, 1-16=forward, -1 to -4=reverse (offset 125)
    def gear_byte(g):
        return max(0, min(0xFE, g + 125))
    d = bytes([0xFF, 0xFF, 0xFF,
               gear_byte(current), gear_byte(selected), gear_byte(requested),
               0xFF, 0xFF])
    return CANFrame(time.time(), _j1939_id(6, 0xFEFB, sa), d, True)


def _trf1(oil_c: float, sa: int = 0x03) -> CANFrame:
    """TRF1 — 0xFEF9  Trans fluid temp."""
    oil = _pack16(oil_c, 0.03125, -273)
    d   = bytes([oil[0], oil[1], 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
    return CANFrame(time.time(), _j1939_id(6, 0xFEF9, sa), d, True)


def _vep1(batt_v: float, alt_v: float, sa: int = 0x17) -> CANFrame:
    """VEP1 — 0xFEF8  Battery/alternator voltage."""
    b = _pack16(batt_v, 0.05, 0)
    a = _pack16(alt_v,  0.05, 0)
    d = bytes([0xFF, 0xFF, a[0], a[1], b[0], b[1], 0xFF, 0xFF])
    return CANFrame(time.time(), _j1939_id(6, 0xFEF8, sa), d, True)


def _hours(h: float, sa: int = 0x00) -> CANFrame:
    """HOURS — 0xFEE5  Engine total hours."""
    raw = int(h / 0.05)
    raw = min(0xFFFFFFFE, raw)
    d = bytes([raw & 0xFF, (raw >> 8) & 0xFF,
               (raw >> 16) & 0xFF, (raw >> 24) & 0xFF,
               0xFF, 0xFF, 0xFF, 0xFF])
    return CANFrame(time.time(), _j1939_id(6, 0xFEE5, sa), d, True)


def _dm1_no_fault(sa: int) -> CANFrame:
    """DM1 with no active faults."""
    d = bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
    return CANFrame(time.time(), _j1939_id(6, 0xFECA, sa), d, True)


def _dm1_fault(sa: int, spn: int, fmi: int, oc: int = 1) -> CANFrame:
    """DM1 with one active fault — amber warning lamp."""
    b0 = spn & 0xFF
    b1 = (spn >> 8) & 0xFF
    b2 = ((spn >> 11) & 0xE0) | (fmi & 0x1F)
    b3 = oc & 0x7F
    # Lamp byte: amber warning lamp bit 3 = on (value 01 in 2-bit field)
    lamp = 0x08   # amber warning
    d = bytes([lamp, 0xFF, b0, b1, b2, b3, 0xFF, 0xFF])
    return CANFrame(time.time(), _j1939_id(6, 0xFECA, sa), d, True)


# ── Simulator class ────────────────────────────────────────────────────────────

class CANSimulator:
    """
    Drop-in replacement for the live CAN reader.
    Call start() to begin generating frames; register callbacks the same
    way as CANManager.
    """

    def __init__(self):
        self._callbacks: List[Callable] = []
        self._status_cb: Optional[Callable] = None
        self._error_cb:  Optional[Callable] = None
        self._thread:    Optional[threading.Thread] = None
        self._running    = False
        self.connected   = False
        self.interface   = "sim"
        self.bitrate     = 250000
        self.protocol    = "J1939"
        self.frame_count = 0
        self.error_count = 0
        self.fps         = 0.0
        self._fps_count  = 0
        self._fps_time   = time.time()

        # Simulated state
        self._t          = 0.0
        self._hours      = 1842.5
        self._fault_active  = False
        self._fault_timer   = 0.0
        self._fault_spn     = 110   # coolant temp
        self._fault_fmi     = 0     # above range
        self._next_fault_in = random.uniform(8, 20)

    def register_frame_callback(self, cb):
        self._callbacks.append(cb)

    def register_error_callback(self, cb):
        self._error_cb = cb

    def register_status_callback(self, cb):
        self._status_cb = cb

    def connect(self, *args, **kwargs):
        self.connected = True
        self._running  = True
        self._thread   = threading.Thread(
            target=self._loop, daemon=True)
        self._thread.start()
        if self._status_cb:
            self._status_cb("Simulated  sim  250k")
        return True, "Simulation mode active"

    def disconnect(self):
        self._running  = False
        self.connected = False
        self.frame_count = 0
        self.fps = 0.0
        if self._status_cb:
            self._status_cb("Disconnected")

    def send_request(self, pgn: int, sa: int = 0xF9, da: int = 0xFF):
        """Simulate response to DM1 request."""
        if pgn == 0xFECA:
            self._dispatch(_dm1_no_fault(0x00))
            self._dispatch(_dm1_no_fault(0x03))
        return True

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self):
        # 2 Hz loop — 0.5s per tick
        # tick % 2 == 0  →  every 1s
        # tick % 20      →  every 10s
        tick = 0
        while self._running:
            self._t    += 0.5
            tick       += 1
            now         = time.time()

            rpm        = 1800 + 400 * math.sin(self._t * 0.3) + random.gauss(0, 10)
            throttle   = 30  + 20  * math.sin(self._t * 0.2)
            load       = 40  + 25  * math.sin(self._t * 0.25)
            coolant    = 82  + 5   * math.sin(self._t * 0.05)
            oil_eng    = 95  + 3   * math.sin(self._t * 0.04)
            oil_press  = 380 + 20  * math.sin(self._t * 0.1)
            fuel_press = 600 + 10  * math.sin(self._t * 0.15)
            speed      = max(0, 12 + 8 * math.sin(self._t * 0.12))
            oil_trans  = 75  + 4   * math.sin(self._t * 0.06)
            batt_v     = 12.4 + 0.3 * math.sin(self._t * 0.08)
            alt_v      = 13.8 + 0.2 * math.sin(self._t * 0.1)
            gear       = max(1, min(4, int(speed / 4)))
            self._hours += 0.5 / 3600

            # Every tick (2 Hz) — engine speed and vehicle speed
            self._dispatch(_eec1(rpm, load))
            self._dispatch(_ccvs(speed))

            # Every other tick (1 Hz) — everything else
            if tick % 2 == 0:
                self._dispatch(_eec2(throttle, load))
                self._dispatch(_et1(coolant, oil_eng))
                self._dispatch(_efl_p1(oil_press, fuel_press))
                self._dispatch(_vep1(batt_v, alt_v))
                self._dispatch(_trf1(oil_trans))
                self._dispatch(_tci(gear, gear, gear))
                self._dispatch(_dm1_no_fault(0x03))

            # Every 20 ticks (10s) — engine hours
            if tick % 20 == 0:
                self._dispatch(_hours(self._hours))

            # DM1 fault cycle
            if not self._fault_active:
                self._next_fault_in -= 0.5
                if self._next_fault_in <= 0:
                    self._fault_active = True
                    self._fault_timer  = 15.0
                    self._fault_spn    = random.choice([110, 100, 190, 84])
                    self._fault_fmi    = random.choice([0, 3, 4, 7])
                elif tick % 2 == 0:
                    self._dispatch(_dm1_no_fault(0x00))
            else:
                self._fault_timer -= 0.5
                if self._fault_timer <= 0:
                    self._fault_active  = False
                    self._next_fault_in = random.uniform(12, 30)
                elif tick % 2 == 0:
                    self._dispatch(
                        _dm1_fault(0x00, self._fault_spn, self._fault_fmi,
                                   oc=min(127, int(15 - self._fault_timer) + 1)))

            # FPS tracking
            self._fps_count += 1
            if now - self._fps_time >= 1.0:
                self.fps        = self._fps_count / (now - self._fps_time)
                self._fps_count = 0
                self._fps_time  = now

            time.sleep(0.5)   # 2 Hz

    def _dispatch(self, frame: CANFrame):
        self.frame_count += 1
        for cb in self._callbacks:
            try:
                cb(frame)
            except Exception:
                pass
