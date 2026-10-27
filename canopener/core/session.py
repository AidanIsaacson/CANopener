"""A J1939 session: one bus connection plus everything learned from it.

The receive thread feeds frames into ``Session.feed``; the GUI or CLI reads the collected
state (latest payload per PGN/source, DTC history, statistics, raw frames) under ``lock``.
Log files are decoded by creating a Session with no backend and feeding it frames.

READ-ONLY by design: the receive and scan code only ever transmits J1939 Requests (PGN 59904)
and transport-protocol flow control (CTS / end-of-message ACK). The one request that changes a
machine, a DM11 / DM3 code clear, lives in ``core.j1939_clear`` and is sent only after the user
confirms. ``send_frame()`` exists for the OBD-II / UDS clients and separately installed plugins
(see ``canopener.plugins``); it obeys listen-only like everything else.
"""

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from ..can.backends import CanBackend
from ..can.frame import CanFrame
from ..can.logfile import LogWriter
from ..j1939.addresses import J1939Name
from ..j1939.constants import (
    DTC_PGNS,
    GLOBAL_SA,
    PGN_ACK,
    PGN_ADDRESS_CLAIM,
    PGN_REQUEST,
    PGN_TPCM,
    PGN_TPDT,
    TOOL_SA,
)
from ..j1939.dtc import Lamps, decode_dm
from ..j1939.ids import build_id, parse_id, pgn_bytes, pgn_from_bytes
from ..j1939.transport import TransportReceiver

ACK_TEXT = {0: "ACK", 1: "NACK (not supported)", 2: "Access denied", 3: "Cannot respond (busy)"}


@dataclass
class PgnStats:
    count: int
    first: float
    last: float
    dlc: int = 0

    @property
    def rate(self) -> float:
        return (self.count - 1) / (self.last - self.first) if self.last > self.first else 0.0


@dataclass
class DtcEntry:
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
    seen: int = 1

    @property
    def status(self) -> str:
        return DTC_PGNS.get(self.pgn, f"PGN {self.pgn:04X}")


@dataclass(frozen=True)
class Ack:
    t: float
    sa: int
    control: int
    pgn: int

    @property
    def text(self) -> str:
        return ACK_TEXT.get(self.control, f"control {self.control}")


@dataclass
class EcuInfo:
    sa: int
    name: J1939Name | None = None
    claimed_at: float | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    frames: int = 0


@dataclass
class BusHealth:
    rx_errors: int = 0
    last_error: str | None = None
    last_error_t: float = 0.0
    tx_error: str | None = None
    link_error: str | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=200))


