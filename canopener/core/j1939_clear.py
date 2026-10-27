"""Clearing J1939 fault codes: DM11 (active codes) and DM3 (previously active / stored codes).

SAE J1939-73: a clear is a Request (PGN 59904) for the DM11 or DM3 PGN, sent to one module or to
every module (the global address). A module asked directly answers with an Acknowledgment
(ACK, or NACK / access denied / busy when it won't); a request to every module isn't answered,
so read the codes again to see what changed.

This **changes the machine**: every front-end shows ``CLEAR_WARNING`` and asks first (default
No), and nothing is sent in listen-only mode. Codes whose fault is still present come back.
"""

import time
from dataclasses import dataclass, field

from ..j1939.addresses import sa_name
from ..j1939.constants import DM3, DM11, GLOBAL_SA
from .session import Ack, Session

ACTIVE, STORED, BOTH = "active", "stored", "both"
PGNS = {ACTIVE: [DM11], STORED: [DM3], BOTH: [DM11, DM3]}
WHAT = {ACTIVE: "active codes (DM11)", STORED: "stored / previously active codes (DM3)",
        BOTH: "active and stored codes (DM11 + DM3)"}
ANSWER_WAIT = 1.0           # seconds to collect acknowledgments
NAMES = {DM11: "DM11", DM3: "DM3"}


def warning(which: str = ACTIVE, da: int = GLOBAL_SA) -> str:
    """What front-ends show before clearing, so every one warns the same way."""
    where = "every module" if da == GLOBAL_SA else sa_name(da)
    return (f"Clear {WHAT[which]} on {where}?\n\n"
            "This also:\n"
            "  • erases the diagnostic data kept with those codes (occurrence counts, freeze frames),\n"
            "  • can reset emissions readiness on engines that report it (DM5),\n"
            "  • turns warning lamps off. They come back, with the code, if the fault is still there.\n\n"
            "Read and note the codes first: once cleared, the history is gone.\n"
            "Do it with the engine off and the key on.")


@dataclass
class ClearResult:
    which: str
    da: int
    sent: list[int] = field(default_factory=list)      # PGNs actually requested
    answers: list[Ack] = field(default_factory=list)   # acknowledgments to them
    tx_error: str | None = None
    refused: str = ""                                  # why nothing was sent

    @property
    def ok(self) -> bool:
        return bool(self.sent) and not self.tx_error and not any(a.control != 0 for a in self.answers)

    def lines(self) -> list[str]:
        if self.refused:
            return [self.refused]
        if self.tx_error:
            return [f"Sending failed: {self.tx_error} (nothing ACKing on the bus? wrong bitrate?)"]
        out = []
        for pgn in self.sent:
            got = [a for a in self.answers if a.pgn == pgn]
            for a in got:
                out.append(f"{sa_name(a.sa)}: {NAMES.get(pgn, pgn)} {a.text}")
            if not got:
                out.append(f"{NAMES.get(pgn, pgn)} sent to every module (they don't answer a global clear)"
                           if self.da == GLOBAL_SA else f"{sa_name(self.da)}: no answer to {NAMES.get(pgn, pgn)}")
        out.append("Read the codes again to check what cleared.")
        return out


def clear_codes(session: Session, which: str = ACTIVE, da: int = GLOBAL_SA, *, wait: float = ANSWER_WAIT,
                sleep=time.sleep) -> ClearResult:
    """Send the clear and collect the answers. Front-ends must have asked the user first."""
    r = ClearResult(which, da)
    if session.offline or session.listen_only:
        r.refused = "Listen-only: nothing was sent. Clearing codes has to transmit."
        return r
    t0 = time.time()
    for pgn in PGNS[which]:
        if session.request(pgn, da):
            r.sent.append(pgn)
        else:
            r.tx_error = session.health.tx_error or "the adapter refused the frame"
            break
        sleep(0.05)
    if r.sent:
        sleep(wait)
        r.answers = [a for a in session.acks_since(t0) if a.pgn in r.sent]
    return r
