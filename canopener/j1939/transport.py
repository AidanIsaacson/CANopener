"""J1939-21 transport protocol (multi-packet messages up to 1785 bytes).

``TransportReceiver`` reassembles both broadcast (BAM) and connection-mode (RTS/CTS)
transfers seen on the bus. For RTS/CTS transfers addressed to us it also sends the CTS
and end-of-message ACK so the sender keeps going.
"""

from dataclasses import dataclass, field

from .constants import PGN_TPCM
from .ids import build_id, pgn_bytes, pgn_from_bytes

TP_RTS = 0x10
TP_CTS = 0x11
TP_EOM_ACK = 0x13
TP_BAM = 0x20
TP_ABORT = 0xFF

TP_MAX_SIZE = 1785


@dataclass
class _RxSession:
    pgn: int
    size: int
    packets: int
    max_per_cts: int
    rts: bool
    t_last: float
    parts: dict = field(default_factory=dict)
    next: int = 1
    win_end: int = 0


class TransportReceiver:
    TIMEOUT = 2.5   # seconds without traffic before a half-finished transfer is dropped

    def __init__(self, local_sa: int, send):
        self.local_sa = local_sa
        self._send = send           # send(can_id, data) -> bool
        self.respond = True         # False when decoding a log file
        self.sessions: dict[tuple[int, int], _RxSession] = {}
        self.completed = 0
        self.dropped = 0

    def _cm(self, dest: int, payload: bytes):
        if self.respond:
            self._send(build_id(PGN_TPCM, self.local_sa, dest, priority=7), payload)

    def _send_cts(self, sa: int, sess: _RxSession):
        remaining = sess.packets - sess.next + 1
        n = remaining if sess.max_per_cts == 0xFF else min(remaining, sess.max_per_cts)
        n = max(1, min(n, 255))
        sess.win_end = sess.next + n - 1
        self._cm(sa, bytes([TP_CTS, n, sess.next, 0xFF, 0xFF]) + pgn_bytes(sess.pgn))

    def expire(self, t: float):
        stale = [k for k, s in self.sessions.items() if t - s.t_last > self.TIMEOUT]
        for k in stale:
            del self.sessions[k]
            self.dropped += 1

    def on_cm(self, t: float, sa: int, da: int, d: bytes):
        self.expire(t)
        if len(d) < 8:
            return
        ctrl = d[0]
        if ctrl in (TP_BAM, TP_RTS):
            sess = _RxSession(
                pgn=pgn_from_bytes(d[5:8]), size=d[1] | (d[2] << 8), packets=d[3],
                max_per_cts=d[4], rts=ctrl == TP_RTS, t_last=t,
            )
            if sess.packets == 0 or sess.size > TP_MAX_SIZE:
                return
            sess.win_end = sess.packets
            self.sessions[(sa, da)] = sess
            if sess.rts and da == self.local_sa:
                self._send_cts(sa, sess)
        elif ctrl == TP_ABORT:
            if self.sessions.pop((sa, da), None):
                self.dropped += 1

    def on_dt(self, t: float, sa: int, da: int, d: bytes):
        """Feed a TP.DT frame. Returns (pgn, payload) when a transfer completes."""
        sess = self.sessions.get((sa, da))
        if not sess or len(d) < 2:
            return None
        sess.t_last = t
        seq = d[0]
        if not 1 <= seq <= sess.packets:
            return None
        sess.parts[seq] = bytes(d[1:8])
        ours = sess.rts and da == self.local_sa
        if len(sess.parts) >= sess.packets:
            del self.sessions[(sa, da)]
            buf = b"".join(sess.parts[i] for i in range(1, sess.packets + 1))[:sess.size]
            if ours:
                self._cm(sa, bytes([TP_EOM_ACK, sess.size & 0xFF, sess.size >> 8, sess.packets, 0xFF])
                         + pgn_bytes(sess.pgn))
            self.completed += 1
            return sess.pgn, buf
        if ours and seq >= sess.win_end:
            # Ask for the next window, starting at the first packet we're still missing.
            sess.next = next(i for i in range(1, sess.packets + 1) if i not in sess.parts)
            self._send_cts(sa, sess)
        return None


def split_packets(payload: bytes) -> list[bytes]:
    """Split a payload into TP.DT frames (sequence number + 7 data bytes, 0xFF padded)."""
    out = []
    for i in range(0, len(payload), 7):
        chunk = payload[i:i + 7].ljust(7, b"\xff")
        out.append(bytes([len(out) + 1]) + chunk)
    return out


def bam_header(pgn: int, payload: bytes) -> bytes:
    n = (len(payload) + 6) // 7
    return bytes([TP_BAM, len(payload) & 0xFF, len(payload) >> 8, n, 0xFF]) + pgn_bytes(pgn)


def rts_header(pgn: int, payload: bytes, max_per_cts: int = 0xFF) -> bytes:
    n = (len(payload) + 6) // 7
    return bytes([TP_RTS, len(payload) & 0xFF, len(payload) >> 8, n, max_per_cts]) + pgn_bytes(pgn)