class Session:
    def __init__(self, backend: CanBackend | None = None, *, tool_sa: int = TOOL_SA,
                 raw_maxlen: int = 250_000, request_dlc: int = 3):
        self.backend = backend
        self.tool_sa = tool_sa
        self.request_dlc = request_dlc       # 3 per J1939-21; set 8 if an ECU ignores short requests
        self.listen_only = backend is None or not backend.can_transmit
        self.lock = threading.RLock()

        self.latest: dict[tuple[int, int], tuple[float, bytes]] = {}   # (pgn, sa) -> (t, data)
        self.stats: dict[tuple[int, int], PgnStats] = {}              # (sa, pgn) -> stats
        self.dtc_hist: dict[tuple[int, int, int, int], DtcEntry] = {}  # (pgn, sa, spn, fmi)
        self.ecus: dict[int, EcuInfo] = {}
        self.acks: deque[Ack] = deque(maxlen=2000)
        self.raw: deque[CanFrame] = deque(maxlen=raw_maxlen)
        self.frame_count = 0        # total frames appended to ``raw`` (rx + tx)
        self.rx_count = 0
        self.tx_count = 0
        self.bytes_count = 0
        self.last_rx_t = 0.0        # wall clock of the last good received frame (bus "alive" check)
        self.health = BusHealth()
        self.started_at = time.time()

        self.tp = TransportReceiver(tool_sa, self._send_raw)
        self.tp.respond = backend is not None
        self._recorder: LogWriter | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._listeners: list = []

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def offline(self) -> bool:
        return self.backend is None

    def start(self):
        if self.backend is None:
            raise RuntimeError("Session has no backend (offline log decode)")
        self.backend.open()
        self.started_at = time.time()
        self._running = True
        self._thread = threading.Thread(target=self._rx_loop, name="can-rx", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self.backend:
            try:
                self.backend.close()
            except OSError:
                pass
        self.stop_recording()

    def _rx_loop(self):
        while self._running:
            try:
                frame = self.backend.recv()
            except OSError as e:
                self.health.link_error = str(e)
                time.sleep(0.5)
                continue
            self.health.link_error = None
            if frame is not None:
                self.feed(frame)

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------
    def start_recording(self, path: str):
        self.stop_recording()
        self._recorder = LogWriter(path, self.backend.channel if self.backend else "can0")

    def stop_recording(self) -> tuple[str, int] | None:
        rec, self._recorder = self._recorder, None
        if rec:
            rec.close()
            return rec.path, rec.count
        return None

    @property
    def recording(self) -> LogWriter | None:
        return self._recorder

    # ------------------------------------------------------------------
    # transmit
    # ------------------------------------------------------------------
    def _send_raw(self, can_id: int, data: bytes) -> bool:
        return self.send_frame(CanFrame(can_id, bytes(data), True))

    def send_frame(self, frame: CanFrame) -> bool:
        """Transmit any frame (11- or 29-bit) for a plugin. Returns False in listen-only mode,
        with no backend, or if the bus refused it (see ``health.tx_error``)."""
        if self.listen_only or self.backend is None:
            return False
        frame = CanFrame(frame.can_id, bytes(frame.data), frame.extended, time.time(), tx=True)
        try:
            self.backend.send(frame)
        except OSError as e:
            self.health.tx_error = str(e)
            return False
        self.health.tx_error = None
        self.tx_count += 1
        self._log_frame(frame)
        return True

    def add_listener(self, fn):
        """Call ``fn(frame)`` for every received frame, on the receive thread. Keep it quick."""
        if fn not in self._listeners:
            self._listeners.append(fn)

    def remove_listener(self, fn):
        if fn in self._listeners:
            self._listeners.remove(fn)

    def request(self, pgn: int, da: int = GLOBAL_SA) -> bool:
        data = pgn_bytes(pgn)
        if self.request_dlc == 8:
            data += b"\xff" * 5
        return self._send_raw(build_id(PGN_REQUEST, self.tool_sa, da, priority=6), data)

    # ------------------------------------------------------------------
    # receive
    # ------------------------------------------------------------------
    def _log_frame(self, frame: CanFrame):
        with self.lock:
            self.raw.append(frame)
            self.frame_count += 1
        if self._recorder:
            self._recorder.write(frame)

    def feed(self, frame: CanFrame):
        if frame.error:
            h = self.health
            h.rx_errors += 1
            h.last_error, h.last_error_t = frame.error, frame.timestamp
            h.history.append((frame.timestamp, frame.error))
            self._log_frame(frame)
            return
        self.rx_count += 1
        self.bytes_count += len(frame.data)
        self.last_rx_t = time.time()
        self._log_frame(frame)
        for fn in list(self._listeners):
            try:
                fn(frame)
            except Exception:           # a broken plugin must not stop the receive thread
                pass
        if not frame.extended:
            return
        t, data = frame.timestamp, bytes(frame.data)
        jid = parse_id(frame.can_id)
        pgn, sa, da = jid.pgn, jid.sa, jid.da
        with self.lock:
            st = self.stats.get((sa, pgn))
            if st:
                st.count += 1
                st.last = t
                st.dlc = len(data)
            else:
                self.stats[(sa, pgn)] = PgnStats(1, t, t, len(data))
            ecu = self.ecus.get(sa)
            if ecu is None:
                ecu = self.ecus[sa] = EcuInfo(sa, first_seen=t)
            ecu.last_seen = t
            ecu.frames += 1

        if pgn == PGN_TPCM:
            with self.lock:
                self.tp.on_cm(t, sa, da, data)
        elif pgn == PGN_TPDT:
            with self.lock:
                done = self.tp.on_dt(t, sa, da, data)
            if done:
                with self.lock:     # count reassembled messages under their own PGN too
                    st = self.stats.get((sa, done[0]))
                    if st:
                        st.count, st.last, st.dlc = st.count + 1, t, len(done[1])
                    else:
                        self.stats[(sa, done[0])] = PgnStats(1, t, t, len(done[1]))
                self._store(t, done[0], sa, done[1])
        elif pgn == PGN_ACK:
            if len(data) >= 8:
                with self.lock:
                    self.acks.append(Ack(t, sa, data[0], pgn_from_bytes(data[5:8])))
            self._store(t, pgn, sa, data)
        elif pgn == PGN_ADDRESS_CLAIM:
            if len(data) >= 8:
                with self.lock:
                    self.ecus[sa].name = J1939Name.from_bytes(data)
                    self.ecus[sa].claimed_at = t
            self._store(t, pgn, sa, data)
        else:
            self._store(t, pgn, sa, data)

    def _store(self, t: float, pgn: int, sa: int, data: bytes):
        with self.lock:
            self.latest[(pgn, sa)] = (t, data)
            if pgn in DTC_PGNS:
                lamps, dtcs = decode_dm(data)
                for d in dtcs:
                    key = (pgn, sa, d.spn, d.fmi)
                    h = self.dtc_hist.get(key)
                    if h:
                        h.last, h.oc, h.lamps = t, d.oc, lamps
                        h.seen += 1
                    else:
                        self.dtc_hist[key] = DtcEntry(pgn, sa, d.spn, d.fmi, d.oc, d.cm, d.alt_spn,
                                                      lamps, t, t)

    # ------------------------------------------------------------------
    # queries (thread-safe snapshots)
    # ------------------------------------------------------------------
    def snapshot_since(self, t0: float) -> dict[tuple[int, int], tuple[float, bytes]]:
        with self.lock:
            return {k: v for k, v in self.latest.items() if v[0] >= t0}

    def latest_for(self, pgn: int) -> list[tuple[int, float, bytes]]:
        """All sources of a PGN as (sa, t, data), engine (SA 00) first."""
        with self.lock:
            out = [(sa, t, d) for (p, sa), (t, d) in self.latest.items() if p == pgn]
        out.sort(key=lambda x: (x[0] != 0x00, x[0]))
        return out

    def frames_since(self, seq: int) -> tuple[int, list[CanFrame]]:
        """Frames appended after sequence number ``seq``. Returns (new_seq, frames)."""
        with self.lock:
            total = self.frame_count
            n = min(total - seq, len(self.raw))
            if n <= 0:
                return total, []
            if n >= len(self.raw):
                return total, list(self.raw)
            new = list(itertools.islice(reversed(self.raw), n))     # only the new ones, not the whole buffer
            new.reverse()
            return total, new

    def acks_since(self, t0: float) -> list[Ack]:
        with self.lock:
            return [a for a in self.acks if a.t >= t0]

    def known_ecus(self) -> list[int]:
        """Source addresses heard from, excluding tools / null, busiest first."""
        with self.lock:
            ecus = [e for e in self.ecus.values() if e.sa < 0xF8]
        ecus.sort(key=lambda e: -e.frames)
        return [e.sa for e in ecus]

    def current_lamps(self, max_age: float = 5.0) -> dict[int, Lamps]:
        """Lamp status per ECU from the latest DM1 (only DM1s newer than ``max_age`` seconds)."""
        now = time.time() if not self.offline else float("inf")
        out = {}
        for sa, t, d in self.latest_for(0xFECA):
            if self.offline or now - t <= max_age:
                out[sa] = decode_dm(d)[0]
        return out

    def ecu_list(self) -> list[EcuInfo]:
        with self.lock:
            return sorted((EcuInfo(**vars(e)) for e in self.ecus.values()), key=lambda e: e.sa)

    def stats_copy(self) -> dict[tuple[int, int], PgnStats]:
        with self.lock:
            return {k: PgnStats(v.count, v.first, v.last, v.dlc) for k, v in self.stats.items()}

    def describe(self) -> str:
        if self.backend is None:
            return "offline"
        return self.backend.describe()
