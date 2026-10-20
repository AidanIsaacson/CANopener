"""A simulated telehandler: a Deutz-style engine ECU (SA 00) plus a machine controller (SA 27).

The engine warms up, revs, and throws an intermittent low-oil-pressure code at idle so
the live views and DTC history have something interesting to show. It answers requests
for stored codes, freeze frames, readiness and ID PGNs, using single frames, BAM, or
RTS/CTS exactly like a real ECU, so it exercises the whole receive path.
"""

import math
import random
import threading
import time

from ..can.backends import CanBackend
from ..can.frame import CanFrame
from ..j1939 import fields
from ..j1939.addresses import J1939Name
from ..j1939.constants import (
    DM1,
    DM2,
    DM4,
    DM5,
    DM6,
    DM12,
    GLOBAL_SA,
    PGN_COMPONENT_ID,
    PGN_ECU_ID,
    PGN_ENGINE_HOURS,
    PGN_FUEL_CONSUMPTION,
    PGN_REQUEST,
    PGN_SOFTWARE_ID,
    PGN_TPCM,
)
from ..j1939.dtc import Dtc, encode_dm
from ..j1939.ids import parse_id, pgn_from_bytes
from ..j1939.signals import SIGNALS_BY_PGN
from .ecu import SimEcu

LAMP_OFF = 0x00      # all four lamps off (status bits 00)
LAMP_WARN = 0x04     # amber warning lamp on
FLASH_NONE = 0xFF


class EngineModel:
    """Very rough engine behaviour driven by wall-clock time."""

    def __init__(self, t0: float, rng: random.Random):
        self.t0 = t0
        self.rng = rng
        self.hours0 = 2417.35
        self.fuel_total0 = 18342.5

    def elapsed(self, now=None) -> float:
        return (now or time.time()) - self.t0

    def rpm(self, now=None) -> float:
        t = self.elapsed(now)
        if t < 8:
            return 850 + self.rng.uniform(-8, 8)
        work = max(0.0, math.sin((t - 8) / 6.0)) ** 2
        return 850 + 1450 * work + self.rng.uniform(-10, 10)

    def load(self, rpm: float) -> float:
        return max(0.0, min(100.0, (rpm - 800) / 16 + self.rng.uniform(-2, 2)))

    def coolant(self) -> float:
        return 22 + 66 * (1 - math.exp(-self.elapsed() / 90))

    def oil_pressure(self, rpm: float) -> float:
        # Weak at idle when hot, so SPN 100 goes active / inactive as the engine revs.
        hot_penalty = 0.6 * (self.coolant() - 22)
        return max(40.0, 90 + (rpm - 850) * 0.22 - hot_penalty + self.rng.uniform(-4, 4))

    def hours(self) -> float:
        return self.hours0 + self.elapsed() / 3600


