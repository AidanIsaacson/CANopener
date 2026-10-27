"""Simulated ECU: periodic broadcasts, request handling and transport-protocol sending."""

import queue
import threading
import time
from dataclasses import dataclass

from ..j1939.addresses import J1939Name
from ..j1939.constants import GLOBAL_SA, PGN_ACK, PGN_ADDRESS_CLAIM, PGN_TPCM, PGN_TPDT
from ..j1939.ids import build_id, pgn_bytes
from ..j1939.transport import TP_ABORT, TP_CTS, TP_EOM_ACK, bam_header, rts_header, split_packets


@dataclass
class Broadcast:
    pgn: int
    period: float
    build: object          # callable() -> bytes
    priority: int = 6
    next_due: float = 0.0


class SimEcu:
    BAM_GAP = 0.02         # seconds between BAM packets (J1939 says 50-200 ms; faster is fine here)
    T3 = 1.25              # wait for CTS / EOM ACK

    def __init__(self, sa: int, name: J1939Name, send):
        self.sa = sa
        self.name = name
        self._send = send                  # send(can_id, data)
        self.broadcasts: list[Broadcast] = []
        self.responders: dict[int, object] = {}   # pgn -> callable() -> bytes | None
        self.commands: dict[int, object] = {}     # pgn -> callable() -> bool: requests that do something (DM11 / DM3)
        self._tp_lock = threading.Lock()   # one multi-packet transfer at a time per ECU
        self._cts: dict[int, queue.Queue] = {}

    def every(self, pgn: int, period: float, build, priority: int = 6):
        self.broadcasts.append(Broadcast(pgn, period, build, priority))

    def respond(self, pgn: int, build):
        self.responders[pgn] = build

    def command(self, pgn: int, run):
        """A request that does something instead of returning data (J1939-73 DM11 / DM3 clears)."""
        self.commands[pgn] = run

    # --- tx helpers ---
    def send_pgn(self, pgn: int, data: bytes, da: int = GLOBAL_SA, priority: int = 6):
        if len(data) <= 8:
            self._send(build_id(pgn, self.sa, da, priority), data)
        elif da == GLOBAL_SA:
            threading.Thread(target=self._send_bam, args=(pgn, data), daemon=True).start()
        else:
            threading.Thread(target=self._send_rts, args=(pgn, data, da), daemon=True).start()

    def claim_address(self):
        self._send(build_id(PGN_ADDRESS_CLAIM, self.sa, GLOBAL_SA, 6), self.name.to_bytes())

    def nack(self, pgn: int, requester: int):
        self.ack(pgn, requester, control=1)

    def ack(self, pgn: int, requester: int, control: int = 0):
        """Acknowledgment (PGN 59392): 0 ACK, 1 NACK, 2 access denied, 3 cannot respond."""
        self._send(build_id(PGN_ACK, self.sa, GLOBAL_SA, 6),
                   bytes([control, 0xFF, 0xFF, 0xFF, requester]) + pgn_bytes(pgn))

    def _send_bam(self, pgn: int, data: bytes):
        with self._tp_lock:
            self._send(build_id(PGN_TPCM, self.sa, GLOBAL_SA, 7), bam_header(pgn, data))
            for pkt in split_packets(data):
                time.sleep(self.BAM_GAP)
                self._send(build_id(PGN_TPDT, self.sa, GLOBAL_SA, 7), pkt)

    def _send_rts(self, pgn: int, data: bytes, da: int):
        with self._tp_lock:
            q: queue.Queue = queue.Queue()
            self._cts[da] = q
            try:
                packets = split_packets(data)
                self._send(build_id(PGN_TPCM, self.sa, da, 7), rts_header(pgn, data))
                while True:
                    try:
                        ctrl, d = q.get(timeout=self.T3)
                    except queue.Empty:
                        self._send(build_id(PGN_TPCM, self.sa, da, 7),
                                   bytes([TP_ABORT, 3, 0xFF, 0xFF, 0xFF]) + pgn_bytes(pgn))
                        return
                    if ctrl == TP_EOM_ACK or ctrl == TP_ABORT:
                        return
                    if ctrl == TP_CTS:
                        n, start = d[1], d[2]
                        for seq in range(start, min(start + n, len(packets) + 1)):
                            self._send(build_id(PGN_TPDT, self.sa, da, 7), packets[seq - 1])
                            time.sleep(0.002)
            finally:
                self._cts.pop(da, None)

    # --- rx ---
    def on_request(self, pgn: int, requester: int, directed: bool):
        if pgn == PGN_ADDRESS_CLAIM:
            self.claim_address()
            return
        run = self.commands.get(pgn)
        if run is not None:
            done = run()
            if directed:                  # J1939-73: a clear sent to everyone isn't acknowledged
                self.ack(pgn, requester, control=0 if done else 1)
            return
        build = self.responders.get(pgn)
        data = build() if build else None
        if data is None:
            if directed:                  # J1939-21: no NACK in reply to a global request
                self.nack(pgn, requester)
            return
        self.send_pgn(pgn, data, requester if directed else GLOBAL_SA)

    def on_tpcm(self, sender: int, d: bytes):
        q = self._cts.get(sender)
        if q is not None and d:
            q.put((d[0], d))

    def tick(self, now: float):
        for b in self.broadcasts:
            if now >= b.next_due:
                # Keep a steady cadence, but don't try to catch up after a long stall.
                b.next_due = b.next_due + b.period if now - b.next_due < b.period else now + b.period
                data = b.build()
                if data is not None:
                    self.send_pgn(b.pgn, data, GLOBAL_SA, b.priority)
