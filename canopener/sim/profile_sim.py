"""A simulator built from a machine profile: every signal the profile describes, broadcast from
its source address with values that move, so a profile can be exercised without the machine.

* One simulated ECU per source address (signals with no ``sa`` come from address 00). Each
  claims its address and broadcasts DM1.
* Each PGN is broadcast every 100 ms (1 s for multi-packet ones, sent as BAM).
* Values: enum signals step through their states, 1-bit flags toggle, signals with a normal
  range swing slowly inside it (occasionally just outside, so out-of-range highlighting shows),
  anything else wanders over the lower part of its raw range.
* ``active_codes``: (sa, Dtc) pairs to report in DM1, e.g. to try out a profile's fault text.

It's an exerciser for decoding and display, not a model of any real machine: the values
don't relate to each other the way real ones do. Recorded logs (Replay) are the real thing.
"""

import math
import random
import threading
import time

from ..can.backends import CanBackend
from ..can.frame import CanFrame
from ..j1939.addresses import J1939Name
from ..j1939.constants import DM1, GLOBAL_SA, PGN_REQUEST, PGN_TPCM
from ..j1939.dtc import Dtc, encode_dm
from ..j1939.ids import parse_id, pgn_from_bytes
from ..j1939.signals import Signal
from .ecu import SimEcu

FAST, SLOW = 0.1, 1.0


class _Value:
    """Generates a slowly varying value for one signal."""

    def __init__(self, sig: Signal, rng: random.Random):
        self.sig = sig
        self.rng = rng
        self.phase = rng.uniform(0, 2 * math.pi)
        self.period = rng.uniform(8, 40)

    def __call__(self, t: float):
        s = self.sig
        if s.enum:
            keys = [k for k, _ in s.enum]
            raw = keys[int(t / 4 + self.phase) % len(keys)]
            return raw * s.scale + s.offset
        if s.width_bits == 1:
            return float(int(t / self.period * 4 + self.phase) % 2)
        wave = (math.sin(2 * math.pi * t / self.period + self.phase) + 1) / 2      # 0..1
        if s.low is not None and s.high is not None and s.high > s.low:
            span = s.high - s.low
            return s.low - 0.05 * span + wave * 1.1 * span     # a little past both ends at the peaks
        bits = s.width_bits
        top = (1 << bits) - 1 if bits < 8 else {8: 0xFA, 16: 0xFAFF, 32: 0xFAFFFFFF}.get(bits, (1 << bits) - 2)
        if s.signed:
            raw = (wave - 0.5) * (1 << (bits - 1)) * 0.5
        else:
            raw = (0.1 + 0.4 * wave) * (top - (1 if bits >= 2 else 0))
        return raw * s.scale + s.offset


class ProfileSimulator:
    def __init__(self, backend: CanBackend, profile, *, seed: int | None = None,
                 active_codes: list[tuple[int, Dtc]] | None = None):
        if not profile.signals:
            raise ValueError(f"profile '{profile.name}' has no signals to simulate")
        self.backend = backend
        self.profile = profile
        self.rng = random.Random(seed)
        self.t0 = time.time()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._send_lock = threading.Lock()
        self.active_codes = list(active_codes or [])
        self.ecus: dict[int, SimEcu] = {}
        self._layout: dict[tuple[int, int], list[tuple[Signal, _Value]]] = {}
        for sig in profile.signals:
            sa = sig.sa[0] if isinstance(sig.sa, tuple) else (0x00 if sig.sa is None else sig.sa)
            self._layout.setdefault((sa, sig.pgn), []).append((sig, _Value(sig, self.rng)))
        for (sa, pgn), sigs in self._layout.items():
            ecu = self._ecu(sa)
            size = max(8, max(s.start + (s.bit + s.width_bits + 7) // 8 for s, _ in sigs))
            build = self._builder(sa, pgn, size)
            ecu.every(pgn, SLOW if size > 8 else FAST, build)
            ecu.respond(pgn, build)
        for sa, ecu in self.ecus.items():
            ecu.every(DM1, 1.0, self._dm1_builder(sa))
            ecu.respond(DM1, self._dm1_builder(sa))

    def _ecu(self, sa: int) -> SimEcu:
        if sa not in self.ecus:
            name = J1939Name.build(identity=0x1000 + sa, function=0 if sa == 0 else 128, industry_group=5,
                                   arbitrary_address_capable=False)
            self.ecus[sa] = SimEcu(sa, name, self._send)
        return self.ecus[sa]

    def _builder(self, sa: int, pgn: int, size: int):
        sigs = self._layout[(sa, pgn)]

        def build():
            t = time.time() - self.t0
            buf = bytearray(b"\xff" * size)
            for sig, gen in sigs:
                try:
                    sig.encode_into(buf, gen(t))
                except (ValueError, OverflowError):
                    pass
            return bytes(buf)
        return build

    def _dm1_builder(self, sa: int):
        def build():
            dtcs = [d for s, d in self.active_codes if s == sa]
            return encode_dm(0x04 if dtcs else 0x00, 0xFF, dtcs)
        return build

    def _send(self, can_id: int, data: bytes):
        with self._send_lock:
            try:
                self.backend.send(CanFrame(can_id, bytes(data), True, time.time()))
            except OSError:
                pass

    # ------------------------------------------------------------------
    def start(self):
        self.backend.open()
        self._running = True
        for ecu in self.ecus.values():
            ecu.claim_address()
        for target, name in ((self._tx_loop, "psim-tx"), (self._rx_loop, "psim-rx")):
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
            for ecu in list(self.ecus.values()):
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
                for ecu in self.ecus.values():
                    if jid.da in (GLOBAL_SA, ecu.sa):
                        ecu.on_request(pgn, jid.sa, directed=jid.da != GLOBAL_SA)
            elif jid.pgn == PGN_TPCM:
                for ecu in self.ecus.values():
                    if jid.da == ecu.sa:
                        ecu.on_tpcm(jid.sa, fr.data)