class MachineSimulator:
    ENGINE_SA = 0x00
    CONTROLLER_SA = 0x27

    LOW_OIL = 75.0     # kPa: below this at idle the engine sets SPN 100 FMI 18

    def __init__(self, backend: CanBackend, seed: int | None = None):
        self.backend = backend
        self.rng = random.Random(seed)
        self.model = EngineModel(time.time(), self.rng)
        self._running = False
        self._threads: list[threading.Thread] = []
        self._send_lock = threading.Lock()
        self.tx_errors = 0

        self.oil_low_active = False
        self.oil_low_count = 6
        self.stored = [
            Dtc(100, 18, 6),            # oil pressure low - moderately severe (history)
            Dtc(168, 4, 2),             # battery potential low
            Dtc(97, 3, 1),              # water-in-fuel sensor shorted high
            Dtc(520205, 11, 5),         # manufacturer-specific
        ]
        self.active_engine = [Dtc(3719, 16, 3)]   # DPF soot load moderately high

        self.engine = SimEcu(self.ENGINE_SA, J1939Name.build(
            identity=123456, function=0, industry_group=3, arbitrary_address_capable=True), self._send)
        self.controller = SimEcu(self.CONTROLLER_SA, J1939Name.build(
            identity=4242, function=128, industry_group=3, vehicle_system=1), self._send)
        self.ecus = [self.engine, self.controller]
        self._setup_engine()
        self._setup_controller()

    # ------------------------------------------------------------------
    def _send(self, can_id: int, data: bytes):
        with self._send_lock:
            try:
                self.backend.send(CanFrame(can_id, bytes(data), True, time.time()))
            except OSError:
                self.tx_errors += 1

    @staticmethod
    def _pack(pgn: int, values: dict) -> bytes:
        buf = bytearray(b"\xff" * 8)
        for sig in SIGNALS_BY_PGN[pgn]:
            if sig.name in values:
                sig.encode_into(buf, values[sig.name])
        return bytes(buf)

    def _engine_active_dtcs(self) -> list[Dtc]:
        rpm = self._rpm
        pressure = self.model.oil_pressure(rpm)
        if pressure < self.LOW_OIL and not self.oil_low_active:
            self.oil_low_active = True
            self.oil_low_count = min(126, self.oil_low_count + 1)
        elif pressure > self.LOW_OIL + 15:
            self.oil_low_active = False
        dtcs = list(self.active_engine)
        if self.oil_low_active:
            dtcs.append(Dtc(100, 18, self.oil_low_count))
        return dtcs

    def _setup_engine(self):
        e, m = self.engine, self.model
        self._rpm = m.rpm()

        def eec1():
            self._rpm = m.rpm()
            torque = min(100, max(0, (self._rpm - 800) / 15))
            return self._pack(0xF004, {"Engine speed": self._rpm, "Actual engine torque": torque})

        def eec2():
            return self._pack(0xF003, {"Accelerator pedal 1": max(0, (self._rpm - 850) / 14.5),
                                       "Load at current speed": m.load(self._rpm)})

        def et1():
            c = m.coolant()
            return self._pack(0xFEEE, {"Coolant temp": c, "Fuel temp": c * 0.55 + 10, "Oil temp": c + 6})

        def eflp1():
            return self._pack(0xFEEF, {"Fuel delivery pressure": 380, "Oil level": 88,
                                       "Oil pressure": m.oil_pressure(self._rpm), "Coolant level": 100})

        def ic1():
            boost = 101 + max(0, self._rpm - 900) * 0.09
            return self._pack(0xFEF6, {"DPF inlet pressure": 3 + boost * 0.05, "Boost pressure": boost,
                                       "Intake manifold temp": 32 + m.coolant() * 0.15,
                                       "Air inlet pressure": 99, "Air filter diff. pressure": 1.2,
                                       "Exhaust gas temp": 180 + self._rpm * 0.12})

        def vep1():
            return self._pack(0xFEF7, {"Charging system voltage": 14.1 + self.rng.uniform(-0.05, 0.05),
                                       "Battery voltage": 13.9 + self.rng.uniform(-0.05, 0.05),
                                       "Keyswitch voltage": 13.8})

        def amb():
            return self._pack(0xFEF5, {"Barometric pressure": 98.5, "Ambient air temp": 18.0,
                                       "Air inlet temp": 21})

        def lfe():
            return self._pack(0xFEF2, {"Fuel rate": 1.8 + max(0, self._rpm - 850) * 0.009})

        def at1s():
            return self._pack(0xFD7B, {"DPF soot load": 82, "DPF ash load": 14})

        def dd():
            return self._pack(0xFEFC, {"Fuel level": 61.6})

        def def_tank():
            return self._pack(0xFE56, {"DEF tank level": 47.2, "DEF tank temp": 15})

        def dm1():
            dtcs = self._engine_active_dtcs()
            return encode_dm(LAMP_WARN if dtcs else LAMP_OFF, FLASH_NONE, dtcs)

        e.every(0xF004, 0.02, eec1, priority=3)
        e.every(0xF003, 0.05, eec2, priority=3)
        e.every(0xFEEE, 1.0, et1)
        e.every(0xFEEF, 0.5, eflp1)
        e.every(0xFEF6, 0.5, ic1)
        e.every(0xFEF7, 1.0, vep1)
        e.every(0xFEF5, 1.0, amb)
        e.every(0xFEF2, 0.1, lfe)
        e.every(0xFD7B, 1.0, at1s)
        e.every(0xFEFC, 1.0, dd)
        e.every(0xFE56, 1.0, def_tank)
        e.every(DM1, 1.0, dm1)

        e.respond(DM1, dm1)
        e.respond(DM2, lambda: encode_dm(LAMP_OFF, FLASH_NONE, self.stored))
        e.respond(DM6, lambda: encode_dm(LAMP_OFF, FLASH_NONE, []))
        e.respond(DM12, lambda: encode_dm(LAMP_OFF, FLASH_NONE, []))
        e.respond(DM4, self._dm4)
        e.respond(DM5, lambda: bytes([len(self._engine_active_dtcs()), len(self.stored), 5]) + b"\xff" * 5)
        e.respond(PGN_COMPONENT_ID, lambda: b"DEUTZ*TCD3.6 L4*SIM00012345*GTH-SIM*")
        e.respond(PGN_SOFTWARE_ID, lambda: b"\x02EMR4 SIM 1.0.0*BOOT 2.1*")
        e.respond(PGN_ECU_ID, lambda: b"04512345*SIM-ECU-0001*ENGINE*EMR4*DEUTZ*")
        e.respond(PGN_ENGINE_HOURS, lambda: fields.encode(m.hours(), 4, 0.05)
                  + fields.encode(m.hours() * 60 * 1650, 4, 1000))
        e.respond(PGN_FUEL_CONSUMPTION, lambda: fields.encode(41.5, 4, 0.5)
                  + fields.encode(m.fuel_total0 + m.elapsed() * 0.001, 4, 0.5))

    def _dm4(self) -> bytes:
        dtc = Dtc(100, 18, self.oil_low_count)
        body = dtc.raw() + bytes([0x00]) + fields.encode(101, 1, 2) + fields.encode(850, 2, 0.125) \
            + fields.encode(18, 1) + fields.encode(87, 1, 1, -40) + fields.encode(0.0, 2, 1 / 256) \
            + bytes([0x3C, 0x00])
        return bytes([len(body)]) + body

    def _setup_controller(self):
        c, m = self.controller, self.model
        counter = [0]

        def ccvs():
            speed = 0.0 if m.elapsed() < 8 else max(0.0, 12 * math.sin((m.elapsed() - 8) / 10))
            buf = bytearray(b"\xff" * 8)
            SIGNALS_BY_PGN[0xFEF1][0].encode_into(buf, speed)
            return bytes(buf)

        def vf():
            return self._pack(0xFE68, {"Hydraulic oil temp": 25 + 30 * (1 - math.exp(-m.elapsed() / 150))})

        def prop_fast():
            counter[0] = (counter[0] + 1) & 0xFF
            boom = int(512 + 400 * math.sin(m.elapsed() / 7)) & 0xFFFF
            return bytes([counter[0], boom & 0xFF, boom >> 8, 0x00, 0x10, 0x00, 0xFF, 0xFF])

        def prop_slow():
            return bytes([0x01, 0x00, int(m.elapsed()) & 0xFF, 0xA5, 0x00, 0x00, 0xFF, 0xFF])

        no_faults = lambda: encode_dm(LAMP_OFF, FLASH_NONE, [])   # noqa: E731
        c.every(0xFEF1, 0.1, ccvs)
        c.every(0xFE68, 1.0, vf)
        c.every(0xFF10, 0.1, prop_fast)
        c.every(0xFF22, 0.5, prop_slow)
        c.every(DM1, 1.0, no_faults)
        c.respond(DM1, no_faults)
        c.respond(DM2, lambda: encode_dm(LAMP_OFF, FLASH_NONE, [Dtc(522001, 5, 1)]))
        c.respond(PGN_COMPONENT_ID, lambda: b"SIMULATED*MACHINE CTRL*SIM-0042*")
        c.respond(PGN_SOFTWARE_ID, lambda: b"\x01CTRL 3.4.2*")

    # ------------------------------------------------------------------
    def start(self):
        self.backend.open()
        self._running = True
        for ecu in self.ecus:
            ecu.claim_address()
        for target, name in ((self._tx_loop, "sim-tx"), (self._rx_loop, "sim-rx")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._running = False
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()
        try:
            self.backend.close()
        except OSError:
            pass

    @property
    def running(self) -> bool:
        return self._running

    def _tx_loop(self):
        while self._running:
            now = time.time()
            for ecu in self.ecus:
                ecu.tick(now)
            time.sleep(0.005)

    def _rx_loop(self):
        while self._running:
            try:
                fr = self.backend.recv()
            except OSError:
                time.sleep(0.2)
                continue
            if fr is None or not fr.extended or fr.error:
                continue
            jid = parse_id(fr.can_id)
            if jid.pgn == PGN_REQUEST and len(fr.data) >= 3:
                pgn = pgn_from_bytes(fr.data[:3])
                for ecu in self.ecus:
                    if jid.da in (GLOBAL_SA, ecu.sa):
                        ecu.on_request(pgn, jid.sa, directed=jid.da != GLOBAL_SA)
            elif jid.pgn == PGN_TPCM:
                for ecu in self.ecus:
                    if jid.da == ecu.sa:
                        ecu.on_tpcm(jid.sa, fr.data)
